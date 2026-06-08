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

# Anatomy palette for the extra (non-primary) model SEGs that get loaded
# alongside the case. Values mirror lnq-segmenter/_registry.json so the
# slice + 3D color is consistent with the model card. Anything not listed
# falls back to gray.
EXTRA_ANATOMY_COLORS = {
    "abdominopelvic-v1": (120 / 255, 220 / 255, 120 / 255),  # green
    "axillary-v1":       (255 / 255, 150 / 255, 100 / 255),  # orange
    "inguinal-v1":       (240 / 255, 220 / 255,  60 / 255),  # yellow-gold
}


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
                    # Pale-yellow GT — reads as a "safety" reference layer
                    # against both the dark CT and the magenta model SEG
                    # without competing with the red CT contrast.
                    s.SetColor(1.0, 0.95, 0.55)
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
                    # 3D representation lives behind the model SEG +
                    # probability VR; ~1/3 opacity keeps all three layers
                    # legible at once in the 3D view.
                    disp.SetVisibility3D(True)
                    disp.SetOpacity3D(0.33)
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
                    disp.SetVisibility3D(True)
                    disp.SetOpacity3D(0.33)
        if paths.get("model_prob") and os.path.isfile(paths["model_prob"]):
            out["model_prob"] = slicer.util.loadVolume(
                paths["model_prob"], properties={"show": False})
            if out["model_prob"] is not None:
                out["model_prob"].SetName("LNQReview:probability")
        return out

    @staticmethod
    def loadExtraAnatomySegmentation(model_name, seg_path):
        """Load one non-primary model's SEG NRRD and dress it so it reads
        as a distinct anatomy in the slice + 3D views. Colors come from
        EXTRA_ANATOMY_COLORS, falling back to gray if a new model is
        introduced without a palette entry."""
        if not seg_path or not os.path.isfile(seg_path):
            return None
        node = slicer.util.loadSegmentation(seg_path)
        if node is None:
            return None
        node.SetName(f"LNQReview:{model_name}")
        color = EXTRA_ANATOMY_COLORS.get(model_name, (0.6, 0.6, 0.6))
        seg = node.GetSegmentation()
        if seg.GetNumberOfSegments():
            sid = seg.GetNthSegmentID(0)
            s = seg.GetSegment(sid)
            s.SetName(model_name)
            s.SetColor(*color)
        disp = node.GetDisplayNode()
        if disp is not None:
            # Same dressing as the primary model SEG: faint fill + crisp
            # outline in 2D, 1/3 opacity in 3D so the layered anatomies
            # stay legible together. Visibility is on by default; the
            # extras follow the LNQ-prediction toggle (which currently
            # flips just the primary model — extras need their own
            # widget toggles in a follow-up if reviewers want
            # per-anatomy isolation).
            disp.SetVisibility2DFill(True)
            disp.SetOpacity2DFill(0.30)
            disp.SetVisibility2DOutline(True)
            disp.SetVisibility3D(True)
            disp.SetOpacity3D(0.33)
        return node

    @staticmethod
    def setupSliceViewOverlay(ct_node, prob_node, segmentation_nodes):
        """Wire all 3 slice views (Red/Yellow/Green) of the FourUp layout:
        CT background, probability as Inferno foreground on top,
        segmentations layered above. Reviewer can scan axial/coronal/
        sagittal simultaneously. Slice views are linked so panning and
        zooming in any one moves the other two in lockstep."""
        layoutManager = slicer.app.layoutManager()
        for color in ("Red", "Yellow", "Green"):
            sw = layoutManager.sliceWidget(color)
            if sw is None:
                continue
            cn = sw.sliceLogic().GetSliceCompositeNode()
            if ct_node is not None:
                cn.SetBackgroundVolumeID(ct_node.GetID())
            if prob_node is not None:
                cn.SetForegroundVolumeID(prob_node.GetID())
                cn.SetForegroundOpacity(0.55)
            # Linked navigation: any FOV / offset change propagates to the
            # other two slice views in the FourUp layout. Hot-link extends
            # that to the linked control widgets in the slice toolbar.
            cn.SetLinkedControl(True)
            cn.SetHotLinkedControl(True)
            sw.sliceLogic().FitSliceToAll()
        # Inferno colormap on the probability volume.
        if prob_node is not None:
            disp = prob_node.GetDisplayNode()
            if disp is not None:
                heat = (slicer.util.getFirstNodeByName("Inferno")
                        or slicer.util.getFirstNodeByName("FullRainbow"))
                if heat is not None:
                    disp.SetAndObserveColorNodeID(heat.GetID())

    @staticmethod
    def buildClosedSurfaces(seg_nodes):
        """Ensure each segmentation has a closed-surface representation
        so the 3D view can render the segments without the user having
        to flip the 'show 3D' switch in the SegmentEditor."""
        for n in seg_nodes:
            if n is None:
                continue
            try:
                n.CreateClosedSurfaceRepresentation()
            except Exception as exc:
                logging.warning("CreateClosedSurfaceRepresentation %s: %s",
                                n.GetName(), exc)

    @staticmethod
    def segmentationsCenter(seg_nodes, reference_volume):
        """Centroid (RAS) of the union of non-zero voxels across the
        provided segmentations, resampled onto reference_volume's grid.
        Used to jump slice offsets and frame the 3D view. Returns None if
        every segmentation is empty or reference_volume is missing.

        Why not GetRASBounds: that method reports the *source-volume*
        bounds on a segmentation node — i.e. the whole CT — so it picks
        the middle of the patient rather than the middle of the
        lymph-node cluster. Walk the labelmap representation on the CT
        grid instead and average the non-zero IJK indices, then convert
        through the CT's IJK→RAS matrix to land in world coordinates."""
        if reference_volume is None:
            return None
        import numpy as np
        ijk_sum = np.zeros(3, dtype=np.float64)
        ijk_count = 0
        for n in seg_nodes:
            if n is None:
                continue
            sid = n.GetSegmentation().GetNthSegmentID(0) if n.GetSegmentation() else None
            if sid is None:
                continue
            try:
                arr = slicer.util.arrayFromSegmentBinaryLabelmap(
                    n, sid, reference_volume)
            except Exception:
                continue
            if arr is None or arr.size == 0:
                continue
            nz = np.argwhere(arr > 0)            # (N, 3) in k, j, i order
            if nz.size == 0:
                continue
            ijk_sum += np.flip(nz, axis=1).sum(axis=0)   # → i, j, k
            ijk_count += nz.shape[0]
        if ijk_count == 0:
            return None
        ijk_mean = ijk_sum / ijk_count
        m = vtk.vtkMatrix4x4()
        reference_volume.GetIJKToRASMatrix(m)
        homogeneous = [ijk_mean[0], ijk_mean[1], ijk_mean[2], 1.0]
        out = [0.0, 0.0, 0.0, 0.0]
        m.MultiplyPoint(homogeneous, out)
        return [out[0], out[1], out[2]]

    @staticmethod
    def jumpSlicesToRAS(ras):
        """Drop each slice view onto the given RAS point. Uses JumpAllSlices
        so Red/Yellow/Green move together to the same world coordinate."""
        if ras is None:
            return
        slicer.modules.markups.logic().JumpSlicesToLocation(
            ras[0], ras[1], ras[2], True)

    @staticmethod
    def segmentationsExtent(seg_nodes, reference_volume, pad_mm=30.0):
        """RAS bounding-box half-extents (mm) for the union of non-zero
        voxels across `seg_nodes`, padded so the slice views show some
        anatomical context around the cluster. Returned as (dR, dA, dS)
        so callers can size each slice view's FOV per axis."""
        if reference_volume is None:
            return None
        import numpy as np
        ijk_min = np.array([+1e18] * 3, dtype=np.float64)
        ijk_max = np.array([-1e18] * 3, dtype=np.float64)
        any_filled = False
        for n in seg_nodes:
            if n is None:
                continue
            sid = n.GetSegmentation().GetNthSegmentID(0) if n.GetSegmentation() else None
            if sid is None:
                continue
            try:
                arr = slicer.util.arrayFromSegmentBinaryLabelmap(
                    n, sid, reference_volume)
            except Exception:
                continue
            if arr is None or arr.size == 0:
                continue
            nz = np.argwhere(arr > 0)
            if nz.size == 0:
                continue
            any_filled = True
            ijk_min = np.minimum(ijk_min, np.flip(nz, axis=1).min(axis=0))
            ijk_max = np.maximum(ijk_max, np.flip(nz, axis=1).max(axis=0))
        if not any_filled:
            return None
        m = vtk.vtkMatrix4x4()
        reference_volume.GetIJKToRASMatrix(m)
        ras_corners = []
        for di in (ijk_min[0], ijk_max[0]):
            for dj in (ijk_min[1], ijk_max[1]):
                for dk in (ijk_min[2], ijk_max[2]):
                    out = [0.0] * 4
                    m.MultiplyPoint([di, dj, dk, 1.0], out)
                    ras_corners.append(out[:3])
        import numpy as np
        ras_corners = np.array(ras_corners)
        ras_min = ras_corners.min(axis=0)
        ras_max = ras_corners.max(axis=0)
        return (
            0.5 * (ras_max[0] - ras_min[0]) + pad_mm,
            0.5 * (ras_max[1] - ras_min[1]) + pad_mm,
            0.5 * (ras_max[2] - ras_min[2]) + pad_mm,
        )

    @staticmethod
    def zoomSlicesToExtent(extent):
        """Set each slice view's FOV so its in-plane axes contain the
        provided RAS half-extents while preserving the viewport's pixel
        aspect ratio. extent = (dR, dA, dS).

        Each slice viewer has a fixed pixel aspect ratio determined by
        the layout; if we set FOV (W, H) that doesn't match the pixel
        aspect, Slicer stretches the rendered image non-uniformly. The
        right play is: pick whichever axis is the limiting one
        (segmentation half-width / viewport_aspect vs. segmentation
        half-height), use that as the FOV's dominant dimension, and let
        the other axis grow to fill — so the segmentation just fits and
        the aspect ratio is preserved.

        Axial (Red) shows R/A in-plane; Green coronal shows R/S; Yellow
        sagittal shows A/S. Composite nodes are linked, so a slice
        offset change in one propagates — but FOV doesn't auto-sync,
        which is why we set each view explicitly."""
        if extent is None:
            return
        dR, dA, dS = extent
        lm = slicer.app.layoutManager()
        plans = [
            ("Red",    2 * dR, 2 * dA),
            ("Green",  2 * dR, 2 * dS),
            ("Yellow", 2 * dA, 2 * dS),
        ]
        for color, want_w, want_h in plans:
            sw = lm.sliceWidget(color)
            if sw is None:
                continue
            node = sw.mrmlSliceNode()
            if node is None:
                continue
            dims = node.GetDimensions()
            if not dims or len(dims) < 2 or dims[0] <= 0 or dims[1] <= 0:
                continue
            viewport_aspect = float(dims[1]) / float(dims[0])
            # Choose FOV so both desired half-extents are inside the
            # viewport, preserving the viewport's H/W ratio.
            fov_w = max(want_w, want_h / viewport_aspect)
            fov_h = fov_w * viewport_aspect
            current = node.GetFieldOfView()
            depth = current[2] if current and len(current) >= 3 else 1.0
            node.SetFieldOfView(fov_w, fov_h, depth)

    @staticmethod
    def frame3DViewOnRAS(ras, half_extent_mm=80.0):
        """Aim the active 3D camera at the given RAS point and pull in
        the dolly so a ~2*half_extent_mm window around the focal point
        fills the view. Without an explicit zoom the 3D view frames the
        whole probability volume (~50 cm tall on chest CTs) and the LN
        cluster sits as a ~10-px speck. Also turns off the box / axis
        labels so the volume-rendered iso-shell reads cleanly against
        the dark background."""
        if ras is None:
            return
        layoutManager = slicer.app.layoutManager()
        if layoutManager.threeDViewCount == 0:
            return
        viewWidget = layoutManager.threeDWidget(0)
        view = viewWidget.threeDView()
        viewNode = view.mrmlViewNode()
        if viewNode is not None:
            viewNode.SetBoxVisible(False)
            viewNode.SetAxisLabelsVisible(False)
        cam = view.cameraNode()
        if cam is None:
            view.resetFocalPoint()
            view.resetCamera()
            return
        cam.SetFocalPoint(ras[0], ras[1], ras[2])
        # Look from the anterior (RAS +A) so the slice we just jumped to
        # is roughly head-on; offset is far enough that the parallel-
        # projection / clipping planes don't trim the volume.
        position = [ras[0], ras[1] + 4 * half_extent_mm, ras[2]]
        cam.SetPosition(position[0], position[1], position[2])
        cam.SetViewUp(0.0, 0.0, 1.0)
        camRaw = cam.GetCamera()
        if camRaw is not None:
            # ParallelScale = half the vertical extent in world units. Pull
            # in the dolly to roughly the LN cluster's bounding sphere.
            camRaw.SetParallelScale(half_extent_mm)
            camRaw.SetClippingRange(half_extent_mm * 0.5,
                                     half_extent_mm * 8.0)
        view.scheduleRender()

    # ----- probability volume rendering -----

    @staticmethod
    def setupProbabilityVolumeRendering(prob_node, threshold):
        """Configure the volume-rendering display node for the probability
        map so it shows a thin opacity 'spike' at the current slice
        threshold. The spike acts like an iso-surface outline in 3D —
        anywhere the probability map crosses p == threshold renders a
        narrow band, so the reviewer sees the same iso-surface in 3D that
        the slice threshold drives in 2D. Updates by re-calling this
        function when the slider changes."""
        if prob_node is None:
            return None
        vrLogic = slicer.modules.volumerendering.logic()
        disp = vrLogic.GetFirstVolumeRenderingDisplayNode(prob_node)
        if disp is None:
            disp = vrLogic.CreateDefaultVolumeRenderingNodes(prob_node)
        if disp is None:
            return None
        disp.SetVisibility(True)
        LNQReviewLogic._updateProbabilityVRTransferFunction(disp, threshold)
        return disp

    @staticmethod
    def _updateProbabilityVRTransferFunction(disp, threshold):
        """Build the spike opacity + Inferno-colored RGB transfer function
        on the probability VR display node. The band is sized in
        *log space* (half a decade on each side of the threshold) so it
        stays visible regardless of how deep the slider is — at p=0.001
        a linear ±10% band would be 1e-4 wide, well below the ray-caster's
        sampling density."""
        if disp is None:
            return
        propNode = disp.GetVolumePropertyNode()
        if propNode is None:
            return
        prop = propNode.GetVolumeProperty()
        if prop is None:
            return
        import math
        t = max(1e-5, min(0.999, float(threshold)))
        # Half a decade in log space (factor ~3.16). Stays narrow enough
        # to read as an outline while leaving the inner / outer core
        # transparent for context.
        log_eps = 0.5
        lo = max(1e-6, t / (10 ** log_eps))
        hi = min(1.0,  t * (10 ** log_eps))

        opacity = prop.GetScalarOpacity()
        opacity.RemoveAllPoints()
        opacity.AddPoint(0.0, 0.0)
        opacity.AddPoint(lo,   0.0)
        opacity.AddPoint(t,    1.0)
        opacity.AddPoint(hi,   0.0)
        opacity.AddPoint(1.0,  0.0)

        rgb = prop.GetRGBTransferFunction()
        rgb.RemoveAllPoints()
        # Inferno-ish — dark purple at low p, hot yellow at the spike,
        # tapering to white at high p. The non-spike colors are invisible
        # (opacity 0) but Slicer still needs them defined.
        rgb.AddRGBPoint(0.0, 0.05, 0.03, 0.18)
        rgb.AddRGBPoint(lo,  0.40, 0.10, 0.40)
        rgb.AddRGBPoint(t,   1.00, 0.75, 0.10)
        rgb.AddRGBPoint(hi,  0.95, 0.55, 0.10)
        rgb.AddRGBPoint(1.0, 1.00, 0.95, 0.85)

        # No gradient opacity contribution — let the scalar spike alone
        # drive the outline (gradient opacity here would dim the band on
        # smoothly-varying regions of the probability map).
        gradOpacity = prop.GetGradientOpacity()
        gradOpacity.RemoveAllPoints()
        gradOpacity.AddPoint(0.0, 1.0)
        gradOpacity.AddPoint(255.0, 1.0)

        prop.SetShade(True)
        prop.SetAmbient(0.35)
        prop.SetDiffuse(0.65)
        prop.SetSpecular(0.10)
        prop.SetInterpolationTypeToLinear()


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
        self._probVRDisplayNode = None       # vtkMRMLVolumeRenderingDisplayNode
        self._extraAnatomyNodes = {}         # {model_name: vtkMRMLSegmentationNode}

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
        # Push the 3D iso-band along with the 2D threshold so the
        # reviewer sees the same surface in both views.
        if self._probVRDisplayNode is not None:
            self.logic._updateProbabilityVRTransferFunction(
                self._probVRDisplayNode, threshold)
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
        # Button is checkable: checked == "hidden". SetVisibility() drops
        # both 2D + 3D in one call so the toggle affects every viewer.
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
        # 2D foreground in every slice viewer + the 3D VR display node
        # get hidden together so the toggle catches all three viewports.
        # Setting foreground opacity to 0 is the standard way to suppress
        # the slice layer without tearing it down.
        hide = self._overlayVisibleButton.isChecked()
        opacity = 0.0 if hide else 0.55
        for c in slicer.app.layoutManager().sliceViewNames():
            cn = slicer.app.layoutManager().sliceWidget(c).sliceLogic().GetSliceCompositeNode()
            cn.SetForegroundOpacity(opacity)
        if self._probVRDisplayNode is not None:
            self._probVRDisplayNode.SetVisibility(not hide)
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
        for n in slicer.util.getNodesByClass("vtkMRMLVolumeRenderingDisplayNode"):
            slicer.mrmlScene.RemoveNode(n)
        # The prior VR pointer just became invalid; null it before the
        # threshold slider can fire and touch a dangling node.
        self._probVRDisplayNode = None
        if self._prompts is not None:
            self._prompts.clearAll()

        # Reviewer asked for 4-up so axial/coronal/sagittal + 3D are all
        # visible at the same time; the threshold-coupled probability VR
        # only makes sense if the 3D view is on screen.
        slicer.app.layoutManager().setLayout(
            slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)

        load_paths = {"ct": paths["ct"], "gt": paths["gt"],
                       "model_seg": paths["model_seg"],
                       "model_prob": paths["model_prob"]}
        self._sceneNodes = self.logic.loadCase(load_paths)
        # Load every additional anatomy that has a SEG on disk so the
        # reviewer sees abdominopelvic / axillary / inguinal candidates
        # at the same time as the primary mediastinal layers. Each is
        # painted in a distinct color (EXTRA_ANATOMY_COLORS).
        self._extraAnatomyNodes = {}
        for extra in paths.get("extra_anatomies", []):
            node = self.logic.loadExtraAnatomySegmentation(
                extra["name"], extra["seg_path"])
            if node is not None:
                self._extraAnatomyNodes[extra["name"]] = node
        all_seg_nodes = [self._sceneNodes["gt"], self._sceneNodes["model_seg"]]
        all_seg_nodes += list(self._extraAnatomyNodes.values())
        self.logic.setupSliceViewOverlay(
            self._sceneNodes["ct"], self._sceneNodes["model_prob"],
            all_seg_nodes)
        if self._sceneNodes["model_prob"]:
            self._threshold.setProbabilityVolume(self._sceneNodes["model_prob"])

        # 3D pipeline: closed-surface meshes for every segmentation + a
        # spike-opacity volume rendering of the primary probability map.
        # The VR is recomputed each time the threshold slider moves so
        # the 3D iso-band tracks what the slice views are showing. Only
        # the primary model gets a probability VR — the extras stay as
        # closed-surface meshes since stacking 4 VR display nodes would
        # both cost frame rate and read as visual mush.
        self.logic.buildClosedSurfaces(all_seg_nodes)
        self._probVRDisplayNode = self.logic.setupProbabilityVolumeRendering(
            self._sceneNodes["model_prob"], self._threshold.threshold)
        center = self.logic.segmentationsCenter(
            all_seg_nodes, self._sceneNodes["ct"])
        extent = self.logic.segmentationsExtent(
            all_seg_nodes, self._sceneNodes["ct"])
        if center is not None:
            self.logic.jumpSlicesToRAS(center)
            if extent is not None:
                self.logic.zoomSlicesToExtent(extent)
                # Match the 3D camera dolly to the biggest in-plane
                # half-extent so the LN cluster fills the 3D view the
                # same way it fills the slice views.
                self.logic.frame3DViewOnRAS(center,
                    half_extent_mm=max(extent) * 1.2)
            else:
                self.logic.frame3DViewOnRAS(center)

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
