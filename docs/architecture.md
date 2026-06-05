# SlicerLNQ Architecture

This document is the bridging design between [plans.md](plans.md) (the application/UX vision) and the implementation split into separate repositories. It uses the *Chronicle* vocabulary from the [2014 Chronicle pamphlet](https://docs.google.com/document/d/1gLv74QRUYPSZTHe5o_9KaTbKJhRZ0rHFBXAp3LqFM7s/edit) (Pieper, Isomics) — SlicerLNQ is Chronicle scoped to the lymph-node quantification domain, with twelve years of intervening tooling (dcmjs, dicomweb-server, OHIF, Jetstream2, modern CouchDB, GitHub Actions) that finally make the design practical to operate.

## Three artifacts, three repositories

- **Chronicler** — a forkable GitHub repository template that defines the operational lifecycle of a single Chronicle instance on Jetstream2: Terraform/OpenTofu for the VM and storage, cloud-init for first-boot provisioning, Docker Compose for the running services, runbooks for backup / restore / health / role assignment, and GitHub Actions workflows that drive lifecycle, scheduled maintenance, and (later) shelve/unshelve to manage SU burn. Chronicler is intentionally generic; nothing about it is LNQ-specific.

- **SlicerLNQ-Chronicler** — the LNQ project's fork of Chronicler. Holds Js2 application credentials (as GitHub secrets), the JSON Schemas and `_design` documents that define the SlicerLNQ document model, the OIDC client configuration for ACCESS/CILogon, and any LNQ-specific overlays on the upstream containers. Operating the LNQ backend means running workflows in this repo.

- **SlicerLNQ-Chronicle** — the running CouchDB + dicomweb-server instance on Js2 that SlicerLNQ-Chronicler stands up and maintains. Stateful but rebuildable from the repo plus a backup snapshot.

- **SlicerLNQ** *(this repo)* — the 3D Slicer extension implementing the LNQStudio module that talks to SlicerLNQ-Chronicle. The application layer.

## Document model (Chronicle vocabulary)

The store holds **immutable, time-ordered documents that reference earlier documents** (Chronicle pamphlet §8). Updates are new documents, not edits to old ones; the only mutations are corrections.

Top-level entity documents in the LNQ instance:

- **Cohort** — selection criteria, source URIs (IDC queries, DICOMweb endpoints, local URIs), and a frozen list of resolved patient/study references. CONSORT-diagram metadata.
- **Protocol** — annotation SOP, color table with controlled terminology, decision rules for ambiguous boundaries.
- **Project** — binds a Cohort to a Protocol with user roles (admin / annotator / reviewer).
- **Annotation** — references a single (study, segmentation) pair plus the Project context, annotator identity, software/hardware fingerprint, and status notes.
- **ModelGeneration** — a single trained model, referencing its training Cohort, Protocol, Annotations, training-job log URIs, container image hash, git SHA, and hardware fingerprint. (`retro6` becomes `ModelGeneration {"label": "retro6", ...}`.)
- **InferenceRun** — a model applied to a cohort or scan, referencing the ModelGeneration and the input Cohort, with output SEG references.
- **ComparisonReport** — agent-materialized derived document with per-case Dice / Hausdorff / etc. across two or more InferenceRuns. Powers the Review tab without ad-hoc analytics.
- **Comment** — signed commentary documents from the Deploy tab, referencing the SEG they comment on, validated on write by `validate_doc_update`.

DICOM instances themselves (source CTs/PETs, derived SEGs, derived SRs) live behind dicomweb-server. Documents in CouchDB hold pointers (URL + sha256 + size), never bulk pixel data.

## Agents and the changes feed

The LNQ-retro scripts (`lnq-retro2.py` … `lnq-retro6.py`) are Chronicle agents implemented imperatively. The systematization goal is to promote them to long-running services that subscribe to filtered changes feeds and emit derived documents:

- **Cohort resolver** — on Cohort creation, materializes the resolved patient/study list from source URIs.
- **Training-set builder** — on Project + sufficient Annotations, prepares an `nnUNet_raw` tree on the share and emits a TrainingJob document. (Phase 3.)
- **Trainer launcher** — on TrainingJob, launches a Js2 GPU instance via clouds.yaml, monitors logs into Swift, emits ModelGeneration when complete.
- **Inference launcher** — on InferenceRun creation, runs the corresponding model on the referenced cohort, emits SEGs via dicomweb-server.
- **Comparison materializer** — on InferenceRun completion, computes metrics against ground-truth Annotations and emits ComparisonReport.

Agents run on the Chronicle host or on dedicated Js2 instances launched on demand. Phase 1 has no agents; phase 2 introduces the cohort resolver; phase 3 introduces the training/inference loop.

## Phase plan

**Phase 1 — Infrastructure stand-up.** SlicerLNQ-Chronicler repo with Terraform, cloud-init, Compose. Two-instance topology on a separate Js2 personal allocation, isolated from the production `LNQ-data` share: an always-on m3.tiny **doorman** terminating TLS and managing the lifecycle of the m3.medium **core** running CouchDB and dicomweb-server. Activity-driven shelving with a 20-minute idle timeout (see below). Core is built from a Js2 featured image so the Exosphere web desktop is preserved for hands-on poking. TLS via Let's Encrypt under `lnq-chronicle.isomics.dev` (later `lnqproject.org`). Admin auth only; basic-auth on Fauxton at `/_utils`. Dev test data is a small slice of public IDC studies in the dev Swift bucket. Done when: `tofu apply` from a clean repo brings up the two-instance topology, the core auto-unshelves on first request within ~90 s, and a test STOW + QIDO + WADO round-trip works against IDC data.

**Phase 2 — Auth, schemas, Slicer extension.** Auth introduced in two stages to match the user pool. **Stage 2a (≤5 testers):** native CouchDB `_users` with admin-provisioned passwords; Caddy passes Basic auth through. **Stage 2b (broader):** Caddy adds OIDC with Google + ORCID as identity providers, mapping claims to CouchDB roles via [proxy authentication](https://docs.couchdb.org/en/stable/api/server/authn.html) headers and a shared secret. Per-database `_security` docs for the LNQ databases. JSON Schemas and `_design/_validate` deployed via a `deploy-views.yml` workflow. Cohort resolver agent comes online. SlicerLNQ extension implements the Config, Cohorts, Protocols, Projects, and Annotate tabs against the live instance. Done when: an LNQ team member signs in with Google or ORCID, gets the right role, and can complete an annotation that round-trips through the database.

**Phase 3 — Training, inference, review, deploy.** Train and Infer tabs launch Js2 GPU instances via clouds.yaml; trainer/inference launcher agents handle the workflow. Review tab consumes ComparisonReports. Deploy tab spins up an OHIF site (decision deferred from this doc) consuming a published cohort + inference, with signed-comment ingest. Done when: a full retro-style generation runs end-to-end through the system without dropping out to ad-hoc scripts.

## Deployment topology (phase 1)

```
            Internet
                │
                ▼
  lnq-chronicle.isomics.dev  (A record → doorman floating IP)
                │
                ▼
        ┌──────────────────────────┐
        │        Doorman           │  m3.tiny, always on
        │  ┌────────────────────┐  │  Caddy: TLS + OIDC (phase 2)
        │  │ Caddy + Watchman   │  │  Watchman: shelve/unshelve core
        │  └────────────────────┘  │  on activity; placeholder during wake
        └──────────────┬───────────┘
                       │  internal Js2 network (no public route to core)
                       ▼
        ┌──────────────────────────┐
        │          Core            │  m3.medium, shelved when idle
        │  ┌──────────┐ ┌───────┐  │  Js2 featured image
        │  │ CouchDB  │ │ dwsv  │  │  → Exosphere web desktop preserved
        │  │   3.5    │ │       │  │  → pinned-commit dicomweb-server +
        │  └────┬─────┘ └───┬───┘  │     LNQ patch branch (PR upstream)
        └───────┼───────────┼──────┘
                │           │
         ┌──────┴────┐ ┌────┴─────┐
         │  Manila   │ │  Swift   │   (separate Js2 allocation,
         │  share    │ │  bucket  │    isolated from production)
         │  (work)   │ │ (backup) │
         └───────────┘ └──────────┘

/var/lib/couchdb on core's root disk for now (migrate to cinder volume in phase 3 if needed).
```

GitHub Actions in SlicerLNQ-Chronicler talks to Js2 via OpenStack application credentials (stored as GitHub secrets, scoped to this allocation, rotated quarterly). The watchman on the doorman holds a *separate, narrower* application credential used only for shelve/unshelve. Terraform state lives in a versioned Swift bucket via the OpenStack backend. SSH-into-the-box runbooks use a deploy key generated specifically for Actions.

## Activity-driven lifecycle (dev mode)

For development, the core auto-shelves after idle and auto-unshelves on traffic, so SU burn closely tracks actual use without manual operator action. The doorman is always on; the core is shelved whenever no one is using it.

**Watchman** service on the doorman (small Node/Fastify daemon):

1. Tracks `last-activity-at` from Caddy's structured access log, ignoring known internal traffic (its own health pings, the GitHub Actions external monitor) recognized via path or User-Agent.
2. After **20 minutes** of no real activity, calls `nova shelve` on the core via OpenStack.
3. On any incoming request while the core is shelved, immediately returns a placeholder HTML page (auto-refresh every 5 s, friendly "spinning up the LNQ server, ~90 s" copy) and issues `nova unshelve`.
4. Polls `core:5984/_up` until ready, then transparently proxies subsequent requests. Activity timer resets.
5. Serializes lifecycle transitions so a shelve-in-flight isn't immediately countermanded by an arriving request, and vice versa.

**Failure modes considered up front:**

- *Doorman crash:* systemd `Restart=on-failure` handles transients. Permanent failure means rebuilding the doorman from Terraform; the core is unaffected.
- *OpenStack API failure during shelve/unshelve:* watchman logs and alerts via the GitHub Actions issue mechanism. No aggressive retries — an OpenStack outage shouldn't translate into a thundering herd. Manual recovery via `runbooks/wake.sh`.
- *Unshelve takes longer than expected:* placeholder keeps refreshing. User sees a longer spinner, not a broken site.
- *Sticky activity from monitors:* the external health workflow targets `/health/doorman` (handled by the watchman, never proxied to the core), so it doesn't keep the core hot. Same exclusion for the Caddy ACME refresh path.
- *Web-desktop access while shelved:* Exosphere talks to the OpenStack control plane, not through the doorman, so unshelving from the Exosphere UI works regardless. Hitting the website is the simpler path most of the time.

This dev-mode pattern is **explicitly scoped to phases 1 and 2**. Phase 3 with real users may switch to always-on (with the doorman either retired or kept as a TLS/OIDC frontend without the lifecycle role), depending on observed usage patterns.

## Security and auth posture

- No admin-party. Admin password from secret, set on first boot.
- HTTPS only, automatic certs via Caddy.
- CORS allowlist tied to the SlicerLNQ extension's origin and (phase 3) the Deploy site's origin only.
- Phase 2a: native CouchDB `_users` with admin-provisioned passwords. No external IdP. Right cost for ≤5 testers.
- Phase 2b: OIDC via Caddy with **Google** (broadest coverage for clinical collaborators) and **ORCID** (research-native, also the identity used for signed comments in the Deploy tab). Caddy injects `X-Auth-CouchDB-UserName` + `X-Auth-CouchDB-Roles` headers; CouchDB's [proxy authentication handler](https://docs.couchdb.org/en/stable/api/server/authn.html) trusts them via a shared secret. Roles assigned by Chronicler runbooks updating per-database `_security` docs. Additional IdPs (GitHub, CILogon, institutional Azure AD, Microsoft) can be added later as plug-ins to Caddy without restructuring; not built until a real need shows up.
- Phase 2+: `validate_doc_update` functions enforce schema on writes; signed-Comment validation on the Deploy databases.
- Phase 1 data is public anyway; security posture is correct from day one but not load-bearing yet.

## Backup and disaster recovery

- Nightly `runbooks/backup.sh` rsyncs `data/` and `etc/` from the host to a Swift bucket in the same allocation. 14-day rolling window.
- Weekly tarball off-site (separate Swift bucket in a different allocation, or local NAS).
- Smoosh handles compaction automatically. Disk-fill alert at 50% to ensure compaction headroom.
- `runbooks/restore.sh` rebuilds an instance from a snapshot ID — exercised periodically as a real test, not just documented.
- View indexes are backed up alongside `data/.shards`. First-query post-restore re-index is otherwise an outage on its own.

## Cost / SU envelope

Two instances: always-on doorman (m3.tiny, **1 SU/hr**) and activity-shelved core (m3.medium, **8 SU/hr** only while awake).

- Doorman, always on: ~8,760 SU/year.
- Core, dev-mode bursts (~30 hr/wk active): ~12,500 SU/year.
- Core, phase-2 ramp with pilot users (~50 hr/wk): ~21,000 SU/year.
- Core, phase-3 24/7: ~70,000 SU/year — at this point migrate off personal allocation, and the doorman's lifecycle role is probably retired.

Combined dev + phase-2 burn: roughly **21k–30k SU/year**. The 44k+ personal allocation lasts ~1.5–2 years before phase 3 forces migration to a project allocation.

Plus GPU bursts for training jobs in phase 3, on a separate allocation and budget. Manual `shelve.yml` / `wake.yml` workflows exist as overrides and recovery; activity-driven shelving handles day-to-day.

## Non-goals (intentional)

- HA / multi-node CouchDB. Single-node, document the upgrade path, defer.
- Scale beyond the LNQ project's data volumes. Generality is for *forking* Chronicler, not for scaling one instance.
- PHI handling. Public data only; if scope changes, this document needs revision.
- A managed-cloud fallback. The whole point is to *not* lock into one.
- Replacing dcm4chee, Orthanc, or any production PACS. Adjacent territory, different design center.

## Open items

- **Upstream dicomweb-server revival path.** Maintain a branch in SlicerLNQ-Chronicler, PR back to `dcmjs-org/dicomweb-server` as patches stabilize. Eventual goal: collapse the overlay back into upstream.
- **OHIF Deploy site topology** (deferred from this doc). Likely a second Compose service on the same instance initially, sibling instance later.
- **Off-site backup destination.** Swift bucket in a different ACCESS allocation, or local NAS. Decide before phase 2.
- **Schema migration discipline.** When `_design/_validate` changes, what's the deploy procedure? Probably: stage on a clone DB, dual-write during transition, swap. Detail in phase 2.
- **Watchman implementation choice.** Node/Fastify daemon vs. a custom Caddy module (latter is more elegant but has a Caddy-plugin learning curve). Node is the path of least resistance and matches the rest of the stack. Pick before phase 1 build-out.
- **Activity-signal definition.** What exactly counts as "real" traffic? Default: any request to a core-proxied path that isn't `/health/*`, `/.well-known/*`, or from a known internal User-Agent. Refine as we observe traffic patterns in phase 1.
