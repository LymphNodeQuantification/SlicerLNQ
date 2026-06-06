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
+-----------------------------------+-----------------------+
|                                   |  Case 42 of 200       |
|                                   |  Patient MED_LYMPH_021|
|                                   |  Mediastinal          |
|        Red slice                  |                       |
|     (CT + Inferno overlay         |  Threshold:  [====]   |
|      + composite SEG outline      |  0.001                |
|      + reference GT outline)      |                       |
|                                   |  Tools:               |
|                                   |   [+] Positive        |
|                                   |   [-] Negative        |
|                                   |   [○] Sphere paint    |
|                                   |   [   ] Auto threshold|
|                                   |                       |
|                                   |  [  Save & next  ]    |
|                                   |  [  Skip          ]   |
|                                   |  [  Escalate      ]   |
+-----------------------------------+-----------------------+
                              [scrub]
```

Hidden by default: the module selector, the Data tree, the Module
panel header, the Help & Acknowledgement collapser, the Reload & Test
section, the Python console, the 3D view (until the reviewer toggles
it), the slice intersection lines, the orientation widget. Visible:
one slice view + the Review-tab control surface.

Keyboard shortcuts (the difference between cycle time = 30 s and cycle
time = 3 s):

| key | action |
|---|---|
| `j` / `k` | scrub slice down / up |
| `1` | positive-click tool |
| `2` | negative-click tool |
| `3` | sphere brush |
| `a` | "auto threshold from prompts" |
| `[` / `]` | step threshold down / up (log-scaled) |
| `enter` | Save & next |
| `s` | Skip (defer to later in queue) |
| `e` | Escalate (flag for a second reviewer) |
| `u` | Undo last action |

These are wired through Slicer's `qt.QShortcut` against the main
window — works in a remote-desktop frame because key events get
forwarded by the streaming layer.

## Case-flow loop

Cases live in chronicle as already-defined `Case` docs. The new
documents we need:

- **`ReviewQueue`** — a named list of `case_id`s assigned to a project
  (e.g. "ct_lymph_nodes mediastinal review batch 1"). One queue per
  batch.
- **`ReviewAssignment`** — one per (queue, reviewer) pair. Tracks
  which cases this reviewer has opened, saved, skipped, or escalated.
  Carries `cursor` for "where I am in the queue" so a Save & next
  picks up correctly on a page refresh.
- **`Annotation`** (existing type, gain a `producer.kind == "review"`
  value) — the saved corrected mask + the prompt set + the threshold
  the reviewer settled on + the model_generation_id that seeded the
  review. Already designed in review-tab.md.

API surface on the Chronicle backend (all over the existing CouchDB
HTTP, so no new server process):

| endpoint | purpose |
|---|---|
| `GET /lnq/_design/review/_view/queue?reviewer=...` | list queues this reviewer can claim |
| `GET /lnq/_design/review/_view/next?queue=...&reviewer=...` | next un-reviewed case in queue (advances cursor on Save) |
| `PUT /lnq/annotation:<uuid>` | save the corrected mask + prompts |
| `PUT /lnq/reviewassignment:<id>` | update cursor / skip / escalate |

The Review tab's Save button does a single PUT; the "& next" half is a
follow-up GET that returns the case_id of the next assignment. The
Slicer-side code then asks dicomweb-server for the CT + SEG and asks
the Chronicle for the pre-computed probability map blob.

## The pre-compute pipeline (sketch — details deferred)

When a case enters the queue, a worker pre-runs `lnq-segmenter` for
the queue's anatomy on the case CT and writes the SEG + probability
NRRD into the blob store. The reviewer hits a hot cache. We will
flesh out the exact worker shape (cron, queue table, ephemeral Js2
instance) when we get there.

Two principles we should hold:

- **The reviewer should never wait for inference.** First paint < 1 s
  after Save & next or first sign-in. Anything slower kills throughput.
- **Pre-compute is idempotent.** A case that re-enters a new queue
  (different anatomy, different reviewer batch) finds existing model
  outputs by `(case_id, model_generation_id)` hash and skips
  recomputing.

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
