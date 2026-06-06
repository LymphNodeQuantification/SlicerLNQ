# Review deployment — Slicer over remote desktop

Status: spec only; not yet implemented.

## Goal

An expert opens a URL, signs in, sees a CT with the model's prediction
already overlaid, makes a few clicks, hits Save, and the next case is
ready in under a second. No Slicer install, no DICOM dialogs, no
"which volume node?" confusion. Cycle time per easy case: under a
minute.

The interaction model is the one specified in [review-tab.md](review-tab.md);
this document is about *delivery* — what the reviewer sees, how cases
get queued, and how the experience stays fast.

## Architecture: SlicerLNQ + remote desktop, no separate web app

The decision (after considering OHIF + custom web tooling) is to keep
the entire review experience inside Slicer and deliver it over a
remote-desktop frame in the browser.

Why this works:

- **Zero code duplication.** Slice rendering, segmentation overlays,
  Markups fiducials, paint brush, undo/redo, sequence scrubbing — all
  already exist. The Review tab in LNQStudio is the only new UI.
- **No second backend.** Chronicle (CouchDB + dicomweb-server) already
  holds cases, annotations, and model outputs. The web app would have
  needed a parallel API and second auth layer.
- **Forward-compatible.** When we integrate SlicerNNInteractive,
  SAM-medical, or any other Slicer extension, the reviewer gets it for
  free — no plugin rewrite. Same for any future cohort/annotation
  tooling.
- **Lower per-reviewer cost than a "fat client" install.** No support
  load for OS/Slicer version mismatches; everyone runs the same
  pinned instance.

## What the reviewer sees

A custom Slicer layout chosen on launch hides almost all Slicer chrome
and surfaces only what the Review tab needs:

```
+-----------------------------------+----------------------------+
|                                   |  Case 42 of 200            |
|                                   |  Patient MED_LYMPH_021     |
|                                   |  Mediastinal               |
|                                   |                            |
|                                   |  Threshold (log)           |
|        Red slice                  |  [=====]    0.001          |
|     (CT + Inferno overlay         |  [  Auto from prompts (a)] |
|      + composite SEG outline      |                            |
|      + reference GT outline)      |  Tools                     |
|                                   |  [+] Positive          (1) |
|                                   |  [-] Negative          (2) |
|                                   |  [○] Sphere paint      (3) |
|                                   |  [-] Erase             (4) |
|                                   |  [↶] Undo              (u) |
|                                   |                            |
|                                   |  Case notes                |
|                                   |  [                       ] |
|                                   |  [                       ] |
|                                   |                            |
|                                   |  [   Save & next       (↵)]|
|                                   |  [   Skip               (s)]|
|                                   |  [   Reject — quality   (r)]|
|                                   |  [   Escalate           (e)]|
+-----------------------------------+----------------------------+
                                 [j / k scrub]
```

Two principles for this surface:

1. **Every action is reachable by mouse.** No "hidden behind a
   keyboard shortcut" features. The tools panel is a column of
   buttons, the threshold has a visible slider, the case notes are
   always-visible text fields. A reviewer who never touches the
   keyboard should be able to do everything except the speed pass.
2. **Every shortcut is visible.** Each button shows its hotkey in
   parens — `Save & next (↵)`, `Positive (1)` — so the reviewer
   *learns* the shortcuts by using the buttons. After a session
   they're flying on keys; from the first click everything just
   works.

Hidden by default: the module selector, the Data tree, the Module
panel header, the Help & Acknowledgement collapser, the Reload & Test
section, the Python console, the 3D view (until the reviewer toggles
it), the slice intersection lines, the orientation widget. Visible:
one slice view + the Review-tab control surface.

Keyboard shortcuts (the difference between cycle time = 30 s and cycle
time = 3 s on the speed-pass after the buttons have been internalised):

