# LNQStudio Review tab — design spec

Status: spec only; not yet implemented (as of `lnq-segmenter` v0.2.3).

## Why this exists

The four phase-2 models (inguinal-v1, abdominopelvic-v1, axillary-v1,
mediastinal-v1) generalize unevenly to out-of-distribution data. On the
NIH CT Lymph Node collection case MED_LYMPH_021, `mediastinal-v1` hits
Dice 0.45 against the radiologist ground truth — but precision is 0.88
and sensitivity is 0.31. The model is *conservative* on new data, not
wrong: it confidently catches obvious enlarged nodes and skips smaller
or borderline ones the NIH annotators included.

We have two annotation sources of different quality:

- **Tagwa Idris-curated LNQ phase-2** — the model's training set; tight
  inclusion criteria, consistent across cases.
- **NIH/TCIA CT Lymph Node** — broader inclusion (smaller, borderline
  nodes), but inter-annotator variance is visible to expert eyes.

We want a workflow where one of our experts can spend a few minutes per
case, *guided by both the model's probability map and the NIH ground
truth*, to produce a corrected mask that genuinely improves model
generalizability. Crucially, the corrected mask must carry provenance
so we can run A/B comparisons of model variants trained on different
mixtures of the source datasets.

This tab is where that workflow lives.

## Inputs the tab consumes

Per case the tab needs four pieces of data, all already producible by
the rest of the system:

1. **Source CT** — already loaded via the IDC Browser or the existing
   Cohorts tab.
2. **Reference mask** — radiologist GT from IDC's DICOM SEG, *or* a
   prior corrected mask from chronicle if one exists for this case.
3. **Model SEG output** — produced by LymphNodeQuantifier; lands as
   one segment in the composite `vtkMRMLSegmentationNode`.
4. **Probability map** — produced by `lnq-segmenter` v0.2.3's
   `--probability-output` flag, loaded as a scalar volume with an
   Inferno colormap clamped to [0, 1].

## Empirical priors (from MED_LYMPH_021 inspection)

The first probability-map study (mediastinal-v1 vs the NIH GT on
MED_LYMPH_021) found the model output is sharply **bimodal**:

| population | n voxels | mean p | median p | % with p ≥ 0.3 |
|---|---:|---:|---:|---:|
| true positives | 36,850 | 0.91 | 0.98 | 100 % |
| false positives | 5,197 | 0.91 | 0.98 | 100 % |
| **missed (GT only)** | **83,360** | **0.045** | **0.003** | **4.1 %** |
| background | 14.5 M | 4 × 10⁻⁵ | ~0 | ~0 % |

