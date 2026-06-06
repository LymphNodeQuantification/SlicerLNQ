"""Per-case interactive tools for LNQReview.

Two kinds of tools live here:

* Positive / Negative point prompts — vtkMRMLMarkupsFiducialNode pairs
  whose placement mode is bound to the tool buttons. Used both to drive
  the "tune threshold to prompts" sweep and (post-v1) to feed
  SlicerNNInteractive or similar prompt-based segmentation backends.

* Probability-threshold display — log-scaled slider whose value gates
  the Inferno overlay on the probability volume and (when the user
  clicks Accept) the connected-component grow that turns "model
  whispered here" into a segment edit."""
from __future__ import annotations

import math

import qt
import slicer
import vtk


POSITIVE_NAME = "LNQReview:positive"
NEGATIVE_NAME = "LNQReview:negative"


def getOrCreateFiducialNode(name, color_rgb):
    """Find a Markups fiducial node by name or make a fresh one with the
    given display color. Used as the destination for click-to-place."""
    node = slicer.util.getFirstNodeByName(name)
    if node is None or not node.IsA("vtkMRMLMarkupsFiducialNode"):
        node = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLMarkupsFiducialNode", name)
    disp = node.GetDisplayNode()
    if disp is not None:
        disp.SetSelectedColor(*[c / 255.0 for c in color_rgb])
        disp.SetColor(*[c / 255.0 for c in color_rgb])
        disp.SetGlyphScale(2.0)
    return node


def clearFiducialNode(node):
    if node is not None:
        node.RemoveAllControlPoints()


class PromptToolController:
    """Switches Slicer's interaction mode between placing positive
    fiducials, placing negative fiducials, and the default view mode."""

    def __init__(self):
        self.positive = getOrCreateFiducialNode(POSITIVE_NAME, (60, 220, 60))
        self.negative = getOrCreateFiducialNode(NEGATIVE_NAME, (230, 60, 60))
        self._activeMode = None

    def setMode(self, mode):
        """`mode` is "positive" | "negative" | None.

        When "positive"/"negative" is selected, Slicer's selection node
        is pointed at the matching fiducial node and StartPlaceMode is
        engaged so the next left-click drops a point. Persistent place
        mode is on so the reviewer can drop several in a row without
        re-clicking the tool button."""
        if mode is None:
            self._endPlace()
            self._activeMode = None
            return
        target = self.positive if mode == "positive" else self.negative
        sel = slicer.app.applicationLogic().GetSelectionNode()
        sel.SetReferenceActivePlaceNodeClassName(
            "vtkMRMLMarkupsFiducialNode")
        sel.SetActivePlaceNodeID(target.GetID())
        interaction = slicer.app.applicationLogic().GetInteractionNode()
        interaction.SetPlaceModePersistence(1)
        interaction.SwitchToPersistentPlaceMode()
        self._activeMode = mode

    def _endPlace(self):
        interaction = slicer.app.applicationLogic().GetInteractionNode()
        interaction.SetPlaceModePersistence(0)
        interaction.SwitchToViewTransformMode()

    @property
    def activeMode(self):
        return self._activeMode

    def clearAll(self):
        clearFiducialNode(self.positive)
        clearFiducialNode(self.negative)