| key | action |
|---|---|
| `j` / `k` | scrub slice down / up |
| `1` | positive-click tool |
| `2` | negative-click tool |
| `3` | sphere brush |
| `4` | erase brush |
| `a` | "auto threshold from prompts" |
| `[` / `]` | step threshold down / up (log-scaled) |
| `enter` | Save & next |
| `s` | Skip (defer to later in queue) |
| `r` | Reject — poor quality |
| `e` | Escalate (flag for a second reviewer) |
| `u` | Undo last action |

These are wired through Slicer's `qt.QShortcut` against the main
window — works in a remote-desktop frame because key events get
forwarded by the streaming layer. Every shortcut also fires the same
slot the button does, so behavior is identical between paths.

### Case-level outcomes

Per case the reviewer can choose one of four exit actions:

- **Save & next** — corrected mask + prompts + notes get written, this
  case advances `producer.kind == "review"`, case becomes
  training-eligible (see below).
- **Skip** — defer to later in the queue. No annotation written. The
  case re-appears at the end of the assignment.
- **Reject — poor quality** — opens a small reason picker (motion,
  contrast bolus, missing slices, slice thickness, other-with-text).
  The case is *not* training-eligible, even if any segments were drawn.
  Reason gets stored on the Annotation as `quality_flag` +
  `quality_reason`. Useful both for filtering training data and for
  going back to whoever curated the cohort and saying "stop including
  cases like this."
- **Escalate** — flag for second reviewer. Annotation may be partial.
  The case lands in a `ReviewAssignment.escalated` list that other
  reviewers (or a supervising radiologist) see at the top of their
  queue.

### Case notes

A persistent free-form text area sits above the action buttons. It's
saved as `Annotation.case_notes` regardless of which exit action the
reviewer picks. Used for "interesting case", "ask Tagwa to confirm
SVC location", "patient has surgical clips", anything else. Indexable
later from LNQStudio if we want to surface "find cases with notes
mentioning X."

## Case-flow loop

The queue lives on top of the existing `Cohort` / `CohortResolution` /
`Project` doc types. We add one new doc type and augment two existing
ones; we do *not* add a separate `ReviewQueue` because a Project
already names a Cohort and the queue ordering is a property of the
Cohort.

### Doc types touched

- **`Cohort`** (existing) — the set of `case_id`s being reviewed. The
  IDC NIH cohort gets ingested as a Cohort just like the LNQ phase-2
  cohorts were. New optional field `case_order` to pin the queue
  walking order; absent ⇒ sort by `case_id`.
- **`CohortResolution`** (existing) — the snapshot of where the files
  actually live. Used by the pre-compute worker (see below) to know
  where to read CT volumes from.
- **`Project`** (existing) — the review *workflow*: ties cohort +
  members + protocol. Augmented with a `project_kind` field — values
  so far: `annotation`, `training_export`, and the new `review`.