What this tells the tab design: on out-of-distribution NIH-style cases,
**threshold-grow at p ≥ 0.3 only recovers ~4 % of the missed nodes.**
The model isn't "almost seeing" the missed regions; for the median
missed voxel it's claiming p = 0.003, indistinguishable from
background. Probability-seeded grow is therefore *not the primary
correction mechanism* on these cases — it's a fast-path for the small
slice of nodes where confidence is borderline. The main mechanism is
classic paint, with the probability overlay as a *guide* (so the
reviewer can spot at a glance "the model didn't even consider this
region a candidate" and head straight there).

## Core interaction: probability-seeded confirm + delete

The expert opens the tab and sees the CT with three overlays:

- model SEG (filled, anatomy color)
- reference GT (outline only, red)
- probability map (foreground color, opacity scaled)

The model SEG is editable; the GT and probability are read-only. Two
tools work on the model SEG:

### "Confirm a missed node" (Add tool)

- Mouse cursor: crosshair with a `+` glyph.
- Click on a pixel that the reference GT covers but the model SEG
  doesn't.
- Behavior depends on what the probability map shows at the click:
  1. If a connected component of voxels with p ≥ threshold (default
     0.3, sticky per session) intersects the click, accept that whole
     component as a new region of the model SEG segment — single
     click, no painting. This is the fast path for the small slice
     of cases where the model is "hesitant".
  2. Otherwise (the empirically dominant case on NIH-style data: the
     model gave p ≈ 0.003 in the missed region), drop into a small
     sphere brush primed at the click location. The expert paints
     the node by hand, but the Inferno overlay is still visible so
     the borders of the GT outline guide them.
- Why the dual mode: the MED_LYMPH_021 study showed only ~4 % of
  missed voxels were above p ≥ 0.3, so the seed-grow path is the
  exception, not the rule. The fallback paint with the GT outline as
  a guide is what actually does the work on cases like these.

### "Remove a false positive" (Delete tool)

- Mouse cursor: crosshair with a `−` glyph.
- Click anywhere inside an over-predicted region.
- Action: delete the connected component of the model SEG segment
  containing the click point.

### Confidence threshold slider

- Range 0.05 → 0.95, default 0.3.
- Live-updates the Inferno overlay's lower threshold so the expert can
  see what would-be-added regions look like at different cutoffs.
- Sticky per session (QSettings), per-anatomy if we want to fine-tune
  later.

## Save behavior

Save writes:

1. A **new `Annotation` chronicle doc** linked to:
   - `case_id`
   - `source_model_generation_id` (the model that produced the seed)
   - `reference_annotation_id` (the NIH GT or whatever was on screen)
   - `reviewer` (current LNQStudio user)
   - `producer: {kind: "expert-corrected", label: "review", ...}`
   - `click_count`, `tool_breakdown` (added vs. deleted),
     `threshold_used`
2. A NRRD seg file uploaded to the same blob store the original
   Annotations live in, addressed by the new Annotation's `seg_ref`.
3. A pointer back to the model run that seeded the review, so future
   queries can answer "what did corrections look like for inference
   produced by ModelGeneration X?"

The chronicle schema for the new Annotation type fields lives in
`SlicerLNQ-Chronicler/schemas/annotation.schema.json` and may need a
small additive field (`review_seed_ref`); design that PR alongside the
implementation.

## Training feedback loop

A corrected Annotation has `producer.label == "review"` and
`producer.kind == "expert-corrected"`. The training set selector in the
Train tab gains a `Include expert-corrected reviews` checkbox so we can
run side-by-side trainings of:

- baseline (Tagwa-only)
- baseline + corrected NIH reviews
- baseline + raw NIH (no expert pass)

…and compare their generalization to a held-out OOD set. This is the
whole point: the corrections only matter if we can prove they help.

## What gets built

| step | effort | deliverable |
|---|---|---|
| 1 | s | wire the Review tab into LNQStudio.py (currently a placeholder) |
| 2 | m | reference-GT loader: DICOM SEG via pydicom-seg fallback (QR not always present) |
| 3 | m | probability-map seed grow (connected-component above threshold + click point) |
| 4 | s | Add/Delete tool buttons + cursor glyphs |
| 5 | s | confidence-threshold slider with live Inferno re-window |
| 6 | m | Save → chronicle Annotation + blob upload |
| 7 | s | Train tab: "include expert-corrected reviews" checkbox |

Total: ~1–2 weeks for one person.

## Why these reviews matter for the next training round

The MED_LYMPH_021 numbers above frame the value proposition: the model
is *confidently wrong* on the regions it misses (p ≈ 0.003), not
borderline. That makes these the strongest possible kind of hard
negative — every corrected case directly contradicts a high-confidence
prediction the model would have made on similar future cases. Adding
them to the training set should push generalization much harder than
another batch of in-distribution Tagwa cases would. The Train tab's
"Include expert-corrected reviews" toggle is what lets us actually
measure that with controlled A/B runs.

## Out of scope for v1

- Multi-segment editing per case (one anatomy at a time keeps the UI
  simple and matches how the per-anatomy models actually run).
- Inter-reviewer agreement metrics — useful but a separate Review
  tab feature for later.
- Dictation / structured reports — radiologist-grade reporting is a
  bigger surface and isn't the differentiator here.

## Open questions

- Threshold-grow's connected-component definition: 6-connected,
  26-connected, or volume-bounded BFS? 26-connected matches what
  `scipy.ndimage.label` does by default and feels closest to what an
  expert means by "this whole node."
- Should we also let the expert *contract* a connected component (set
  threshold higher just for that node) so an over-segmented LN can be
  trimmed without painting? Probably yes; same mechanism, different
  cursor.
- Storage of the probability map itself: keep it as a temp file the
  Review tab consumes and throws away, or persist alongside each
  prediction in chronicle? Probably temp-only — the map is reproducible
  from the model + CT and uncompressed it's ~50 MB per case.
