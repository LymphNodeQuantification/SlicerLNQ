"""LNQReview — single-case expert review experience inside Slicer.

The bigger context for this module is laid out in
[docs/review-tab.md](../docs/review-tab.md) and
[docs/review-deployment.md](../docs/review-deployment.md). In short:
a reviewer signs in via a remote-desktop frame, sees a CT with the
model's prediction already overlaid, places a handful of positive and
negative point prompts, tunes the threshold (or hits "Auto from
prompts"), optionally edits with sphere paint / erase, and clicks
Save & next. Cycle time per easy case should be a few seconds.

Two principles drive the UI:

* Every action is reachable by mouse — buttons, sliders, free-text
  fields. No "hidden behind a keyboard shortcut" features.
* Every keyboard shortcut is visible inside its button's label so the
  reviewer learns by using. After a session they're flying on keys;
  from the first click everything just works.

This first iteration ships the chrome-hiding layout, the right-side
tool panel with all the spec'd buttons + sliders + notes field, the
positive/negative prompt tools wired against Markups fiducials, the
log-scaled threshold slider tied to the probability volume's display,
and a "Load demo" button that pulls the smoke-tested MED_LYMPH_001
case from chronicle so the experience is testable end-to-end before
the queue/assignment plumbing lands."""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import qt
import slicer
import vtk
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModule,
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleTest,
    ScriptedLoadableModuleWidget,
)


# CT window/level presets (Hounsfield units), ordered by clinical frequency
# for our use case. (label, window, level)
CT_PRESETS = [
    ("Default (auto)",  None, None),
    ("CT-Abdomen",      350,   40),
    ("CT-Mediastinum",  350,   50),
    ("CT-Chest",        400,   40),
    ("CT-Lung",         1500, -600),
    ("CT-Bone",         1500,  300),
]

# Threshold-preset buttons next to the log-scaled slider; values chosen to
# span the empirically interesting range (1e-3 is the calibrated cutoff,
# 0.3 the conservative bump, 0.5 the argmax).
THRESHOLD_PRESETS = [0.001, 0.01, 0.1, 0.3, 0.5]


# =============================================================================
# Module
# =============================================================================

