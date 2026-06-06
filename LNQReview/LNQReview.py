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
                    s.SetColor(1.0, 0.2, 0.2)
                disp = out["gt"].GetDisplayNode()
                if disp is not None:
                    disp.SetVisibility2DFill(False)
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
        self._cohortSection = None # CohortListSection (table view)
        self._perCaseWidgets = []  # widgets hidden until a case loads

    # ----- module entry / exit -----

    def enter(self):
        """Slicer calls enter() each time the user switches into this
        module. We hide chrome here (capturing a restore-point on the
        widget) and switch to the single-slice layout."""
        from LNQReviewLib import layout
        if self._chromeState is None:
            self._chromeState = layout.hideChrome()
        self._installShortcuts()

    def exit(self):
        """Restore the user's normal Slicer chrome on the way out."""
        from LNQReviewLib import layout
        if self._chromeState is not None:
            layout.restoreChrome(self._chromeState)
            self._chromeState = None
        self._uninstallShortcuts()

    # ----- UI construction -----

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)

        # Lazily-imported tools controller (Slicer scene must exist).
        from LNQReviewLib.tools import PromptToolController, ThresholdController
        from LNQReviewLib.cohort_list import CohortListSection
        self._prompts = PromptToolController()
        self._threshold = ThresholdController()

        # Cohort browser: the reviewer's landing surface. Spreadsheet of
        # every case in the project; double-click loads.
        self._cohortSection = CohortListSection()
        self._cohortSection.caseActivated.connect(self._onCaseFromList)
        cohortBox = qt.QGroupBox("Cohort")
        cohortLay = qt.QVBoxLayout(cohortBox)
        cohortLay.addWidget(self._cohortSection.widget)
        self.layout.addWidget(cohortBox)

        # Per-case widgets — hidden until a case is loaded; double-click
        # in the cohort table switches them on.
        for w in (
            self._buildCaseHeader(),
            self._buildThresholdSection(),
            self._buildToolsSection(),
            self._buildNotesSection(),
            self._buildActionsSection(),
            self._buildScrubHintLabel(),
        ):
            self.layout.addWidget(w)
            self._perCaseWidgets.append(w)
        self._setPerCaseVisible(False)

        self.layout.addStretch(1)
        self._refreshButtonStates()

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

    def _stepThreshold(self, direction):
        step = max(1, self._threshold.SLIDER_TICKS // 100)
        self._thresholdSlider.setValue(
            self._thresholdSlider.value + direction * step)

    def _onAutoThreshold(self):
        threshold, diagnostics = self._threshold.tuneToPrompts(
            self._prompts.positive, self._prompts.negative)
        from LNQReviewLib.tools import ThresholdController
        self._thresholdSlider.setValue(
            ThresholdController.thresholdToSlider(threshold))
        note = diagnostics.get("note") or ""
        self._actionStatusLabel.setText(
            f"Auto threshold → p ≥ {threshold:.4g} "
            f"(pos={diagnostics['positives']}, neg={diagnostics['negatives']}"
            + (f", balanced acc={diagnostics['objective']}" if diagnostics.get("objective") else "")
            + (f"; {note}" if note else "") + ")")

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

    # ----- view switching -----

    def _setPerCaseVisible(self, visible):
        """Toggle the per-case tool surface on/off as a unit so the
        landing experience is just the cohort table."""
        for w in self._perCaseWidgets:
            w.setVisible(visible)

    def _setCohortVisible(self, visible):
        # Walk up to the parent QGroupBox so the table + its surrounding
        # labels all collapse together.
        if self._cohortSection is None:
            return
        host = self._cohortSection.widget
        while host is not None and not isinstance(host, qt.QGroupBox):
            host = host.parent() if hasattr(host, "parent") else None
        if host is not None:
            host.setVisible(visible)

    def _onBackToList(self):
        self._setPerCaseVisible(False)
        self._setCohortVisible(True)
        # Clear the prompts so they don't pollute the next case.
        if self._prompts is not None:
            self._prompts.clearAll()
        # The CT + segmentations stay in the scene; if the reviewer picks
        # the same row they'll be re-loaded via the same paths.

    # ----- load case driven by the cohort table -----

    def _onCaseFromList(self, case_id):
        """The cohort table double-clicked a row. Resolve its paths
        from the same directory convention and load."""
        from LNQReviewLib.cohort_list import derive_case_paths
        paths = derive_case_paths(self._cohortSection.dataRoot,
                                   self._cohortSection.modelName,
                                   case_id)
        missing = [k for k in ("ct", "model_seg", "model_prob")
                    if paths.get(k) is None]
        if missing:
            slicer.util.errorDisplay(
                f"Case {case_id} is missing required files: "
                + ", ".join(missing)
                + f"\n\nLooked under {self._cohortSection.dataRoot}/ with the "
                  f"ingest-idc-cohort.py directory convention.")
            return

        for n in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            slicer.mrmlScene.RemoveNode(n)
        for n in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
            slicer.mrmlScene.RemoveNode(n)
        if self._prompts is not None:
            self._prompts.clearAll()

        # Reuse the existing loader — same node names, same colors, same
        # overlay wiring.
        load_paths = {"ct": paths["ct"], "gt": paths["gt"],
                       "model_seg": paths["model_seg"],
                       "model_prob": paths["model_prob"]}
        self._sceneNodes = self.logic.loadCase(load_paths)
        self.logic.setupSliceViewOverlay(
            self._sceneNodes["ct"], self._sceneNodes["model_prob"],
            [self._sceneNodes["gt"], self._sceneNodes["model_seg"]])
        if self._sceneNodes["model_prob"]:
            self._threshold.setProbabilityVolume(self._sceneNodes["model_prob"])

        self._caseProgressLabel.setText(case_id)
        self._patientLabel.setText(f"Patient: {case_id}")
        self._anatomyLabel.setText(f"Anatomy: {self._cohortSection.modelName}")
        self._actionStatusLabel.setText(
            "Loaded. Use j/k to scrub slices, threshold slider to tune, "
            "1/2 to drop positive/negative prompts.")

        self._setCohortVisible(False)
        self._setPerCaseVisible(True)

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
