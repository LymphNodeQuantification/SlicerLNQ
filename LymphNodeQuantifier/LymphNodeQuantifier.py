"""Lymph Node Quantifier — Slicer module that runs LNQ segmentation models
via the `lnq-segmenter` CLI as a managed subprocess, then prepares the
result for downstream segment-statistics work.

Subprocess management is delegated to LymphNodeQuantifierLib.runner, which
wraps QProcess. This module owns the UI: model selection, run/cancel,
progress, log."""
from __future__ import annotations

import json
import logging
import os
import tempfile
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


_LNQ_SEGMENTER_REPO = "git+https://github.com/pieper/lnq-segmenter.git"


# =============================================================================
# Module
# =============================================================================

class LymphNodeQuantifier(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        parent.title = "Lymph Node Quantifier"
        parent.categories = ["LNQ"]
        parent.dependencies = []
        parent.contributors = ["Steve Pieper (Isomics)"]
        parent.helpText = (
            "Runs LNQ segmentation models (via the lnq-segmenter PyPI "
            "package) against the input CT volume and prepares the result "
            "for downstream segment-statistics analysis. Inference runs in "
            "an external subprocess with progress streaming + cancel."
        )
        parent.acknowledgementText = (
            "This work is part of the SlicerLNQ project; see "
            "https://lnqproject.org."
        )


# =============================================================================
# Logic
# =============================================================================

class LymphNodeQuantifierLogic(ScriptedLoadableModuleLogic):
    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)

    @staticmethod
    def hasSegmenter() -> bool:
        try:
            import lnq_segmenter  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def listModels() -> list:
        """Return registry entries from the installed lnq-segmenter. Empty
        list if it isn't installed yet."""
        if not LymphNodeQuantifierLogic.hasSegmenter():
            return []
        from lnq_segmenter import registry
        return registry.list_models()

    @staticmethod
    def installSegmenter():
        """pip-install lnq-segmenter into Slicer's Python from git main."""
        slicer.util.pip_install(_LNQ_SEGMENTER_REPO)

    @staticmethod
    def saveVolumeToTempNrrd(volumeNode) -> str:
        """Write `volumeNode` to a temp .nrrd readable by lnq-segmenter."""
        tmp = tempfile.NamedTemporaryFile(
            suffix=".nrrd", delete=False, prefix="lnq-in-")
        tmp.close()
        if not slicer.util.saveNode(volumeNode, tmp.name):
            raise RuntimeError(f"failed to save volume to {tmp.name}")
        return tmp.name

    @staticmethod
    def loadSegmentationFromNrrd(segPath, name="LNQ Segmentation"):
        """Load a SEG NRRD as a vtkMRMLSegmentationNode and return it."""
        seg = slicer.util.loadSegmentation(segPath)
        if seg is None:
            raise RuntimeError(f"failed to load segmentation from {segPath}")
        seg.SetName(name)
        return seg

    @staticmethod
    def loadProbabilityMap(probPath, entry, volumeNode):
        """Load a single-channel float probability NRRD as a scalar volume,
        apply a heat-mapped color table, and overlay it over `volumeNode`
        in the slice views. Replaces any prior probability volume for the
        same (model, CT) pair so repeat runs don't pile up nodes."""
        name = f"LNQ:prob-{entry['name']}-{volumeNode.GetName()}"
        existing = slicer.util.getFirstNodeByName(name)
        if existing is not None:
            slicer.mrmlScene.RemoveNode(existing)
        vol = slicer.util.loadVolume(probPath, properties={"show": False})
        if vol is None:
            raise RuntimeError(f"failed to load probability map {probPath}")
        vol.SetName(name)
        disp = vol.GetDisplayNode()
        if disp is not None:
            # FullRainbow reads well over greyscale CT; clamp window to 0..1
            # so a colored voxel directly means "model thought it was a LN
            # with this probability."
            ct_node = slicer.mrmlScene.GetFirstNodeByClass(
                "vtkMRMLColorTableNode")
            heatmap = slicer.util.getFirstNodeByName(
                "Inferno") or slicer.util.getFirstNodeByName("FullRainbow")
            if heatmap is not None:
                disp.SetAndObserveColorNodeID(heatmap.GetID())
            disp.SetAutoWindowLevel(False)
            disp.SetWindowLevelMinMax(0.0, 1.0)
            disp.SetThreshold(0.05, 1.0)
            disp.SetApplyThreshold(True)
        # Show as the foreground over the CT in all slice views.
        composite = slicer.app.applicationLogic().GetSelectionNode()
        layoutManager = slicer.app.layoutManager()
        for c in layoutManager.sliceViewNames():
            sliceLogic = layoutManager.sliceWidget(c).sliceLogic()
            cn = sliceLogic.GetSliceCompositeNode()
            cn.SetForegroundVolumeID(vol.GetID())
            cn.SetForegroundOpacity(0.4)
        return vol

    @staticmethod
    def getOrCreateComposite(volumeNode):
        """Return the composite SegmentationNode for `volumeNode`, creating it
        on first use. One composite per CT — repeat runs on the same CT
        accumulate segments into the same node so the user can layer
        anatomies. Reference image geometry is locked to the CT so all
        per-region NRRDs (which share the CT's grid) drop in cleanly."""
        name = f"LNQ:composite-{volumeNode.GetName()}"
        existing = slicer.mrmlScene.GetFirstNodeByName(name)
        if existing and existing.IsA("vtkMRMLSegmentationNode"):
            return existing
        seg = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLSegmentationNode", name)
        seg.CreateDefaultDisplayNodes()
        seg.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
        return seg

    @staticmethod
    def mergeRegionIntoComposite(compositeSeg, regionNrrd, entry):
        """Load `regionNrrd` as a one-segment Segmentation, then move that
        segment into `compositeSeg` with the model's display name, color and
        SNOMED-CT terminology applied. If a segment with this region's name
        already exists in the composite (re-run on the same CT), it's
        replaced rather than duplicated."""
        tmp = slicer.util.loadSegmentation(regionNrrd)
        if tmp is None:
            raise RuntimeError(f"failed to load {regionNrrd}")
        try:
            src = tmp.GetSegmentation()
            if src.GetNumberOfSegments() == 0:
                # Predict produced an empty mask; nothing to merge but still
                # surface the result so the user knows the model ran.
                return None
            seg_name = (entry.get("segment_name")
                        or f"{entry.get('anatomy', entry['name'])} LNs")
            color = entry.get("color") or [255, 200, 60]
            r, g, b = [c / 255.0 for c in color[:3]]

            # If we already have a segment with this name from a prior run,
            # drop it so the new prediction takes its slot.
            existing_id = compositeSeg.GetSegmentation().GetSegmentIdBySegmentName(seg_name)
            if existing_id:
                compositeSeg.GetSegmentation().RemoveSegment(existing_id)

            src_id = src.GetNthSegmentID(0)
            src_segment = src.GetSegment(src_id)
            src_segment.SetName(seg_name)
            src_segment.SetColor(r, g, b)
            snomed_code = entry.get("snomed_code")
            snomed_term = entry.get("snomed_term")
            if snomed_code:
                src_segment.SetTag("SNOMED-CT.code", snomed_code)
            if snomed_term:
                src_segment.SetTag("SNOMED-CT.term", snomed_term)
            src_segment.SetTag("lnq.model",
                               f"{entry['name']}@{entry['version']}")

            compositeSeg.GetSegmentation().CopySegmentFromSegmentation(
                src, src_id)
            return seg_name
        finally:
            slicer.mrmlScene.RemoveNode(tmp)


