# LNQStudio Review tab — design spec

Status: spec only; not yet implemented (as of `lnq-segmenter` v0.2.3).

Companion document: [review-deployment.md](review-deployment.md) covers
how this tab gets delivered to reviewers — Slicer over remote desktop
rather than a custom web app. The interaction design specified here is
identical whether the reviewer is running Slicer on their laptop or in
a browser tab pointing at a hosted instance.

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

The probability map on MED_LYMPH_021 (mediastinal-v1 vs NIH GT, default
window) looks bimodal — TPs and FPs sit around p = 0.91, missed voxels
around p = 0.003 — but that picture is misleading. A linear colormap
windowed to [0, 1] flattens everything below ~0.05 into "looks like
background." The structure is at the *low* end, on a log scale.

When you compare missed-GT voxels against true-background voxels at
small thresholds, the missed region is enriched 600–1000× over
background out to extremely low probability cutoffs:

| threshold | missed-GT % above | bg % above | enrichment |
|---:|---:|---:|---:|
| 1 × 10⁻⁴ | 72.5 % | 0.12 % | 600 × |
| 1 × 10⁻³ | 58.7 % | 0.07 % | **836 ×** |
| 5 × 10⁻³ | 44.0 % | 0.05 % | 928 × |
| 1 × 10⁻² | 36.7 % | 0.04 % | 954 × |
| 5 × 10⁻² | 21.6 % | 0.02 % | 1035 × |
| 0.3 | 4.1 % | 4 × 10⁻⁵ | 929 × |

The model is *whispering* about the missed regions — confidence is real
but compressed against zero by the softmax. Re-thresholding the same
probability map at p ≥ 0.001 (instead of the default argmax-equivalent
0.5) on this case lifts Dice 0.45 → **0.78** with precision unchanged:

| threshold | Dice | sensitivity | precision |
|---:|---:|---:|---:|
| 0.001 | **0.775** | 0.714 | 0.848 |
| 0.005 | 0.714 | 0.611 | 0.859 |
| 0.01 | 0.680 | 0.561 | 0.862 |
| 0.1 | 0.556 | 0.408 | 0.872 |
| 0.5 (default) | 0.454 | 0.307 | 0.876 |

What this tells the tab design:

- The seed-grow mechanic absolutely works — but the threshold for
  "where the model is signaling" lives down at **0.001–0.01**, not 0.3.
- The threshold slider should be **log-scaled** (e.g. 10⁻⁵ → 1) so the
  reviewer can actually navigate the working range.
- The Inferno overlay needs to be displayed against a log-scaled
  colormap or a window like [0, 0.05] by default, otherwise the whisper
  is invisible.
- The tab is also where per-case (or per-collection) threshold tuning
  surfaces: a "find best threshold against reference GT" button that
  sweeps the curve above and snaps the slider to the Dice argmax is
  cheap and immediately useful.

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
- Action: grow the model's probability-map connected component above
  the current threshold (default 0.001, log-scaled slider 10⁻⁵ → 1)
  at the click point. The grown component becomes a new region of the
  model SEG segment.
- The default sits orders of magnitude below the argmax cutoff because
  that's where the empirical signal lives — see "Empirical priors"
  above. The slider is log-scaled so the reviewer can move it across
  the working range without spending the whole travel between 0.3
  and 1.0.
- If no probability voxels above threshold connect to the click point
  (the model truly didn't see anything there — rare but possible at
  high thresholds or on hard cases), fall back to a small sphere
  brush primed at the click location.

### "Remove a false positive" (Delete tool)

- Mouse cursor: crosshair with a `−` glyph.
- Click anywhere inside an over-predicted region.
- Action: delete the connected component of the model SEG segment
  containing the click point.

### Case-level outcomes (full design in [review-deployment.md](review-deployment.md#case-level-outcomes))

In addition to per-voxel edits, the reviewer picks one of four
case-level exit actions: Save & next, Skip, **Reject — poor quality**,
or Escalate. Reject prompts a small reason picker (motion, contrast
bolus, missing slices, slice thickness, other-with-text) and the
case's Annotation gets `quality_flag = rejected_poor_quality` plus
the picked reason — keeping it out of any future training set built
from these reviews.

A persistent free-text **case notes** field lives above the action
buttons and is saved on the Annotation regardless of the exit action.
Useful for "ask Tagwa to confirm SVC location", "patient has surgical
clips", flagging interesting cases, anything else.

### Confidence threshold slider

