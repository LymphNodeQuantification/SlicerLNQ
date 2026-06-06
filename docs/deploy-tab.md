# Deploy tab — design spec

Status: spec only; not yet implemented.

## Why this exists

Today every model in the [`lnq-segmenter`](https://github.com/pieper/lnq-segmenter)
registry ships as a single 4 GB bundle (5-fold ensemble, fp32, mirroring
TTA). That's the right choice for "I want the best Dice this dataset can
produce" but the wrong choice for almost everything else — installation
download, real-time review previewing, point-of-care deployment.
[Future optimization notes](../README.md) cover the technical options
(reduce ensemble, fp16/int8 quant, skip TTA, distill, etc.); this
document is about the *workflow* for publishing multiple optimization
profiles of the same model side by side, with the empirical
accuracy-vs-speed-vs-size tradeoff measured and surfaced to whoever's
making the deploy decision.

The interaction split:

- **LNQStudio → Deploy tab** is the publisher-side view. Someone with
  permission to mint a release picks which variants to produce, sees a
  comparison table against a versioned eval cohort, and uploads the
  bundles + metadata to GitHub Releases + chronicle.
- **LymphNodeQuantifier → install panel** is the consumer-side view.
  A user with no publishing rights picks which variant to download for
  inference, sees a simpler card-per-variant with just download size +
  headline Dice + one sample PNG.

Both panels read from the same `lnq-segmenter` registry. The asymmetry
is in what they let you *do*, not in what they let you *see*.

## Initial variant set

Ships with two profiles per model. The spec is forward-compatible with
more (the registry schema accepts a `variants: [...]` array of arbitrary
length), but the cognitive load on the consumer-side picker stays low
with just two.

| variant | folds | dtype | TTA | tile step | export | nominal size | expected Dice cost vs accurate |
|---|---|---|---|---|---|---|---|
| `accurate` | 5 | fp32 | mirror-8 | 0.5 | PyTorch | ~4 GB | baseline |
| `fast` | 1 (highest-val-Dice fold) | fp16 | off | 0.7 | PyTorch | ~400 MB | empirically 1–3 % Dice; per-anatomy in the table |

The `fast` profile picks a single fold (the one with the highest
held-out validation Dice from the training run, recorded on the
corresponding `ModelGeneration` doc) rather than averaging an
arbitrary subset, so it's reproducible and we don't have to think
about how to mix fold subsets.

Later profiles to consider as the infra matures: `balanced`
(2 folds + fp16) as a middle point, and `tiny` (TensorRT int8 single
fold ≈ 100 MB) for point-of-care.

## Registry schema additions

Additive to [`_registry.json`](https://github.com/pieper/lnq-segmenter/blob/main/src/lnq_segmenter/_registry.json).
A model entry today has a single `weights_assets: [...]` array plus
the top-level `weights_url_template` etc. The change is to add an
optional `variants: [...]` array that supersedes those when present:

```json
{
  "name": "mediastinal-v1",
  "version": "1.0.0",
  "...": "...",
  "default_variant": "accurate",
  "variants": [
    {
      "id": "accurate",
      "display_name": "Accurate",
      "summary": "5-fold ensemble, fp32, mirroring TTA. Best Dice.",
      "folds": [0, 1, 2, 3, 4],
      "dtype": "float32",
      "use_mirroring": true,
      "tile_step_size": 0.5,
      "weights_assets": [{ "...": "..." }],
      "size_bytes_total": 4078000000,
      "eval": {
        "cohort_id": "cohort:eb8e8954-3542-42df-8c1a-1972d0691a79",
        "cohort_name": "IDC ct_lymph_nodes mediastinum",
        "n_cases": 90,
        "dice_mean": 0.46,
        "dice_p25": 0.27,
        "dice_p50": 0.48,
        "dice_p75": 0.74,
        "sensitivity_mean": 0.51,
        "precision_mean": 0.59,
        "predict_seconds_mean": 287.0,
        "sample_pngs": [
          { "case_id": "MED_LYMPH_009", "kind": "clean_win",
            "url": ".../mediastinal-v1-1.0.0/accurate/MED_LYMPH_009.png" },
          { "case_id": "MED_LYMPH_012", "kind": "calibration_rescue",
            "url": ".../mediastinal-v1-1.0.0/accurate/MED_LYMPH_012.png" },
          { "case_id": "MED_LYMPH_010", "kind": "ood_failure",
            "url": ".../mediastinal-v1-1.0.0/accurate/MED_LYMPH_010.png" }
        ]
      }
    },
    {
      "id": "fast",
      "display_name": "Fast",
      "summary": "Single fold (best val Dice), fp16, no TTA, tile step 0.7.",
      "folds": [3],
      "dtype": "float16",
      "use_mirroring": false,
      "tile_step_size": 0.7,
      "weights_assets": [{ "...": "..." }],
      "size_bytes_total": 408000000,
      "eval": { "...": "..." }
    }
  ]
}
```

Backwards compatibility:

- A model entry with no `variants` array behaves like today — the
  existing single-bundle install code path keeps working unchanged.
- A model entry *with* `variants` ignores its top-level `weights_assets`.
  Existing consumers that don't yet know about variants pick
  `default_variant` and read the variant's assets through the new code
  path.

## `publish-model.py` changes

The current invocation produces one bundle. New flag:

```
bin/publish-model.py \
    --model-generation-id modelgeneration:b40f7c1c-... \
    --source-path /tmp/lnq-publish-src/mediastinal-v1 \
    --name mediastinal-v1 --version 1.0.0 \
    --display-name "LNQ-mediastinal v1" \
    --license "CC BY-NC 4.0" \
    --variant accurate          # NEW: accurate | fast | path/to/variant.yaml
```

What each invocation produces:

- `accurate` — current behaviour: bundles all 5 folds at fp32, sets
  `use_mirroring=true`, `tile_step_size=0.5` in the variant metadata.
- `fast` — picks one fold (default: highest val Dice from `ModelGeneration`
  notes; override with `--fold N`), re-saves checkpoint in fp16,
  sets `use_mirroring=false`, `tile_step_size=0.7`. Bundle is ~10× smaller.
- Future variants can be expressed as a YAML config and passed via
  `--variant /path/to/profile.yaml`, so adding `tensorrt-int8` later is
  a config change, not a code change.

The eval step runs after the variant is minted:

```
bin/eval-variant.py \
    --bundle /tmp/lnq-publish/mediastinal-v1-accurate-1.0.0/ \
    --eval-cohort cohort:eb8e8954-...
```

It reuses the [`bin/idc-batch-qc.py`](idc-batch-qc.py) code path:
loads each case from the cohort, runs the variant's bundled
`lnq-segmenter predict --probability-output`, computes Dice etc., and
renders the per-case PNGs. Output is a JSON blob with the summary
stats + sample PNG URLs, which `publish-model.py` slots into the
variant's `eval` block in the registry entry.

The eval cohort itself is deferred — the spec accepts any
`cohort_id`; pick one when we wire it up. NIH `ct_lymph_nodes` (which
we just ingested as a chronicle Cohort) is the obvious candidate for
mediastinal + abdominopelvic; the other anatomies will need a
different OOD source eventually.

## LNQStudio → Deploy tab (publisher view)

The Deploy tab is the publisher-side surface. Today it's a placeholder
in [`LNQStudio.py`](../LNQStudio/LNQStudio.py); this is its first
concrete piece.

```
+-------------------------------------------------------------------+
| Publish a new release                                             |
|                                                                   |
| Model:        mediastinal-v1  [from chronicle ModelGeneration ▼]  |
| Source path:  /media/share/.../nnUNetTrainer__nnUNet...           |
| Display name: LNQ-mediastinal v1                                  |
| Version:      [1.0.1]                                             |
| License:      [CC BY-NC 4.0 ▼]                                    |
|                                                                   |
| Eval cohort:  IDC ct_lymph_nodes mediastinum (90 cases)  [▼]      |
|                                                                   |
| Variants to mint:                                                 |
|   [✓] accurate   (5-fold, fp32, TTA)                              |
|   [✓] fast       (1-fold, fp16, no TTA, tile step 0.7)            |
|                                                                   |
| [ Mint + evaluate (~3 hr)  ]    [ Cancel ]                        |
+-------------------------------------------------------------------+
|                                                                   |
| Comparison (refreshes as variants finish)                         |
|                                                                   |
|   variant    size      eval Dice   pred time   GH release         |
|   accurate   4.08 GB   0.46 ±0.27  287 s/case   pending           |
|   fast       0.41 GB   0.43 ±0.28   12 s/case   pending           |
|                                                                   |
|   3 sample cases per variant rendered as thumbnail strip          |
|   (clean_win, calibration_rescue, ood_failure)                    |
|                                                                   |
| [ Push to lnq-segmenter (gh release create + git push registry) ] |
+-------------------------------------------------------------------+
```

Once both variants are minted + evaluated, the publisher clicks
*Push* and the existing `gh release create` + `git push` flow runs
twice (once per variant) under one release tag, then the registry
entry is committed with both variants populated.

## LymphNodeQuantifier → install panel (consumer view)

The current install panel ([`LymphNodeQuantifier.py`](../LymphNodeQuantifier/LymphNodeQuantifier.py))
shows one checkable row per *anatomy* (inguinal, abdominopelvic,
mediastinal, axillary). Each row will gain a small variant selector
on the right.

```
[✓] LNQ-mediastinal v1  — mediastinal-v1@1.0.0  (4 GB)   [ Accurate ▼ ]
    Accurate   · 4.08 GB · Dice 0.46 ±0.27 · 287 s/case · [sample]
    Fast       · 0.41 GB · Dice 0.43 ±0.28 ·  12 s/case · [sample]
```

Defaults:

- First-time selection defaults to `default_variant` from the registry
  (which is `accurate` for v1.0.0).
- The previously-selected variant is sticky per (anatomy, machine) in
  QSettings so reinstalls keep the user's preference.
- Hovering the `[sample]` link opens a small popup with the three
  sample PNGs side by side. Each PNG carries its `kind` label
  (clean_win / calibration_rescue / ood_failure) so the user can see
  at a glance "this variant agrees with the accurate model on the
  clean case, slightly disagrees on the rescue case, and matches the
  failure mode."

The composite-SegmentationNode workflow downstream of install is
unchanged — the user's variant choice just changes which checkpoint
the predict subprocess loads. The model output is still a SEG NRRD +
optional probability NRRD with the same geometry.

## Why this matters beyond cost

Two longer-term reasons to invest in the variants infra now, not just
"smaller download":

1. **Honest per-deployment tradeoff disclosure.** Reviewers and
   external integrators get to see, with their own eyes, what
   "0.03 Dice less for 10× faster" actually looks like on three
   representative cases before they pick. We don't have to argue the
   tradeoff with words.
2. **Calibration-rescue cases get a permanent home.** The MED_LYMPH_012
   archetype — "Dice doubles when you re-threshold to 0.001" — is
   exactly the kind of result we want a deploying team to *see*, not
   just read about. Surfacing it as one of the three canonical
   `sample_pngs` per variant turns the calibration story into a
   feature rather than a footnote.

## What gets built

| step | effort | deliverable |
|---|---|---|
| 1 | s | registry schema: optional `variants: [...]`, `default_variant`; back-compat path in `lnq-segmenter` consumer code |
| 2 | m | `publish-model.py --variant` + a `fast.yaml` profile config |
| 3 | s | `bin/eval-variant.py` that wraps `idc-batch-qc.py` and emits the variant's `eval` block |
| 4 | m | LNQStudio Deploy tab UI (publisher view) |
| 5 | s | LymphNodeQuantifier install row: variant selector + sample-PNG popover |
| 6 | s | Pick + version the first eval cohort; populate the `eval` block for mediastinal-v1 / abdominopelvic-v1 against it |

Steps 1-3 are the foundation — they unblock everything else and stay
useful even if the Deploy tab UI gets deferred. 4-5 are the user-facing
surfaces. 6 is the data + decision step we deferred above.

Total before users touch it: roughly 1-2 weeks for one person, of
which the eval-cohort compute (Step 6) dominates.

## Out of scope for v1

- More than two variants per model. Easy to add later; the schema
  already accepts an arbitrary list.
- TensorRT / ONNX export paths in the variant pipeline. The `fast`
  variant defined here stays in PyTorch land; TensorRT integration
  is a future variant kind.
- Per-deployment evaluation (the integrator running the eval against
  their own cohort to see how the model does on their data). Useful
  but a separate workflow; can build on this infra later.
