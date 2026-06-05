# SlicerLNQ

3D Slicer extension for the LNQ (Lymph-Node Quantification) project. Two modules:

- **LymphNodeQuantifier** — multi-region segmentation against any CT loaded in the scene. Lists every model registered in [`lnq-segmenter`](https://github.com/pieper/lnq-segmenter) as a checkbox; queued runs land in a single composite `vtkMRMLSegmentationNode` with per-region SNOMED-CT tags and colors. Use this if all you want is "segment lymph nodes on this CT."
- **LNQStudio** — tabbed workflow against a running [SlicerLNQ-Chronicle](https://github.com/pieper/SlicerLNQ-Chronicler) backend (CouchDB + dicomweb-server on Jetstream2): cohorts, protocols, projects, annotation, training observability, model deployment.

Design context: [docs/architecture.md](docs/architecture.md) (system-level) and [docs/plans.md](docs/plans.md) (UX vision). The Chronicle data model — JSON Schemas + the `validate_doc_update` enforcement — lives in [SlicerLNQ-Chronicler/schemas/](https://github.com/pieper/SlicerLNQ-Chronicler/tree/main/schemas) and [SlicerLNQ-Chronicler/design/](https://github.com/pieper/SlicerLNQ-Chronicler/tree/main/design).

## Status

Phase 2 scaffolding. Working today:

- **Config** tab — connect to a Chronicle, persist URL/user/password in QSettings, verify the database has been provisioned.
- **Cohorts**, **Protocols**, **Projects** tabs — list existing documents, view full JSON, create new ones via simple forms.
- **Annotate**, **Train**, **Infer**, **Review**, **Deploy**, **Dashboard** tabs — placeholders. Annotate ships next; the remainder are Phase 3.

The chronicle client ([LNQStudio/LNQStudioLib/chronicle_client.py](LNQStudio/LNQStudioLib/chronicle_client.py)) is intentionally Slicer-free and may be reused by Chronicle agents (cohort resolver, etc.) later.

## Development install

The fastest way to try the module without a full extension build:

1. Have a running Chronicle. From a fresh stand-up, this is `bin/chronicle.sh create` in [SlicerLNQ-Chronicler](https://github.com/pieper/SlicerLNQ-Chronicler), then `bin/deploy-design.sh` to install the validator.
2. In Slicer: **Edit → Application Settings → Modules → Additional module paths** → add the path to this repo's `LNQStudio/` directory.
3. Restart Slicer. *LNQ Studio* appears under the **LNQ** category in the module selector.
4. Open it, switch to the **Config** tab, fill in the Chronicle URL + admin password (the same one in `chronicle.conf`), click **Connect**.

For the full extension build (so the extension manager picks it up like any other), use the standard Slicer extension flow: configure with CMake against your Slicer build, `make`, install. See [Slicer's extension docs](https://slicer.readthedocs.io/en/latest/developer_guide/extensions.html).

## Repository layout

```
LNQStudio/
  LNQStudio.py                         # module + widget + all tabs
  LNQStudioLib/
    chronicle_client.py                # HTTP client; Slicer-free
  Resources/Icons/LNQStudio.png        # placeholder; replace before first release
  Testing/Python/test_LNQStudio.py     # CTest smoke test
docs/
  architecture.md                      # system design across the three repos
  plans.md                             # UX vision and per-tab intent
CMakeLists.txt                         # extension-level
LNQStudio/CMakeLists.txt               # module-level
```

## Auth note

Phase 2a auth (native CouchDB `_users`) and 2b (Caddy OIDC + proxy auth) are not yet wired. Today the module connects with the CouchDB admin credentials, which is the same credential `bin/deploy-design.sh` uses. Acceptable for a single-operator dev environment; revisit when more users come online.