# =============================================================================
# Widget
# =============================================================================

class LymphNodeQuantifierWidget(ScriptedLoadableModuleWidget):
    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        self.logic = LymphNodeQuantifierLogic()
        self._runner = None
        self._inputTempPath: Optional[str] = None
        # Queue of (entry, output_path) — one per anatomy the user checked.
        # Runs serially because a single GPU can only host one nnU-Net
        # ensemble at a time; the queue keeps the GUI responsive between runs.
        self._queue: list = []
        self._queueIndex: int = 0
        self._currentEntry: Optional[dict] = None
        self._currentOutputPath: Optional[str] = None
        self._currentProbPath: Optional[str] = None
        self._compositeSeg = None
        self._allOutputPaths: list = []   # for cleanup at end of run
        self._downloadBytesTotal: dict = {}
        self._downloadBytesDone: dict = {}

    # ----- UI construction -----

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)

        installBox = self._buildInstallSection()
        self.layout.addWidget(installBox)

        inputBox = self._buildInputSection()
        self.layout.addWidget(inputBox)

        modelsBox = self._buildModelsSection()
        self.layout.addWidget(modelsBox)

        runBox = self._buildRunSection()
        self.layout.addWidget(runBox)

        progressBox = self._buildProgressSection()
        self.layout.addWidget(progressBox)

        self.layout.addStretch(1)

        self._refreshSegmenterState()

    def _buildInstallSection(self):
        box = qt.QGroupBox("lnq-segmenter")
        lay = qt.QHBoxLayout(box)
        self._segStatusLabel = qt.QLabel("checking…")
        self._installButton = qt.QPushButton("Install / Update")
        self._installButton.setToolTip(
            f"pip-install {_LNQ_SEGMENTER_REPO} into Slicer's Python.")
        self._installButton.connect("clicked()", self._onInstallClicked)
        lay.addWidget(self._segStatusLabel, 1)
        lay.addWidget(self._installButton, 0)
        return box

    def _buildInputSection(self):
        box = qt.QGroupBox("Input")
        form = qt.QFormLayout(box)

        self._inputSelector = slicer.qMRMLNodeComboBox()
        self._inputSelector.nodeTypes = ["vtkMRMLScalarVolumeNode"]
        self._inputSelector.addEnabled = False
        self._inputSelector.removeEnabled = False
        self._inputSelector.noneEnabled = True
        self._inputSelector.setMRMLScene(slicer.mrmlScene)
        self._inputSelector.connect(
            "currentNodeChanged(vtkMRMLNode*)", self._refreshRunButton)
        form.addRow("CT volume:", self._inputSelector)

        self._deviceCombo = qt.QComboBox()
        self._deviceCombo.addItems(["cuda", "cpu", "mps"])
        form.addRow("Device:", self._deviceCombo)
        return box

    def _buildModelsSection(self):
        """Checkbox list of registered anatomies. Selection drives the run
        queue; each checked entry triggers one `lnq-segmenter predict` call
        whose output becomes a Segment in the composite SegmentationNode."""
        box = qt.QGroupBox("Anatomies")
        v = qt.QVBoxLayout(box)

        hdr = qt.QHBoxLayout()
        self._modelsSummaryLabel = qt.QLabel("checking registry…")
        self._modelsSummaryLabel.setStyleSheet("color: gray;")
        hdr.addWidget(self._modelsSummaryLabel, 1)
        self._selectAllButton = qt.QPushButton("All")
        self._selectNoneButton = qt.QPushButton("None")
        for b in (self._selectAllButton, self._selectNoneButton):
            b.setFixedWidth(60)
        self._selectAllButton.connect(
            "clicked()", lambda: self._setAllChecked(True))
        self._selectNoneButton.connect(
            "clicked()", lambda: self._setAllChecked(False))
        hdr.addWidget(self._selectAllButton)
        hdr.addWidget(self._selectNoneButton)
        v.addLayout(hdr)

        self._modelsList = qt.QListWidget()
        self._modelsList.setSelectionMode(qt.QAbstractItemView.NoSelection)
        # itemChanged fires for both check + text edits; we only care about
        # check toggles, but text isn't editable so this is fine.
        self._modelsList.connect("itemChanged(QListWidgetItem*)",
                                  lambda _item: self._refreshRunButton())
        v.addWidget(self._modelsList)
        return box

    def _buildRunSection(self):
        box = qt.QGroupBox("Run")
        lay = qt.QVBoxLayout(box)

        # Optional: emit the foreground probability map alongside the SEG.
        # Useful for inspecting "ballpark vs missed the mark" on OOD cases
        # and as the seed signal for the Review tab's paint tools.
        self._saveProbCheckbox = qt.QCheckBox(
            "Save probability map (Inferno overlay on the CT)")
        self._saveProbCheckbox.setToolTip(
            "Write the 5-fold-averaged foreground softmax as a scalar volume "
            "in addition to the SEG. Visualizes confidence per voxel.")
        lay.addWidget(self._saveProbCheckbox)

        row = qt.QHBoxLayout()
        self._runButton = qt.QPushButton("Run selected")
        self._runButton.setDefault(True)
        self._runButton.connect("clicked()", self._onRunClicked)
        self._cancelButton = qt.QPushButton("Cancel")
        self._cancelButton.enabled = False
        self._cancelButton.connect("clicked()", self._onCancelClicked)
        row.addWidget(self._runButton, 1)
        row.addWidget(self._cancelButton, 0)
        lay.addLayout(row)
        return box

    def _buildProgressSection(self):
        box = qt.QGroupBox("Progress")
        lay = qt.QVBoxLayout(box)
        self._statusLabel = qt.QLabel("idle")
        self._progressBar = qt.QProgressBar()
        self._progressBar.setRange(0, 100)
        self._progressBar.setValue(0)
        self._progressBar.setTextVisible(True)
        self._logView = qt.QPlainTextEdit()
        self._logView.setReadOnly(True)
        self._logView.setMaximumBlockCount(2000)
        self._logView.setFont(qt.QFont("Menlo", 10))
        lay.addWidget(self._statusLabel)
        lay.addWidget(self._progressBar)
        lay.addWidget(self._logView, 1)
        return box

    # ----- state management -----

    def _refreshSegmenterState(self):
        if self.logic.hasSegmenter():
            self._segStatusLabel.setText("✓ installed")
            self._installButton.setText("Update")
            self._populateModels()
        else:
            self._segStatusLabel.setText(
                "not installed — click Install to fetch from GitHub.")
            self._installButton.setText("Install")
            self._modelsList.clear()
            self._modelsSummaryLabel.setText("install lnq-segmenter first.")
        self._refreshRunButton()

    def _populateModels(self):
        """Rebuild the checkbox list from the registry. Preserves any check
        state across refreshes (e.g. after an in-place Update reinstall) by
        keying on model name."""
        prior_checked = {
            self._modelsList.item(i).data(qt.Qt.UserRole)["name"]
            for i in range(self._modelsList.count)
            if self._modelsList.item(i).checkState() == qt.Qt.Checked
        }
        self._modelsList.blockSignals(True)
        self._modelsList.clear()
        models = self.logic.listModels()
        total_mb = 0.0
        for m in models:
            mb = sum(a.get("size_bytes") or 0
                     for a in m.get("weights_assets") or []) / 1e6
            total_mb += mb
            label = (f"{m['display_name']}  —  "
                     f"{m['name']}@{m['version']}  ({mb:.0f} MB)")
            item = qt.QListWidgetItem(label)
            item.setFlags(item.flags() | qt.Qt.ItemIsUserCheckable)
            item.setCheckState(
                qt.Qt.Checked if m["name"] in prior_checked
                else qt.Qt.Unchecked)
            item.setData(qt.Qt.UserRole, m)
            self._modelsList.addItem(item)
        self._modelsList.blockSignals(False)
        if not models:
            self._modelsSummaryLabel.setText("(no models in registry yet)")
        else:
            self._modelsSummaryLabel.setText(
                f"{len(models)} model(s) available · "
                f"{total_mb:.0f} MB total if you select all")
        self._refreshRunButton()

    def _setAllChecked(self, checked: bool):
        state = qt.Qt.Checked if checked else qt.Qt.Unchecked
        self._modelsList.blockSignals(True)
        for i in range(self._modelsList.count):
            self._modelsList.item(i).setCheckState(state)
        self._modelsList.blockSignals(False)
        self._refreshRunButton()

    def _selectedEntries(self) -> list:
        out = []
        for i in range(self._modelsList.count):
            item = self._modelsList.item(i)
            if item.checkState() == qt.Qt.Checked:
                out.append(item.data(qt.Qt.UserRole))
        return out

    def _refreshRunButton(self):
        ready = (
            self.logic.hasSegmenter()
            and len(self._selectedEntries()) > 0
            and self._inputSelector.currentNode() is not None
            and (self._runner is None or not self._runner.isRunning())
        )
        self._runButton.enabled = ready

    # ----- actions -----

    def _onInstallClicked(self):
        self._installButton.enabled = False
        self._segStatusLabel.setText("installing…")
        qt.QApplication.processEvents()
        try:
            self.logic.installSegmenter()
        except Exception as exc:
            logging.exception("installSegmenter failed")
            slicer.util.errorDisplay(f"Install failed: {exc}")
        finally:
            self._installButton.enabled = True
            self._refreshSegmenterState()

    def _onRunClicked(self):
        volumeNode = self._inputSelector.currentNode()
        selected = self._selectedEntries()
        if not volumeNode or not selected:
            return
        try:
            self._inputTempPath = self.logic.saveVolumeToTempNrrd(volumeNode)
        except Exception as exc:
            slicer.util.errorDisplay(f"Failed to save input volume: {exc}")
            return

        # One composite SegmentationNode per CT; re-runs accumulate or replace
        # segments in place.
        self._compositeSeg = self.logic.getOrCreateComposite(volumeNode)

        # Allocate one output path per region; deleted only after the full
        # batch finishes (so a mid-batch cancel still leaves partial NRRDs
        # available for inspection).
        save_prob = self._saveProbCheckbox.isChecked()
        self._queue = []
        self._allOutputPaths = []
        for entry in selected:
            out_fd = tempfile.NamedTemporaryFile(
                suffix=".nrrd", delete=False,
                prefix=f"lnq-{entry['name']}-")
            out_fd.close()
            os.remove(out_fd.name)  # let lnq-segmenter create it
            prob_path = None
            if save_prob:
                prob_fd = tempfile.NamedTemporaryFile(
                    suffix=".nrrd", delete=False,
                    prefix=f"lnq-{entry['name']}-prob-")
                prob_fd.close()
                os.remove(prob_fd.name)
                prob_path = prob_fd.name
                self._allOutputPaths.append(prob_path)
            self._queue.append((entry, out_fd.name, prob_path))
            self._allOutputPaths.append(out_fd.name)
        self._queueIndex = 0

        self._logView.clear()
        self._appendLog(
            f"-> queue of {len(self._queue)} on {volumeNode.GetName()} "
            f"({self._deviceCombo.currentText})")
        self._runButton.enabled = False
        self._cancelButton.enabled = True
        self._startNextInQueue()

    def _startNextInQueue(self):
        """Pop the next (entry, output_path, prob_path) and spawn its run."""
        if self._queueIndex >= len(self._queue):
            self._onBatchDone()
            return
        entry, output_path, prob_path = self._queue[self._queueIndex]
        self._currentEntry = entry
        self._currentOutputPath = output_path
        self._currentProbPath = prob_path

        self._downloadBytesTotal.clear()
        self._downloadBytesDone.clear()
        self._progressBar.setRange(0, 100)
        self._progressBar.setValue(0)
        self._statusLabel.setText(
            f"[{self._queueIndex + 1}/{len(self._queue)}] "
            f"starting {entry['name']}…")
        self._appendLog(
            f"--- [{self._queueIndex + 1}/{len(self._queue)}] "
            f"{entry['name']}@{entry['version']} ---")

        from LymphNodeQuantifierLib.runner import SegmenterRunner
        self._runner = SegmenterRunner(self.parent)
        self._runner.started.connect(self._onProcStarted)
        self._runner.progressEvent.connect(self._onProgressEvent)
        self._runner.logLine.connect(self._appendLog)
        self._runner.finished.connect(self._onProcFinished)
        self._runner.start_predict(
            f"{entry['name']}@{entry['version']}",
            self._inputTempPath, output_path,
            device=self._deviceCombo.currentText,
            probability_output=prob_path,
        )

    def _onCancelClicked(self):
        if self._runner is not None:
            self._appendLog("-> cancel requested (SIGKILL)")
            self._runner.cancel()
            self._cancelButton.enabled = False

    # ----- runner signal handlers -----

    def _onProcStarted(self):
        self._statusLabel.setText("running…")

    def _onProgressEvent(self, payload):
        # Runner emits the structured event as a JSON string (see runner.py
        # for the why); deserialize here to keep the rest of this handler
        # working on a real dict.
        try:
            event = json.loads(payload) if isinstance(payload, str) else payload
        except Exception:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("event")
        if kind == "bundle_already_cached":
            self._statusLabel.setText("weights cached")
        elif kind == "download_start":
            asset = event.get("asset", "?")
            total = event.get("bytes_total") or 0
            self._downloadBytesTotal[asset] = total
            self._downloadBytesDone[asset] = 0
            self._updateDownloadProgress(asset)
        elif kind == "download_progress":
            asset = event.get("asset", "?")
            self._downloadBytesDone[asset] = event.get("bytes_done") or 0
            self._updateDownloadProgress(asset)
        elif kind == "download_done":
            asset = event.get("asset", "?")
            self._downloadBytesDone[asset] = self._downloadBytesTotal.get(asset, 0)
            self._updateDownloadProgress(asset, done=True)
        elif kind == "unzip_start":
            self._statusLabel.setText(f"unpacking {event.get('asset', '')}…")
        elif kind == "bundle_ready":
            self._statusLabel.setText("weights ready")
        elif kind == "predict_start":
            self._statusLabel.setText(
                f"running inference  ({len(event.get('folds') or [])}-fold)…")
            self._progressBar.setRange(0, 0)  # indeterminate
        elif kind == "predict_done":
            self._statusLabel.setText("inference complete")
            self._progressBar.setRange(0, 100)
            self._progressBar.setValue(100)

    def _updateDownloadProgress(self, current_asset, done=False):
        total_bytes = sum(self._downloadBytesTotal.values()) or 0
        done_bytes = sum(self._downloadBytesDone.values()) or 0
        if total_bytes:
            pct = int(100.0 * done_bytes / max(total_bytes, 1))
            self._progressBar.setRange(0, 100)
            self._progressBar.setValue(pct)
        verb = "fetched" if done else "downloading"
        self._statusLabel.setText(
            f"{verb} {current_asset}  ·  "
            f"{done_bytes / 1e6:.0f} / {total_bytes / 1e6:.0f} MB")

    def _onProcFinished(self, exit_code, status):
        self._progressBar.setRange(0, 100)
        entry = self._currentEntry
        if status == "ok":
            self._progressBar.setValue(100)
            try:
                seg_name = self.logic.mergeRegionIntoComposite(
                    self._compositeSeg, self._currentOutputPath, entry)
                if seg_name:
                    self._appendLog(f"-> added segment '{seg_name}' to "
                                    f"{self._compositeSeg.GetName()}")
                else:
                    self._appendLog(f"-> {entry['name']}: empty mask "
                                    f"(model produced no foreground)")
            except Exception as exc:
                logging.exception("merge into composite failed")
                self._appendLog(f"-> merge failed: {exc}")
            if self._currentProbPath and os.path.isfile(self._currentProbPath):
                try:
                    vol = self.logic.loadProbabilityMap(
                        self._currentProbPath, entry,
                        self._inputSelector.currentNode())
                    self._appendLog(f"-> loaded probability map '{vol.GetName()}'")
                except Exception as exc:
                    logging.exception("loadProbabilityMap failed")
                    self._appendLog(f"-> probability load failed: {exc}")
            self._queueIndex += 1
            self._startNextInQueue()
            return
        if status == "cancelled":
            self._statusLabel.setText(
                f"cancelled after {self._queueIndex} of {len(self._queue)}")
        else:
            self._statusLabel.setText(
                f"{entry['name']} failed (exit {exit_code}); stopping queue")
        self._onBatchDone()

    def _onBatchDone(self):
        """Common cleanup whether the queue finished normally or aborted."""
        if self._queueIndex >= len(self._queue) and len(self._queue):
            self._statusLabel.setText(
                f"done — {len(self._queue)} region(s) merged into "
                f"{self._compositeSeg.GetName() if self._compositeSeg else '?'}")
            self._progressBar.setValue(100)
        self._runButton.enabled = True
        self._cancelButton.enabled = False
        self._cleanupTemps()
        self._queue = []
        self._queueIndex = 0
        self._currentEntry = None
        self._currentOutputPath = None
        self._currentProbPath = None
        # Don't clear _compositeSeg — leave it visible in the scene; next
        # run on the same CT will find it via getOrCreateComposite.

    def _cleanupTemps(self):
        paths = [self._inputTempPath] + list(self._allOutputPaths or [])
        for path in paths:
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._inputTempPath = None
        self._allOutputPaths = []

    # ----- log -----

    def _appendLog(self, line: str):
        self._logView.appendPlainText(line)


# =============================================================================
# Test stub
# =============================================================================

class LymphNodeQuantifierTest(ScriptedLoadableModuleTest):
    def runTest(self):
        self.setUp()
        # Smoke-test wiring only; real inference is exercised via the GUI
        # against a known volume on a CUDA workstation.
        self.delayDisplay("LymphNodeQuantifier loaded.")