class LNQReview(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        parent.title = "LNQ Review"
        parent.categories = ["LNQ"]
        parent.dependencies = []
        parent.contributors = ["Steve Pieper (Isomics)"]
        parent.helpText = (
            "Focused single-case lymph-node review experience. Designed to "
            "be delivered over a remote-desktop frame so radiologists can "
            "review a queue of cases without installing Slicer locally. "
            "See docs/review-tab.md and docs/review-deployment.md in the "
            "SlicerLNQ repo for the broader design.")
        parent.acknowledgementText = (
            "Part of the SlicerLNQ project; see https://lnqproject.org.")


# =============================================================================
# Logic
# =============================================================================

class LNQReviewLogic(ScriptedLoadableModuleLogic):
    """Stateless helpers: scene plumbing, demo case loader."""

    @staticmethod
    def demoCasePaths():
        """Hard-coded local paths to the smoke-tested IDC case the
        `ingest-idc-cohort.py` smoke test left on Manila. Returned for
        the Load demo button so the UI is testable end-to-end without
        the queue plumbing being in place yet."""
        root = "/media/share/LNQ-data/idc/ct_lymph_nodes/nrrd"
        return {
            "ct":   os.path.join(root, "MED_LYMPH_001_0000.nrrd"),
            "gt":   os.path.join(root, "MED_LYMPH_001.nrrd"),
            "model_seg":  ("/media/share/LNQ-data/idc/ct_lymph_nodes/"
                            "predictions/mediastinal-v1/MED_LYMPH_001.nrrd"),
            "model_prob": ("/media/share/LNQ-data/idc/ct_lymph_nodes/"
                            "predictions/mediastinal-v1/MED_LYMPH_001-prob.nrrd"),
        }

    @staticmethod
    def loadCase(paths):
        """Load CT + GT + model SEG + probability map into the scene.
        Returns a dict of created nodes."""
        out = {"ct": None, "gt": None, "model_seg": None, "model_prob": None}
        if paths.get("ct") and os.path.isfile(paths["ct"]):
            out["ct"] = slicer.util.loadVolume(paths["ct"])
            if out["ct"] is not None:
                out["ct"].SetName("LNQReview:CT")
        if paths.get("gt") and os.path.isfile(paths["gt"]):
            out["gt"] = slicer.util.loadSegmentation(paths["gt"])
            if out["gt"] is not None:
                out["gt"].SetName("LNQReview:GT")
                seg = out["gt"].GetSegmentation()
                if seg.GetNumberOfSegments():
                    sid = seg.GetNthSegmentID(0)
                    s = seg.GetSegment(sid)
                    s.SetName("Ground truth")
                    # Pure bright red so the outline reads against the
                    # darker CT background of mediastinal scans.
                    s.SetColor(1.0, 0.0, 0.0)
                disp = out["gt"].GetDisplayNode()
                if disp is not None:
                    # Outline-only for the GT so it doesn't obscure the
                    # model SEG that sits underneath. Touch of fill at
                    # very low alpha gives the outline a more saturated
                    # halo without making it look like a separate region.
                    disp.SetVisibility2DFill(True)
                    disp.SetOpacity2DFill(0.15)
                    disp.SetVisibility2DOutline(True)
                    disp.SetOpacity2DOutline(1.0)
        if paths.get("model_seg") and os.path.isfile(paths["model_seg"]):
            out["model_seg"] = slicer.util.loadSegmentation(paths["model_seg"])
            if out["model_seg"] is not None:
                out["model_seg"].SetName("LNQReview:model")
                seg = out["model_seg"].GetSegmentation()
                if seg.GetNumberOfSegments():
                    sid = seg.GetNthSegmentID(0)
                    s = seg.GetSegment(sid)
                    s.SetName("Mediastinal LNs (model)")
                    s.SetColor(0.78, 0.39, 0.90)
                disp = out["model_seg"].GetDisplayNode()
                if disp is not None:
                    disp.SetVisibility2DFill(True)
                    disp.SetOpacity2DFill(0.35)
                    disp.SetVisibility2DOutline(True)
        if paths.get("model_prob") and os.path.isfile(paths["model_prob"]):
            out["model_prob"] = slicer.util.loadVolume(
                paths["model_prob"], properties={"show": False})
            if out["model_prob"] is not None:
                out["model_prob"].SetName("LNQReview:probability")
        return out

    @staticmethod
    def setupSliceViewOverlay(ct_node, prob_node, segmentation_nodes):
        """Wire the Red slice view: CT as background, probability as
        foreground (Inferno colormap), segmentations layered on top."""
        layoutManager = slicer.app.layoutManager()
        for color in ("Red",):
            sw = layoutManager.sliceWidget(color)
            if sw is None:
                continue
            cn = sw.sliceLogic().GetSliceCompositeNode()
            if ct_node is not None:
                cn.SetBackgroundVolumeID(ct_node.GetID())
            if prob_node is not None:
                cn.SetForegroundVolumeID(prob_node.GetID())
                cn.SetForegroundOpacity(0.55)
            sw.sliceLogic().FitSliceToAll()
        # Inferno colormap on the probability volume.
        if prob_node is not None:
            disp = prob_node.GetDisplayNode()
            if disp is not None:
                heat = (slicer.util.getFirstNodeByName("Inferno")
                        or slicer.util.getFirstNodeByName("FullRainbow"))
                if heat is not None:
                    disp.SetAndObserveColorNodeID(heat.GetID())


# =============================================================================
# Widget
# =============================================================================

class LNQReviewWidget(ScriptedLoadableModuleWidget):

    REJECT_REASONS = ["motion", "contrast bolus", "missing slices",
                      "slice thickness", "other"]

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        self.logic = LNQReviewLogic()
        self._chromeState = None
        self._sceneNodes = {}
        self._prompts = None       # PromptToolController
        self._threshold = None     # ThresholdController
        self._shortcuts = []       # qt.QShortcut handles, kept alive
        # Cohort browsing lives in the LNQ Worklist (see LNQStudio
        # WorklistWindow → "Inference Review" tab). This module is the
        # focused single-case interface that the worklist activates.
        self._currentCohortRoot = ""
        self._currentCohortModel = ""
        self._currentCaseId = ""
        self._lastAutoThreshold = None       # remembered for the Reset button

    # ----- module entry / exit -----

    # While we're iterating on the review experience, keep Slicer's normal
    # chrome (toolbars, 3D view, data probe, console) visible. Flip this
    # to True before going to production so reviewers get the focused
    # single-slice layout the deployment spec calls for.
    HIDE_CHROME_ON_ENTER = False

    def enter(self):
        """Slicer calls enter() each time the user switches into this
        module. With HIDE_CHROME_ON_ENTER=True we capture a restore
        point + collapse to the single-slice layout."""
        if self.HIDE_CHROME_ON_ENTER:
            from LNQReviewLib import layout
            if self._chromeState is None:
                self._chromeState = layout.hideChrome()
        self._installShortcuts()

    def exit(self):
        """Restore the user's normal Slicer chrome on the way out."""
        if self._chromeState is not None:
            from LNQReviewLib import layout
            layout.restoreChrome(self._chromeState)
            self._chromeState = None
        self._uninstallShortcuts()

    # ----- UI construction -----

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)

        # Lazily-imported tools controller (Slicer scene must exist).
        from LNQReviewLib.tools import PromptToolController, ThresholdController
        self._prompts = PromptToolController()
        self._threshold = ThresholdController()

        # Single-case tool surface. The reviewer arrives here from the
        # LNQ Worklist's Inference Review tab via loadFromCohort().
        self.layout.addWidget(self._buildCaseHeader())
        self.layout.addWidget(self._buildThresholdSection())
        self.layout.addWidget(self._buildDisplaySection())
        self.layout.addWidget(self._buildToolsSection())
        self.layout.addWidget(self._buildNotesSection())
        self.layout.addWidget(self._buildActionsSection())
        self.layout.addWidget(self._buildScrubHintLabel())

        self.layout.addStretch(1)
        self._refreshButtonStates()
        self._refreshAutoResetButton()

    def _buildCaseHeader(self):
        box = qt.QGroupBox("Case")
        v = qt.QVBoxLayout(box)

        # Back-to-list affordance — the cohort browser is "home."
        self._backToListButton = qt.QPushButton("← Back to cohort list")
        self._backToListButton.setStyleSheet(
            "text-align: left; padding: 4px; color: #246;")
        self._backToListButton.setFlat(True)
        self._backToListButton.connect("clicked()", self._onBackToList)
        v.addWidget(self._backToListButton)

        self._caseProgressLabel = qt.QLabel("(no case loaded)")
        self._caseProgressLabel.setStyleSheet("font-weight: bold;")
        self._patientLabel = qt.QLabel("")
        self._anatomyLabel = qt.QLabel("")
        for w in (self._patientLabel, self._anatomyLabel):
            w.setStyleSheet("color: gray;")
        v.addWidget(self._caseProgressLabel)
        v.addWidget(self._patientLabel)
        v.addWidget(self._anatomyLabel)
        return box

    def _buildThresholdSection(self):
        box = qt.QGroupBox("Threshold (log-scaled)")
        v = qt.QVBoxLayout(box)
        from LNQReviewLib.tools import ThresholdController

        row = qt.QHBoxLayout()
        self._thresholdSlider = qt.QSlider(qt.Qt.Horizontal)
        self._thresholdSlider.setMinimum(0)
        self._thresholdSlider.setMaximum(ThresholdController.SLIDER_TICKS)
        self._thresholdSlider.setValue(
            ThresholdController.thresholdToSlider(self._threshold.threshold))
        self._thresholdSlider.connect("valueChanged(int)",
                                       self._onThresholdSliderChanged)
        self._thresholdValueLabel = qt.QLabel(f"p ≥ {self._threshold.threshold:.4g}")
        self._thresholdValueLabel.setMinimumWidth(95)
        row.addWidget(self._thresholdSlider, 1)
        row.addWidget(self._thresholdValueLabel)
        v.addLayout(row)

        self._autoThresholdButton = qt.QPushButton("Auto from prompts (a)")
        self._autoThresholdButton.setToolTip(
            "Sweep the threshold and pick the one that best separates the "
            "positive and negative prompt points by balanced accuracy.")
        self._autoThresholdButton.connect("clicked()", self._onAutoThreshold)
        v.addWidget(self._autoThresholdButton)

        self._resetAutoButton = qt.QPushButton("Reset to auto")
        self._resetAutoButton.setToolTip(
            "Snap the slider back to the threshold the last Auto-from-prompts "
            "sweep picked. Useful after eyeballing a few manual values.")
        self._resetAutoButton.setEnabled(False)
        self._resetAutoButton.connect("clicked()", self._onResetAutoThreshold)
        v.addWidget(self._resetAutoButton)

        # Quick-jump preset row. Each button snaps the log-scaled slider
        # to a frequently-useful value; 0.3 is the conservative bump
        # cited in the deploy-tab spec and 0.5 mirrors the argmax cutoff.
        presetRow = qt.QHBoxLayout()
        presetRow.addWidget(qt.QLabel("Presets:"))
        self._thresholdPresetButtons = []
        for p in THRESHOLD_PRESETS:
            btn = qt.QPushButton(f"{p:g}")
            btn.setMaximumWidth(54)
            btn.setToolTip(f"Snap threshold to p ≥ {p}")
            btn.connect("clicked()",
                        lambda p=p: self._setThresholdValue(p, source="preset"))
            presetRow.addWidget(btn)
            self._thresholdPresetButtons.append(btn)
        presetRow.addStretch(1)
        v.addLayout(presetRow)
        return box

    def _buildDisplaySection(self):
        """Per-case display controls: CT window/level preset + overlay /
        GT-curve visibility toggles. Lives between Threshold (which
        drives the heatmap) and Tools (which drive segmentation
        edits) so the reviewer's display knobs are colocated."""
        box = qt.QGroupBox("Display")
        v = qt.QVBoxLayout(box)

        # CT preset dropdown. Default keeps the loader's auto W/L.
        presetRow = qt.QHBoxLayout()
        presetRow.addWidget(qt.QLabel("CT preset:"))
        self._ctPresetCombo = qt.QComboBox()
        for label, _w, _l in CT_PRESETS:
            self._ctPresetCombo.addItem(label)
        # Land on CT-Abdomen by default per reviewer preference.
        defaultIndex = next((i for i, p in enumerate(CT_PRESETS)
                              if p[0] == "CT-Abdomen"), 0)
        self._ctPresetCombo.setCurrentIndex(defaultIndex)
        self._ctPresetCombo.connect("currentIndexChanged(int)",
                                     self._onCTPresetChanged)
        presetRow.addWidget(self._ctPresetCombo, 1)
        v.addLayout(presetRow)

        # Visibility toggles for the three layered renderings. Each button
        # is checkable; checked == "currently hidden". The reviewer uses
        # them to isolate one layer at a time when comparing the model
        # prediction against the GT or the raw CT.
        toggleRow = qt.QHBoxLayout()
        self._gtVisibleButton = qt.QPushButton("Hide GT outline")
        self._gtVisibleButton.setCheckable(True)
        self._gtVisibleButton.setChecked(False)
        self._gtVisibleButton.setToolTip(
            "Toggle the NIH ground-truth outline. Press to compare the "
            "model SEG without the reference visually anchoring you.")
        self._gtVisibleButton.connect("clicked()", self._onToggleGTVisibility)
        self._modelVisibleButton = qt.QPushButton("Hide LNQ prediction")
        self._modelVisibleButton.setCheckable(True)
        self._modelVisibleButton.setChecked(False)
        self._modelVisibleButton.setToolTip(
            "Toggle the LNQ model SEG (thresholded). Hides both fill and "
            "outline so you can read the CT + GT or the probability heatmap "
            "without the prediction overlapping them.")
        self._modelVisibleButton.connect(
            "clicked()", self._onToggleModelVisibility)
        self._overlayVisibleButton = qt.QPushButton("Hide probability overlay")
        self._overlayVisibleButton.setCheckable(True)
        self._overlayVisibleButton.setChecked(False)
        self._overlayVisibleButton.setToolTip(
            "Toggle the Inferno probability heatmap. Hides the foreground "
            "layer so you see just the CT + segmentations.")
        self._overlayVisibleButton.connect(
            "clicked()", self._onToggleOverlayVisibility)
        toggleRow.addWidget(self._gtVisibleButton)
        toggleRow.addWidget(self._modelVisibleButton)
        toggleRow.addWidget(self._overlayVisibleButton)
        v.addLayout(toggleRow)
        return box

    def _buildToolsSection(self):
        box = qt.QGroupBox("Tools")
        v = qt.QVBoxLayout(box)
        self._positiveButton = qt.QPushButton("+ Positive  (1)")
        self._positiveButton.setCheckable(True)
        self._positiveButton.setToolTip(
            "Drop positive prompt points — places where the reviewer is "
            "sure there's a lymph node. Used by Auto from prompts.")
        self._positiveButton.connect("clicked()",
                                      lambda: self._setPromptMode("positive"))
        self._negativeButton = qt.QPushButton("– Negative  (2)")
        self._negativeButton.setCheckable(True)
        self._negativeButton.setToolTip(
            "Drop negative prompt points — places where the reviewer is "
            "sure there is NOT a lymph node.")
        self._negativeButton.connect("clicked()",
                                      lambda: self._setPromptMode("negative"))
        self._spherePaintButton = qt.QPushButton("○ Sphere paint  (3)")
        self._spherePaintButton.setCheckable(True)
        self._spherePaintButton.setToolTip("Paint a small sphere onto the "
                                           "model SEG segment.")
        self._spherePaintButton.connect("clicked()",
                                        lambda: self._setPromptMode("sphere"))
        self._eraseButton = qt.QPushButton("– Erase  (4)")
        self._eraseButton.setCheckable(True)
        self._eraseButton.setToolTip("Erase from the model SEG segment.")
        self._eraseButton.connect("clicked()",
                                  lambda: self._setPromptMode("erase"))
        self._undoButton = qt.QPushButton("↶ Undo  (u)")
        self._undoButton.setToolTip("Undo last action.")
        self._undoButton.connect("clicked()", self._onUndo)
        self._clearPromptsButton = qt.QPushButton("Clear all prompts")
        self._clearPromptsButton.connect("clicked()", self._onClearPrompts)
        for w in (self._positiveButton, self._negativeButton,
                   self._spherePaintButton, self._eraseButton,
                   self._undoButton, self._clearPromptsButton):
            v.addWidget(w)
        return box

    def _buildNotesSection(self):
        box = qt.QGroupBox("Case notes")
        v = qt.QVBoxLayout(box)
        self._notesEdit = qt.QPlainTextEdit()
        self._notesEdit.setPlaceholderText(
            "Free-text notes about this case. Saved on the Annotation "
            "regardless of which exit action you pick.")
        self._notesEdit.setMaximumHeight(80)
        v.addWidget(self._notesEdit)
        return box

    def _buildActionsSection(self):
        box = qt.QGroupBox("Actions")
        v = qt.QVBoxLayout(box)
        # Double-ampersand escapes Qt's mnemonic so users see "&" literally.
        self._saveButton = qt.QPushButton("✓  Save && next  (Enter)")
        self._saveButton.setDefault(True)
        self._saveButton.setStyleSheet("font-weight: bold; padding: 6px;")
        self._saveButton.connect("clicked()", self._onSaveAndNext)
        self._skipButton = qt.QPushButton("Skip  (s)")
        self._skipButton.connect("clicked()", self._onSkip)
        self._rejectButton = qt.QPushButton("⌀  Reject — poor quality  (r)")
        self._rejectButton.connect("clicked()", self._onReject)
        self._escalateButton = qt.QPushButton("⚐  Escalate  (e)")
        self._escalateButton.connect("clicked()", self._onEscalate)
        for w in (self._saveButton, self._skipButton,
                   self._rejectButton, self._escalateButton):
            v.addWidget(w)
        self._actionStatusLabel = qt.QLabel("")
        self._actionStatusLabel.setStyleSheet("color: gray;")
        self._actionStatusLabel.setWordWrap(True)
        v.addWidget(self._actionStatusLabel)
        return box

    def _buildScrubHintLabel(self):
        # Always-visible hint near the bottom so the reviewer notices
        # j / k will move them through slices.
        label = qt.QLabel("j / k — scrub slice down / up")
        label.setStyleSheet("color: gray; padding: 4px;")
        label.setAlignment(qt.Qt.AlignHCenter)
        return label

    # _buildDemoSection removed — the cohort table is the entry point now;
    # double-click on a row loads the case end-to-end.

    # ----- keyboard shortcuts -----

    def _installShortcuts(self):
        if self._shortcuts:
            return
        main = slicer.util.mainWindow()
        if main is None:
            return
        mappings = [
            ("1", lambda: self._setPromptMode("positive")),
            ("2", lambda: self._setPromptMode("negative")),
            ("3", lambda: self._setPromptMode("sphere")),
            ("4", lambda: self._setPromptMode("erase")),
            ("a", self._onAutoThreshold),
            ("u", self._onUndo),
            ("[", lambda: self._stepThreshold(-1)),
            ("]", lambda: self._stepThreshold(+1)),
            ("j", lambda: self._scrubSlice(-1)),
            ("k", lambda: self._scrubSlice(+1)),
            ("s", self._onSkip),
            ("r", self._onReject),
            ("e", self._onEscalate),
            ("Return", self._onSaveAndNext),
            ("Enter",  self._onSaveAndNext),
        ]
        for key, slot in mappings:
            sc = qt.QShortcut(qt.QKeySequence(key), main)
            sc.setContext(qt.Qt.ApplicationShortcut)
            sc.connect("activated()", slot)
            self._shortcuts.append(sc)

    def _uninstallShortcuts(self):
        for sc in self._shortcuts:
            try:
                sc.setParent(None)
            except Exception:
                pass
        self._shortcuts = []

    # ----- state management -----

    def _refreshButtonStates(self):
        mode = self._prompts.activeMode if self._prompts else None
        self._positiveButton.setChecked(mode == "positive")
        self._negativeButton.setChecked(mode == "negative")
        self._spherePaintButton.setChecked(mode == "sphere")
        self._eraseButton.setChecked(mode == "erase")

    # ----- threshold slot wiring -----

    def _onThresholdSliderChanged(self, value):
        from LNQReviewLib.tools import ThresholdController
        threshold = ThresholdController.sliderToThreshold(value)
        self._threshold.setThreshold(threshold)
        self._thresholdValueLabel.setText(f"p ≥ {threshold:.4g}")
        self._refreshAutoResetButton()

    def _stepThreshold(self, direction):
        step = max(1, self._threshold.SLIDER_TICKS // 100)
        self._thresholdSlider.setValue(
            self._thresholdSlider.value + direction * step)

    def _setThresholdValue(self, threshold, source="manual"):
        """Programmatic slider snap (preset buttons, Reset-to-auto).
        Mirrors what dragging the slider would do but skips the
        auto-resetbutton refresh that the dragged path triggers."""
        from LNQReviewLib.tools import ThresholdController
        self._thresholdSlider.setValue(
            ThresholdController.thresholdToSlider(threshold))
        if source == "preset":
            self._actionStatusLabel.setText(
                f"Threshold preset → p ≥ {threshold:g}")

    def _onAutoThreshold(self):
        threshold, diagnostics = self._threshold.tuneToPrompts(
            self._prompts.positive, self._prompts.negative)
        from LNQReviewLib.tools import ThresholdController
        self._thresholdSlider.setValue(
            ThresholdController.thresholdToSlider(threshold))
        self._lastAutoThreshold = threshold
        note = diagnostics.get("note") or ""
        self._actionStatusLabel.setText(
            f"Auto threshold → p ≥ {threshold:.4g} "
            f"(pos={diagnostics['positives']}, neg={diagnostics['negatives']}"
            + (f", balanced acc={diagnostics['objective']}" if diagnostics.get("objective") else "")
            + (f"; {note}" if note else "") + ")")
        self._refreshAutoResetButton()

    def _onResetAutoThreshold(self):
        if self._lastAutoThreshold is None:
            return
        self._setThresholdValue(self._lastAutoThreshold)
        self._actionStatusLabel.setText(
            f"Snap to last auto threshold → p ≥ {self._lastAutoThreshold:.4g}")

    def _refreshAutoResetButton(self):
        if not hasattr(self, "_resetAutoButton"):
            return
        self._resetAutoButton.setEnabled(self._lastAutoThreshold is not None)
        if self._lastAutoThreshold is not None:
            self._resetAutoButton.setText(
                f"Reset to auto (p ≥ {self._lastAutoThreshold:.4g})")
        else:
            self._resetAutoButton.setText("Reset to auto")

    # ----- display knobs -----

    def _onCTPresetChanged(self, idx):
        if idx < 0 or idx >= len(CT_PRESETS):
            return
        label, window, level = CT_PRESETS[idx]
        ct = self._sceneNodes.get("ct") if self._sceneNodes else None
        if ct is None:
            return
        disp = ct.GetDisplayNode()
        if disp is None:
            return
        if window is None or level is None:
            # Restore auto W/L (the loader's default).
            disp.SetAutoWindowLevel(True)
        else:
            disp.SetAutoWindowLevel(False)
            disp.SetWindowLevel(window, level)
        self._actionStatusLabel.setText(
            f"CT W/L preset → {label}"
            + (f" (W={window}, L={level})" if window is not None else ""))

    def _onToggleGTVisibility(self):
        gt = self._sceneNodes.get("gt") if self._sceneNodes else None
        if gt is None:
            return
        disp = gt.GetDisplayNode()
        if disp is None:
            return
        # Button is checkable: checked == "hidden".
        hide = self._gtVisibleButton.isChecked()
        disp.SetVisibility(not hide)
        self._gtVisibleButton.setText(
            "Show GT outline" if hide else "Hide GT outline")

    def _onToggleModelVisibility(self):
        seg = self._sceneNodes.get("model_seg") if self._sceneNodes else None
        if seg is None:
            return
        disp = seg.GetDisplayNode()
        if disp is None:
            return
        hide = self._modelVisibleButton.isChecked()
        disp.SetVisibility(not hide)
        self._modelVisibleButton.setText(
            "Show LNQ prediction" if hide else "Hide LNQ prediction")

    def _onToggleOverlayVisibility(self):
        prob = self._sceneNodes.get("model_prob") if self._sceneNodes else None
        if prob is None:
            return
        # Foreground volume in every slice viewer. Setting opacity to 0
        # is the standard way to suppress the foreground layer without
        # tearing it down.
        hide = self._overlayVisibleButton.isChecked()
        opacity = 0.0 if hide else 0.55
        for c in slicer.app.layoutManager().sliceViewNames():
            cn = slicer.app.layoutManager().sliceWidget(c).sliceLogic().GetSliceCompositeNode()
            cn.SetForegroundOpacity(opacity)
        self._overlayVisibleButton.setText(
            "Show probability overlay" if hide else "Hide probability overlay")

    # ----- prompt tools -----

    def _setPromptMode(self, mode):
        if mode in ("sphere", "erase"):
            # Stubbed; will route to SegmentEditor effects in a later
            # iteration.
            self._actionStatusLabel.setText(
                f"{mode} tool not implemented yet — coming with SegmentEditor "
                f"effect wiring in the next iteration.")
            self._prompts.setMode(None)
            self._refreshButtonStates()
            return
        if self._prompts.activeMode == mode:
            # Toggle off — second click on the same button leaves view mode.
            self._prompts.setMode(None)
        else:
            self._prompts.setMode(mode)
        self._refreshButtonStates()

    def _onClearPrompts(self):
        self._prompts.clearAll()
        self._actionStatusLabel.setText("Cleared all prompts.")

    # ----- slice nav -----

    def _scrubSlice(self, direction):
        red = slicer.app.layoutManager().sliceWidget("Red")
        if red is None:
            return
        sliceLogic = red.sliceLogic()
        offset = sliceLogic.GetSliceOffset()
        # Estimate slice step from the background volume's z-spacing.
        bgID = sliceLogic.GetSliceCompositeNode().GetBackgroundVolumeID()
        step = 1.0
        if bgID:
            bg = slicer.mrmlScene.GetNodeByID(bgID)
            if bg and bg.GetSpacing():
                step = bg.GetSpacing()[2]
        sliceLogic.SetSliceOffset(offset + direction * step)

    # ----- public API called from the LNQ Worklist's Inference Review tab -----

    def loadFromCohort(self, data_root, model_name, case_id):
        """Load the case identified by `case_id` from the standard
        ingest-idc-cohort.py + idc-batch-qc.py directory layout under
        `data_root`. This is the entry point the worklist's
        Inference Review tab calls on row double-click."""
        from LNQReviewLib.cohort_list import derive_case_paths
        paths = derive_case_paths(data_root, model_name, case_id)
        missing = [k for k in ("ct", "model_seg", "model_prob")
                    if paths.get(k) is None]
        if missing:
            slicer.util.errorDisplay(
                f"Case {case_id} is missing required files: "
                + ", ".join(missing)
                + f"\n\nLooked under {data_root}/ with the "
                  f"ingest-idc-cohort.py directory convention.")
            return

        for n in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            slicer.mrmlScene.RemoveNode(n)
        for n in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
            slicer.mrmlScene.RemoveNode(n)
        if self._prompts is not None:
            self._prompts.clearAll()

        load_paths = {"ct": paths["ct"], "gt": paths["gt"],
                       "model_seg": paths["model_seg"],
                       "model_prob": paths["model_prob"]}
        self._sceneNodes = self.logic.loadCase(load_paths)
        self.logic.setupSliceViewOverlay(
            self._sceneNodes["ct"], self._sceneNodes["model_prob"],
            [self._sceneNodes["gt"], self._sceneNodes["model_seg"]])
        if self._sceneNodes["model_prob"]:
            self._threshold.setProbabilityVolume(self._sceneNodes["model_prob"])

        # Apply whatever CT preset the dropdown is currently set to.
        if hasattr(self, "_ctPresetCombo"):
            self._onCTPresetChanged(self._ctPresetCombo.currentIndex)

        # Reset per-case state.
        self._lastAutoThreshold = None
        if hasattr(self, "_gtVisibleButton"):
            self._gtVisibleButton.setChecked(False)
            self._gtVisibleButton.setText("Hide GT outline")
        if hasattr(self, "_modelVisibleButton"):
            self._modelVisibleButton.setChecked(False)
            self._modelVisibleButton.setText("Hide LNQ prediction")
        if hasattr(self, "_overlayVisibleButton"):
            self._overlayVisibleButton.setChecked(False)
            self._overlayVisibleButton.setText("Hide probability overlay")
        self._refreshAutoResetButton()

        self._currentCohortRoot = data_root
        self._currentCohortModel = model_name
        self._currentCaseId = case_id
        self._caseProgressLabel.setText(case_id)
        self._patientLabel.setText(f"Patient: {case_id}")
        self._anatomyLabel.setText(f"Anatomy: {model_name}")
        self._actionStatusLabel.setText(
            "Loaded. Use j/k to scrub slices, threshold slider to tune, "
            "1/2 to drop positive/negative prompts.")

    # ----- view switching -----

    def _onBackToList(self):
        """Send the reviewer back to the worklist so they can pick the
        next case. We don't kill the scene — if the reviewer picks the
        same row again it just re-loads, which is fine; if they pick a
        different one, loadFromCohort() clears the scene first."""
        if self._prompts is not None:
            self._prompts.clearAll()
        # Bring the worklist back forward — it's an independent top-level
        # window so it survives module switches.
        try:
            ls = slicer.modules.lnqstudio.widgetRepresentation().self()
            if hasattr(ls, "setWorklistVisible"):
                ls.setWorklistVisible(True)
        except Exception:
            pass
        slicer.util.selectModule("LNQStudio")

    # ----- case exit actions (stubbed for this iteration) -----

    def _onSaveAndNext(self):
        notes = self._notesEdit.toPlainText().strip()
        n_pos = self._prompts.positive.GetNumberOfControlPoints()
        n_neg = self._prompts.negative.GetNumberOfControlPoints()
        self._actionStatusLabel.setText(
            f"Save & next — would write Annotation "
            f"(prompts: {n_pos}+ / {n_neg}–, threshold={self._threshold.threshold:.4g}, "
            f"notes={'yes' if notes else 'no'}). Chronicle write wires in next.")

    def _onSkip(self):
        self._actionStatusLabel.setText(
            "Skip — would defer case to end of queue. Chronicle wires next.")

    def _onReject(self):
        reason, ok = qt.QInputDialog.getItem(
            self.parent, "Reject — poor quality",
            "Reason this case is unfit for review:",
            self.REJECT_REASONS, 0, True)
        if not ok:
            return
        self._actionStatusLabel.setText(
            f"Reject ({reason}) — would write Annotation with quality_flag="
            f"rejected_poor_quality + quality_reason='{reason}'. Chronicle "
            f"wires next.")

    def _onEscalate(self):
        self._actionStatusLabel.setText(
            "Escalate — would flag for second reviewer. Chronicle wires next.")

    def _onUndo(self):
        # Slicer's central undo manager (if available) handles most edits;
        # for the prompts specifically we'll roll our own when SegmentEditor
        # wiring lands.
        try:
            slicer.mrmlScene.Undo()
            self._actionStatusLabel.setText("Undid last MRML change.")
        except Exception as exc:
            self._actionStatusLabel.setText(f"Undo failed: {exc}")


# =============================================================================
# Test stub
# =============================================================================

class LNQReviewTest(ScriptedLoadableModuleTest):
    def runTest(self):
        self.setUp()
        self.delayDisplay("LNQReview loaded.")