- Log-scaled range 10⁻⁵ → 1, default 0.001.
- Live-updates the Inferno overlay's lower threshold and the Add
  tool's seed-grow cutoff together so what the reviewer can *see* is
  exactly what a click would *accept*.
- Sticky per session (QSettings), per-anatomy if we want to fine-tune
  later.
- "Best vs reference" button — sweeps the threshold curve against the
  loaded reference GT and snaps to argmax Dice. On MED_LYMPH_021 this
  finds ~0.001 and lifts Dice 0.45 → 0.78 with no painting. Great
  starting point before the reviewer manually fine-tunes.

### Point/scribble prompts (a more general way to tune the threshold)

The "Best vs reference" button assumes a reference GT exists. Often it
won't — and even when it does, the reference may be in a different
inclusion regime than what we actually want (NIH includes borderline
nodes our experts wouldn't, our experts include nodes NIH skipped).
Point/scribble prompts solve both:

- Two tools on the toolbar: **Positive (this is LN)** and **Negative
  (this is not LN)**. The reviewer plants a handful of each — single
  clicks become 1-voxel point landmarks, drag becomes a short scribble
  (a thin polyline of points). The interaction model mirrors what
  Segment Editor's "Paint" tool does, just at much sparser sampling.
- The points are stored as `vtkMRMLMarkupsFiducialNode`s named
  `LNQ:review-pos-*` and `LNQ:review-neg-*` so they live in the scene
  next to the probability volume and survive a Save/Load round-trip.
- A **"Tune threshold to prompts"** action then takes the labelled
  points as a tiny ground-truth set and sweeps the threshold to
  maximise an objective the reviewer cares about (default: balanced
  accuracy on the prompt set, optionally Youden's J or weighted-toward-
  recall). The resulting threshold gets snapped onto the slider, the
  Inferno overlay re-windows, and the Add tool's seed-grow cutoff
  updates in lockstep.
- This is the workhorse for cases without a reference: ~30 seconds of
  expert clicking gives the probability map a personally-calibrated
  cutoff, then a few seed-grow clicks finish the segmentation. The
  resulting Annotation carries the prompts (so the calibration is
  auditable) in addition to the corrected mask.

Optional integration path — **nnInteractive / scribble-prompt model**:
the same prompt points can be passed to an external interactive
segmentation network (e.g. nnInteractive, SAM-medical, ScribblePrompt)
running over the CT instead of our anatomy model's probability map.
Output is a candidate segmentation that the reviewer accepts, rejects,
or further corrects. We'd surface this as a separate "Run interactive
model" button rather than mixing it with the threshold tuning so the
two paths stay readable. nnInteractive integration is post-v1 — the
prompt UI itself is reusable across both backends.

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
| 5 | s | confidence-threshold slider (log-scale) with live Inferno re-window |
| 6 | m | positive/negative prompt tools + "Tune threshold to prompts" action |
| 7 | m | Save → chronicle Annotation + blob upload (incl. prompt points for audit) |
| 8 | s | Train tab: "include expert-corrected reviews" checkbox |

Total: ~1–2 weeks for one person.

## Why these reviews matter for the next training round

The MED_LYMPH_021 study reframes the model behavior on OOD cases:
**it's not blind to most missed nodes, it's miscalibrated against
them.** That's actually the easier failure mode to fix — recalibration
techniques (temperature scaling, Platt scaling on a held-out set)
exist and could lift real-world Dice without retraining at all.

But reviews still matter for two reasons:

- **Calibration data needs labels.** Per-collection threshold tuning
  needs reference GT, and corrected reviews are higher-quality GT
  than raw NIH annotations.
- **Hard cases that the model genuinely missed at p ≈ 0** are still
  the strongest training signal. On MED_LYMPH_021 those are a smaller
  fraction (~30 %) of missed-GT voxels than the whisper-but-below-
  threshold majority, but they're the ones where retraining would
  actually teach the model something new.

The Train tab's "Include expert-corrected reviews" toggle is what lets
us do controlled A/B runs to measure both effects.

## Out of scope for v1

- Multi-segment editing per case (one anatomy at a time keeps the UI
  simple and matches how the per-anatomy models actually run).
- Inter-reviewer agreement metrics — useful but a separate Review
  tab feature for later.
- Dictation / structured reports — radiologist-grade reporting is a
  bigger surface and isn't the differentiator here.
- nnInteractive / SAM-medical / ScribblePrompt backends behind the
  prompt UI. The prompt collection itself is in v1 (drives threshold
  tuning); routing prompts to an external interactive-segmentation
  model is a later integration.

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