class ThresholdController:
    """Owns the log-scaled probability threshold + a couple of helpers
    that read positive/negative prompts and pick the cutoff that best
    separates them.

    The slider lives in the widget; this class exposes apply()/read()
    so the wiring stays clean."""

    LOG_MIN = -5.0   # 10**-5 = 1e-5
    LOG_MAX = 0.0    # 10**0  = 1.0
    SLIDER_TICKS = 1000

    def __init__(self):
        self.threshold = 0.001
        self.probabilityVolume = None  # vtkMRMLScalarVolumeNode

    # ----- slider <-> probability translation -----

    @classmethod
    def sliderToThreshold(cls, slider_value):
        """slider_value in [0, SLIDER_TICKS]. Returns p in [1e-5, 1]."""
        frac = max(0.0, min(1.0, slider_value / cls.SLIDER_TICKS))
        log_p = cls.LOG_MIN + frac * (cls.LOG_MAX - cls.LOG_MIN)
        return 10 ** log_p

    @classmethod
    def thresholdToSlider(cls, threshold):
        if threshold <= 0:
            return 0
        log_p = math.log10(threshold)
        frac = (log_p - cls.LOG_MIN) / (cls.LOG_MAX - cls.LOG_MIN)
        return int(round(max(0.0, min(1.0, frac)) * cls.SLIDER_TICKS))

    # ----- act on the scene -----

    def setProbabilityVolume(self, volumeNode):
        self.probabilityVolume = volumeNode
        self.applyThresholdToDisplay()

    def setThreshold(self, threshold):
        self.threshold = max(10 ** self.LOG_MIN, min(1.0, threshold))
        self.applyThresholdToDisplay()

    def applyThresholdToDisplay(self):
        """Re-window the probability volume's display so colored voxels
        are exactly those above the current threshold. Keeps the Inferno
        overlay synchronized with what an Accept-here click would
        produce, per the spec's 'WYSIWYG' principle."""
        if self.probabilityVolume is None:
            return
        disp = self.probabilityVolume.GetDisplayNode()
        if disp is None:
            return
        disp.SetAutoWindowLevel(False)
        # Window the colormap from threshold up to 1.0 so threshold-just-
        # above-0 still gets the high end of the colormap. This means as
        # the user drags the threshold up, the still-above-threshold
        # voxels keep their colour magnitude.
        disp.SetWindowLevelMinMax(self.threshold, 1.0)
        disp.SetThreshold(self.threshold, 1.0)
        disp.SetApplyThreshold(True)

    # ----- tune threshold to prompts -----

    def tuneToPrompts(self, positiveNode, negativeNode):
        """Sweep thresholds; pick the one that best separates positive
        from negative prompt locations on the probability volume.

        Returns the picked threshold (a float) and a small dict of
        diagnostics: number of positives / negatives the sweep saw, the
        objective value at the picked threshold, etc."""
        diagnostics = {"positives": 0, "negatives": 0, "picked": None,
                       "objective": None, "note": ""}
        if self.probabilityVolume is None:
            diagnostics["note"] = "no probability volume loaded"
            return self.threshold, diagnostics

        pos = _samplePoints(positiveNode, self.probabilityVolume)
        neg = _samplePoints(negativeNode, self.probabilityVolume)
        diagnostics["positives"] = len(pos)
        diagnostics["negatives"] = len(neg)
        if not pos:
            diagnostics["note"] = "no positive prompts placed yet"
            return self.threshold, diagnostics

        # Sweep a log-spaced grid + the prompt probabilities themselves
        # as candidates.
        import numpy as np
        candidates = sorted(set(
            list(np.logspace(self.LOG_MIN, self.LOG_MAX, 40))
            + pos + neg))

        # Objective: balanced accuracy across the two prompt sets.
        # ba(t) = 0.5 * ( |pos >= t| / |pos|  +  |neg < t| / max(|neg|, 1) )
        # If there are no negatives, fall back to "lowest t that still
        # admits all positives" — the lower bound of the positive set.
        if not neg:
            picked = min(pos) if pos else self.threshold
            diagnostics["picked"] = picked
            diagnostics["objective"] = 1.0
            diagnostics["note"] = "no negative prompts; took min(positive)"
            return picked, diagnostics

        best = (-1.0, self.threshold)
        for t in candidates:
            tp = sum(1 for v in pos if v >= t)
            tn = sum(1 for v in neg if v < t)
            ba = 0.5 * (tp / len(pos) + tn / len(neg))
            if ba > best[0]:
                best = (ba, t)
        diagnostics["objective"] = round(best[0], 4)
        diagnostics["picked"] = best[1]
        return best[1], diagnostics


def _samplePoints(fiducialNode, volumeNode):
    """For each Markups control point, sample the volume at that RAS
    location and return the list of probability values. Out-of-bounds
    points are dropped silently."""
    if fiducialNode is None or volumeNode is None:
        return []
    try:
        import slicer.util as _util
        arr = _util.arrayFromVolume(volumeNode)
    except Exception:
        return []
    dims = volumeNode.GetImageData().GetDimensions()  # x, y, z
    rasToIjk = vtk.vtkMatrix4x4()
    volumeNode.GetRASToIJKMatrix(rasToIjk)

    values = []
    n = fiducialNode.GetNumberOfControlPoints()
    for i in range(n):
        ras = [0.0, 0.0, 0.0]
        fiducialNode.GetNthControlPointPosition(i, ras)
        ijkH = [0.0, 0.0, 0.0, 0.0]
        rasToIjk.MultiplyPoint(ras + [1.0], ijkH)
        x, y, z = (int(round(c)) for c in ijkH[:3])
        if 0 <= x < dims[0] and 0 <= y < dims[1] and 0 <= z < dims[2]:
            values.append(float(arr[z, y, x]))
    return values