- **`ReviewAssignment`** (new) — one per (project, reviewer) pair.
  Carries `cursor` (current case index in the project's cohort),
  `completed_case_ids`, `skipped_case_ids`, `escalated_case_ids`, and
  `rejected_case_ids`. Survives page refreshes so Save & next picks
  up where the reviewer left off.
- **`Annotation`** (existing, augmented) — saved corrected mask,
  prompt fiducials, threshold the reviewer settled on,
  `model_generation_id` that seeded the review, `quality_flag` (good
  | rejected_poor_quality | rejected_other), `quality_reason` (short
  string), `case_notes` (free text). `producer.kind` gains a `review`
  value alongside the existing `manual` and `model`.

### API surface

All over the existing CouchDB HTTP, no new server process:

| endpoint | purpose |
|---|---|
| `GET /lnq/_design/review/_view/projects?reviewer=...` | list review-kind projects this reviewer is a member of |
| `GET /lnq/_design/review/_view/next?project=...&reviewer=...` | next un-reviewed case in project's cohort (uses CohortResolution to resolve to files) |
| `PUT /lnq/annotation:<uuid>` | save the corrected mask + prompts + notes + quality_flag |
| `PUT /lnq/reviewassignment:<id>` | update cursor / skip / escalate / reject lists |

The Review tab's Save & next button is a PUT to `annotation:` followed
by a PUT to `reviewassignment:` (updating the cursor + completed list)
followed by a GET to `next`. The Slicer-side code then asks
dicomweb-server for the CT and asks Chronicle for the pre-computed
SEG + probability NRRD blobs registered against this
`(case_id, model_generation_id)` pair.

### Closing the loop: corrected reviews → next training round

The Train tab gains a filter on `producer.kind == "review"` and
`quality_flag != "rejected_*"`. The "Include expert-corrected reviews"
toggle is then `producer.kind in {manual, review}` AND
`quality_flag in {good, null}`. Rejected cases stay out of training
no matter how attractive the corrected mask looks.

This makes the IDC → review → training pipeline self-closing:

1. `bin/ingest-idc-cohort.py` pulls a TCIA collection (or any IDC
   `collection_id`) into chronicle as Cohort + CohortResolution. NIH
   ct_lymph_nodes ingests as `cohort:ct_lymph_nodes_phase1`.
2. A `Project` of kind `review` is created against the cohort with
   the relevant model_generation_id as the seed.
3. Pre-compute worker runs lnq-segmenter on every case in the
   cohort, writes SEG + prob NRRDs as Blobs. (Details below.)
4. Reviewer signs in, hits Save & next some N times, produces
   Annotations with `producer.kind == "review"`.
5. Next training round of mediastinal-v2 (or whatever) sets the Train
   tab filter to `producer.kind in {manual, review}` and gets the
   corrected NIH cases mixed in with the Tagwa-curated ones.

The same plumbing works for any future IDC collection — we just need a
human to point `ingest-idc-cohort.py` at it and pick which model to
seed the review with.

## The pre-compute pipeline

When a `Project` of kind `review` is created, its cohort is enumerated
and a batch inference job runs *before any reviewer touches anything*.
The reviewer hits an entirely hot cache: CT comes from dicomweb-server
(already fast), SEG + probability come from chronicle Blobs (already
on local disk).

Shape:

- A small daemon (`bin/precompute-worker.py`) watches the chronicle
  changes feed for new `Project` docs with `project_kind == "review"`.
- For each new review project, the daemon resolves the cohort,
  iterates `case_id`s, and for each:
  - Looks up `(case_id, model_generation_id)` in chronicle. If a Blob
    pair (SEG + probability) already exists for this hash, skip.
    Otherwise:
  - Pulls the CT (from Manila if the case is one of our own, from a
    cached IDC mirror if it's an IDC ingest).
  - Runs `lnq-segmenter predict --probability-output` for the relevant
    anatomy.
  - Registers the SEG + probability NRRD as Blobs with `kind:
    "model-output"` and a `seed_for_review: project:<uuid>` provenance
    tag.
- Idempotent: re-runs on the same project find existing Blobs and
  skip. New reviewer assignments don't trigger recomputation —
  inference is keyed on `(case_id, model_generation_id)` only.

Hosting the worker is intentionally not specified here — it could be a
systemd timer on an ephemeral g3.xl Js2 instance that the
trainer-watcher pattern already knows how to launch and shelve, or a
long-running service on a small always-on instance. Whatever's cheapest
that holds the "reviewer never waits" contract.

Two principles to hold:

- **The reviewer should never wait for inference.** First paint < 1 s
  after Save & next or first sign-in. Anything slower kills throughput.
- **Pre-compute is idempotent.** A case that re-enters a new project
  (different anatomy, different reviewer batch) finds existing model
  outputs by `(case_id, model_generation_id)` hash and skips
  recomputing.

### Batch IDC ingest

`bin/ingest-idc-cohort.py` (new) handles the IDC → chronicle step:

```
ingest-idc-cohort.py \
    --idc-collection ct_lymph_nodes \
    --anatomy-filter MEDIASTINUM \
    --cohort-name "NIH ct_lymph_nodes mediastinal phase1" \
    --create-review-project mediastinal-v1
```

What it does:

- Queries `idc_index` for the collection (+ optional body-part filter)
  and pulls every CT series via `download_from_selection`. CTs stage
  on Manila under `/media/share/LNQ-data/idc/<collection>/<patient>/`.
- For each case, runs the same DICOM SEG → NRRD conversion we used
  for our own training data so any `Modality=SEG` series in the IDC
  collection lands as a reference NRRD next to the CT.
- Creates a `Cohort` and a `CohortResolution` in chronicle pointing at
  those Manila paths.
- If `--create-review-project <model_name>` is set, also creates a
  `Project` of `project_kind=review` with that model_generation as the
  seed. The pre-compute worker picks it up via the changes feed.
- All work is idempotent: re-running picks up where it left off.

This means the workflow for absorbing a new IDC collection into the
training pipeline is one shell command, then reviewer clicks.

## Auth

Per existing project preference (Google + ORCID, not CILogon):

- Reviewer hits `lnq-review.isomics.dev` (or similar) and is bounced
  to a sign-in page offering Google and ORCID.
- The OAuth handler issues a session cookie that the remote-desktop
  layer validates before granting an X session.
- Reviewer identity (`pieper@isomics.com` or `0000-0001-2345-6789`) is
  embedded in `ReviewAssignment.reviewer` and any saved
  `Annotation.created_by` field.

ORCID is the better choice for radiologists who already have one; the
Google option is for everyone else.

## Hosting (deferred)

Hosting details are explicitly out of scope here — we'll work them out
when we're ready to stand the service up. Plausible directions:

- A new Js2 instance running KasmVNC or noVNC behind Caddy, sized to
  one reviewer per instance, scaled horizontally as we add reviewers.
- The existing per-instance Desktopia setup we've been using for the
  CUDA-box MCP testing, productised for multi-reviewer use.
- A managed remote-desktop service that points at a stateless Slicer
  AMI.

The interaction design is decoupled from this choice; the Review tab
runs identically in all three.

## Future integration: SlicerNNInteractive

Because the review experience is "just Slicer", we can install
[SlicerNNInteractive](https://github.com/wasserth/SlicerNNInteractive)
as a sibling extension on the same image. The Review tab's prompt
tools then have a second target: instead of (or in addition to)
re-thresholding the model's probability map, the positive/negative
prompts are forwarded to the nnInteractive model running on the
review-server's GPU. The reviewer sees the resulting refined
segmentation immediately and accepts/rejects exactly like the
threshold-grown one.

The Review tab UI doesn't change shape — the "Run interactive model"
button mentioned in review-tab.md is what dispatches to either the
local probability-map sweep or the nnInteractive backend. Adding new
interactive backends later (SAM-medical, ScribblePrompt, whatever
2026 brings) is a routing change, not a UI change.

## What gets built

| step | effort | deliverable |
|---|---|---|
| 1 | s | "Review" layout in LNQStudio: custom QLayout that hides Slicer chrome and surfaces the Review tab + one slice view |
| 2 | s | Keyboard shortcut map (qt.QShortcut tied to the main window) |
| 3 | s | CouchDB design doc for the review queue/assignment views |
| 4 | m | Review-tab Save & next loop: PUT annotation → GET next case → load CT + SEG + probability |
| 5 | m | Pre-compute worker (rough — Chronicle changes feed → run inference → write blob) |
| 6 | m | Remote-desktop hosting on a single Js2 instance + Google/ORCID auth proxy |
| 7 | s | Add SlicerNNInteractive to the image; route prompts to it as a second backend |

Steps 1–4 are the things the reviewer sees. 5–6 are infra. 7 is the
post-v1 nnInteractive integration. The hard part is 4 — chronicle
schema for ReviewQueue/Assignment + the views + reliable next-case
delivery.

Total before users touch it: ~2 weeks for steps 1–4 with a real Slicer
extension developer; longer if we have to debug remote-desktop key
forwarding (steps 6–7 might surface latency-sensitive issues that
need profiling).
