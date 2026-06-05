"""Dashboard window for LNQ Studio.

Top-level window (own taskbar entry, geometry persisted) that shows:
  - Projects ready to train (approved annotations available, no live job)
  - Training jobs in flight + recent history
  - Drill-down for a selected job: annotation provenance, metrics, ETA, and
    (when wired) the per-epoch validation sequence loader.

Subscribes to CouchDB's _changes feed via ChronicleChangesWatcher so the UI
reflects new TrainingJob status / metrics within ~1 s, with no idle polling.
"""

import datetime
import logging
import math

import ctk
import qt
import slicer

try:
    import requests
except ImportError:
    slicer.util.pip_install("requests")
    import requests

from LNQStudioLib.chronicle_client import ChronicleChangesWatcher, ChronicleError


# Js2 status page (status.io). Same id the bin/jetstream-status.py uses.
_JS2_STATUS_API = "https://api.status.io/1.0/status/61dc808a7e9a82053ce739d2"
_JS2_STATUS_HTML = "https://jetstream.status.io/"
_JS2_DOCS_STATUS = "https://docs.jetstream-cloud.org/overview/status/"


_DASHBOARD_OBJ = "LNQ-DashboardWindow"
_DASHBOARD_GEOMETRY_KEY = "LNQStudio/dashboard_geometry"

_STATUS_COLOR = {
    "pending":    qt.QColor("#7f8c8d"),  # gray
    "running":    qt.QColor("#2980b9"),  # blue
    "converged":  qt.QColor("#27ae60"),  # green
    "failed":     qt.QColor("#c0392b"),  # red
    "cancelled":  qt.QColor("#8e44ad"),  # purple
}


class _ChangesProxy(qt.QObject):
    """Qt object that owns a thread-safe signal, used to marshal changes-feed
    notifications from the background thread onto the GUI thread."""
    changes = qt.Signal()


class DashboardWindow(qt.QWidget):

    def __init__(self, owner):
        qt.QWidget.__init__(self, slicer.util.mainWindow(), qt.Qt.Window)
        self.setObjectName(_DASHBOARD_OBJ)
        self.setWindowTitle("LNQ Dashboard")
        self._owner = owner
        self._watcher = None
        self._changesProxy = _ChangesProxy()
        self._changesProxy.changes.connect(self._refreshAll)
        self._selectedJobId = None

        outer = qt.QVBoxLayout(self)

        # ----- top row: project filter + manual refresh -----
        topRow = qt.QHBoxLayout()
        topRow.addWidget(qt.QLabel("Project:"))
        self._projectCombo = qt.QComboBox()
        self._projectCombo.sizeAdjustPolicy = qt.QComboBox.AdjustToMinimumContentsLengthWithIcon
        self._projectCombo.minimumContentsLength = 20
        self._projectCombo.currentIndexChanged.connect(self._refreshAll)
        topRow.addWidget(self._projectCombo, 1)
        self._refreshButton = qt.QPushButton("Refresh")
        self._refreshButton.clicked.connect(self._refreshAll)
        topRow.addWidget(self._refreshButton)
        self._liveLabel = qt.QLabel("●")
        self._liveLabel.setStyleSheet("color: gray;")
        self._liveLabel.toolTip = ("Changes-feed status. Green = live "
                                   "(subscribed to CouchDB _changes). "
                                   "Gray = not connected.")
        topRow.addWidget(self._liveLabel)

        # Js2 status badge — auto-updates every 5 min. Pre-empts launches
        # against a known-broken flavor by surfacing the open incident
        # before the user clicks Plan training / trainer.sh create.
        self._js2StatusLabel = qt.QLabel("Js2: checking…")
        self._js2StatusLabel.setStyleSheet("color: gray; padding: 0 6px;")
        self._js2StatusLabel.toolTip = (
            "Auto-refreshes from api.status.io every 5 min. "
            f"Open {_JS2_STATUS_HTML} in a browser for full details.")
        topRow.addWidget(self._js2StatusLabel)

        self._js2Timer = qt.QTimer(self)
        self._js2Timer.setInterval(5 * 60 * 1000)
        self._js2Timer.timeout.connect(self._refreshJs2Status)

        outer.addLayout(topRow)

        # A second row of clocks/state so the user can tell "is the feed
        # actually alive" and "how stale is what I'm looking at" without
        # having to interrogate the dashboard.
        feedRow = qt.QHBoxLayout()
        self._feedAgeLabel = qt.QLabel("Changes feed: idle")
        self._feedAgeLabel.setStyleSheet("color: gray; padding: 0 6px;")
        self._feedAgeLabel.toolTip = (
            "Time since the last CouchDB _changes notification arrived. "
            "Idle is normal — the feed is a long-poll, so silence means "
            "no docs have changed. If running training is silent for "
            ">5 min the runner may be stuck.")
        feedRow.addWidget(self._feedAgeLabel)
        feedRow.addStretch(1)
        outer.addLayout(feedRow)

        # Track last-change time so the age label is meaningful.
        self._lastChangeTime = None
        self._changesProxy.changes.connect(self._notifyChangeArrived)

        # ----- three vertical sections in a splitter -----
        splitter = qt.QSplitter(qt.Qt.Vertical)
        outer.addWidget(splitter, 1)

        # 1. Ready-to-train
        readyBox = ctk.ctkCollapsibleButton()
        readyBox.text = "Ready to train"
        readyBox.collapsed = False
        readyLayout = qt.QVBoxLayout(readyBox)
        self._readyTable = qt.QTableWidget()
        self._readyTable.columnCount = 4
        self._readyTable.setHorizontalHeaderLabels(
            ["Project", "Approved", "Pending jobs", "Action"])
        self._readyTable.horizontalHeader().setStretchLastSection(False)
        self._readyTable.verticalHeader().visible = False
        self._readyTable.editTriggers = qt.QAbstractItemView.NoEditTriggers
        self._readyTable.selectionBehavior = qt.QAbstractItemView.SelectRows
        readyLayout.addWidget(self._readyTable)
        splitter.addWidget(readyBox)

        # 2. Training jobs
        jobsBox = ctk.ctkCollapsibleButton()
        jobsBox.text = "Training jobs"
        jobsBox.collapsed = False
        jobsLayout = qt.QVBoxLayout(jobsBox)
        self._jobsTable = qt.QTableWidget()
        self._jobsTable.columnCount = 11
        self._jobsTable.setHorizontalHeaderLabels(
            ["Status", "Phase", "Project", "Label", "Fold", "Age",
             "Heartbeat", "Epoch", "ETA", "Mean Dice", "Host"])
        # job_id -> last non-None metrics dict; the trainer-runner's heartbeat
        # writes {train_loss:None, val_loss:None, mean_dice:None} for ~10 s
        # out of every 30 s window (parser-format bug we can't hot-patch
        # without restarting training), and the publisher overwrites with
        # real values seconds later. Keep the last-good values so the table
        # cells don't flicker between value and "—".
        self._stickyMetrics = {}
        self._jobsTable.verticalHeader().visible = False
        self._jobsTable.editTriggers = qt.QAbstractItemView.NoEditTriggers
        self._jobsTable.selectionBehavior = qt.QAbstractItemView.SelectRows
        self._jobsTable.selectionMode = qt.QAbstractItemView.SingleSelection
        self._jobsTable.itemSelectionChanged.connect(self._onJobSelectionChanged)
        jobsLayout.addWidget(self._jobsTable)
        splitter.addWidget(jobsBox)

        # Re-tick Age/Heartbeat columns every 5 s without re-fetching from
        # the chronicle. This is purely a UI clock; the underlying job data
        # is already in self._jobs.
        self._ageTimer = qt.QTimer(self)
        self._ageTimer.setInterval(5000)
        self._ageTimer.timeout.connect(self._refreshAgeColumns)

        # 3. Job detail
        detailBox = ctk.ctkCollapsibleButton()
        detailBox.text = "Selected job"
        detailBox.collapsed = False
        detailLayout = qt.QVBoxLayout(detailBox)
        self._detailSummary = qt.QLabel("(no job selected)")
        self._detailSummary.wordWrap = True
        self._detailSummary.setStyleSheet("color: gray;")
        detailLayout.addWidget(self._detailSummary)

        metricsRow = qt.QGridLayout()
        self._metricLabels = {}
        for col, key in enumerate(("epoch", "train_loss", "val_loss", "mean_dice", "eta")):
            metricsRow.addWidget(qt.QLabel(key.replace("_", " ").title() + ":"), 0, col*2)
            lbl = qt.QLabel("—")
            lbl.setStyleSheet("font-weight: bold;")
            self._metricLabels[key] = lbl
            metricsRow.addWidget(lbl, 0, col*2 + 1)
        detailLayout.addLayout(metricsRow)

        # Per-case Dice/volume/LR chart populated from
        # <fold_dir>/validation_raw/metrics.jsonl that lnq-val-snapshotter.py
        # writes on the trainer. Uses qSlicerWebWidget + Apache ECharts so we
        # get nice interactive charts without fighting Qt's matplotlib backend.
        # Collapsed by default so it doesn't fight the live state for screen
        # space until you actually want to inspect convergence.
        metricsChartBox = ctk.ctkCollapsibleGroupBox()
        metricsChartBox.title = "Training metrics (per-case Dice / volume / learning rate)"
        metricsChartBox.collapsed = True
        chartLayout = qt.QVBoxLayout(metricsChartBox)
        self._metricsChartWidget = slicer.qSlicerWebWidget()
        self._metricsChartWidget.setMinimumHeight(380)
        chartLayout.addWidget(self._metricsChartWidget)
        chartActionRow = qt.QHBoxLayout()
        self._metricChoiceCombo = qt.QComboBox()
        for label, value in (("Dice (per case)", "dice"),
                             ("Predicted volume (mL)", "pred_volume_ml"),
                             ("Volume residual (pred − GT, mL)", "volume_residual_ml"),
                             ("Train loss", "train_loss"),
                             ("Val loss", "val_loss"),
                             ("Learning rate (log)", "lr_log")):
            self._metricChoiceCombo.addItem(label, value)
        self._metricChoiceCombo.currentIndexChanged.connect(
            lambda *_: self._refreshMetricsChart())
        chartActionRow.addWidget(qt.QLabel("y:"))
        chartActionRow.addWidget(self._metricChoiceCombo)
        chartActionRow.addStretch(1)
        self._refreshChartButton = qt.QPushButton("Refresh")
        self._refreshChartButton.toolTip = (
            "SCP <fold>/validation_raw/metrics.jsonl from the trainer, "
            "re-render the chart. The snapshotter appends rows whenever it "
            "completes a predict cycle; click here once new ones land.")
        self._refreshChartButton.clicked.connect(self._refreshMetricsChart)
        chartActionRow.addWidget(self._refreshChartButton)
        chartLayout.addLayout(chartActionRow)
        detailLayout.addWidget(metricsChartBox)
        self._metricsChartWidget.setHtml(
            "<body style='font-family: sans-serif; padding: 20px; color: #777'>"
            "Select a TrainingJob row, expand this section, and click Refresh.</body>")

        # Cases table + live state are stacked in a vertical splitter so the
        # user can drag the boundary to give the live state log more room when
        # something interesting is happening. The "Pop out" button next to the
        # live-state header opens the same content in a separate, free-floating
        # window for tracking on a second monitor.
        casesLiveSplitter = qt.QSplitter(qt.Qt.Vertical)

        casesPanel = qt.QWidget()
        casesPanelLayout = qt.QVBoxLayout(casesPanel)
        casesPanelLayout.setContentsMargins(0, 0, 0, 0)
        casesPanelLayout.addWidget(qt.QLabel("<b>Training cases (annotation provenance)</b>"))
        self._casesTable = qt.QTableWidget()
        self._casesTable.columnCount = 6
        self._casesTable.setHorizontalHeaderLabels(
            ["Case", "Sex", "Primary site", "Producer", "Reviewer", "Review notes"])
        self._casesTable.horizontalHeader().setStretchLastSection(True)
        self._casesTable.verticalHeader().visible = False
        self._casesTable.editTriggers = qt.QAbstractItemView.NoEditTriggers
        casesPanelLayout.addWidget(self._casesTable, 1)
        casesLiveSplitter.addWidget(casesPanel)

        livePanel = qt.QWidget()
        livePanelLayout = qt.QVBoxLayout(livePanel)
        livePanelLayout.setContentsMargins(0, 0, 0, 0)
        liveHeaderRow = qt.QHBoxLayout()
        liveHeaderRow.addWidget(qt.QLabel(
            "<b>Live state</b> (events ← openstack ← VM telemetry)"))
        liveHeaderRow.addStretch(1)
        self._popOutLiveStateButton = qt.QPushButton("Pop out")
        self._popOutLiveStateButton.toolTip = (
            "Open the live state log in a separate, resizable window so you "
            "can keep it in view while the dashboard scrolls.")
        self._popOutLiveStateButton.clicked.connect(self._onPopOutLiveState)
        liveHeaderRow.addWidget(self._popOutLiveStateButton)
        livePanelLayout.addLayout(liveHeaderRow)
        # QTextEdit (not QPlainTextEdit) so we can colour each line with HTML
        # for the green→black fade applied to recently arrived lines.
        self._telemetryText = qt.QTextEdit()
        self._telemetryText.readOnly = True
        font = qt.QFont("Menlo")
        font.setStyleHint(qt.QFont.Monospace)
        self._telemetryText.setFont(font)
        self._telemetryText.setHtml(
            "<pre style='margin:0;'>(no observations yet — start "
            "<code>bin/trainer-observe.py</code> for openstack-side state, "
            "and the in-VM trainer-health daemon auto-publishes once cloud-init "
            "brings it up)</pre>")
        livePanelLayout.addWidget(self._telemetryText, 1)
        casesLiveSplitter.addWidget(livePanel)
        casesLiveSplitter.setSizes([220, 380])
        detailLayout.addWidget(casesLiveSplitter, 1)

        # Pop-out window state + animation buffer.
        self._liveStateRows = []
        self._liveStateWindow = None
        self._liveStateWindowView = None
        # Re-render the live state HTML every second so the green→black fade
        # animates smoothly even between doc updates from the changes feed.
        self._liveStateTimer = qt.QTimer(self)
        self._liveStateTimer.setInterval(1000)
        self._liveStateTimer.timeout.connect(self._renderLiveState)

        # Action row: open progress.png + load validation sequence (per case).
        actionRow = qt.QHBoxLayout()
        self._openProgressButton = qt.QPushButton("Open progress.png")
        self._openProgressButton.enabled = False
        self._openProgressButton.toolTip = "Fetch + display the trainer's progress.png. Available when a log_ref is set on the job."
        self._openProgressButton.clicked.connect(self._onOpenProgress)
        actionRow.addWidget(self._openProgressButton)
        actionRow.addWidget(qt.QLabel("  Validation sequence for case:"))
        self._validationCaseCombo = qt.QComboBox()
        self._validationCaseCombo.minimumContentsLength = 14
        self._validationCaseCombo.enabled = False
        actionRow.addWidget(self._validationCaseCombo, 1)
        self._loadValidationButton = qt.QPushButton("Load")
        self._loadValidationButton.enabled = False
        self._loadValidationButton.toolTip = (
            "Loads per-epoch held-out predictions for this case as a Slicer "
            "Sequence so you can scrub convergence visually. Requires "
            "lnq-val-snapshotter.py to be running on the fold's trainer "
            "instance (writes validation_raw/<case>_epoch_NNNNN.nrrd via "
            "nnUNetv2_predict against checkpoint_latest.pth on each Nth epoch). "
            "Files are scp'd down from the trainer over the chronicle jump host.")
        self._loadValidationButton.clicked.connect(self._onLoadValidationSequence)
        actionRow.addWidget(self._loadValidationButton)
        detailLayout.addLayout(actionRow)

        splitter.addWidget(detailBox)
        splitter.setSizes([180, 220, 400])

        if not self._restoreGeometry():
            self.resize(1100, 720)

        # Cached state populated by _refreshAll.
        self._projects = []
        self._annotations_by_project = {}   # project_id -> [annotation]
        self._jobs = []
        self._jobs_by_id = {}

    # ----- geometry persistence -----

    def _restoreGeometry(self):
        geom = qt.QSettings().value(_DASHBOARD_GEOMETRY_KEY)
        if geom:
            try:
                return self.restoreGeometry(geom)
            except Exception:
                return False
        return False

    def _persistGeometry(self):
        qt.QSettings().setValue(_DASHBOARD_GEOMETRY_KEY, self.saveGeometry())

    def hideEvent(self, event):
        self._persistGeometry()
        qt.QWidget.hideEvent(self, event)

    def closeEvent(self, event):
        self._persistGeometry()
        qt.QWidget.closeEvent(self, event)
        if self._owner is not None:
            self._owner._syncDashboardVisibilityState(False)

    # ----- lifecycle -----

    def attach(self):
        """Subscribe to the changes feed and load initial state."""
        client = self._owner.getClient()
        if client is None:
            self._liveLabel.setStyleSheet("color: gray;")
            self._detailSummary.text = "(connect first)"
            return
        self._refreshAll()
        if self._watcher is not None:
            self._watcher.stop()
        self._watcher = ChronicleChangesWatcher(
            client,
            on_changes=lambda _ch: self._changesProxy.changes.emit(),
            doc_types={"TrainingJob", "Annotation", "Project",
                        "CohortResolution", "ModelGeneration"},
        )
        self._watcher.start()
        self._liveLabel.setStyleSheet("color: #27ae60;")
        # Kick off Js2-status refresh now and arm the periodic timer.
        self._refreshJs2Status()
        self._js2Timer.start()
        self._ageTimer.start()
        self._liveStateTimer.start()

    def detach(self):
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        self._liveLabel.setStyleSheet("color: gray;")
        if self._js2Timer.isActive():
            self._js2Timer.stop()
        if self._ageTimer.isActive():
            self._ageTimer.stop()
        if self._liveStateTimer.isActive():
            self._liveStateTimer.stop()

    def _notifyChangeArrived(self):
        """Triggered when the changes feed produces a notification (separate
        from the refresh side-effect)."""
        self._lastChangeTime = datetime.datetime.now(datetime.timezone.utc)
        self._refreshFeedAge()

    def _refreshFeedAge(self):
        if self._lastChangeTime is None:
            self._feedAgeLabel.text = "Changes feed: connected (no events yet)"
            self._feedAgeLabel.setStyleSheet("color: gray; padding: 0 6px;")
            return
        age = (datetime.datetime.now(datetime.timezone.utc) - self._lastChangeTime).total_seconds()
        if age < 60:
            label = f"Last doc change: {int(age)}s ago"
            color = "#27ae60"
        elif age < 5 * 60:
            label = f"Last doc change: {int(age/60)} min ago"
            color = "#2980b9"
        else:
            label = f"Last doc change: {int(age/60)} min ago — quiet"
            color = "gray"
        self._feedAgeLabel.text = label
        self._feedAgeLabel.setStyleSheet(f"color: {color}; padding: 0 6px;")

    def _refreshJs2Status(self):
        """Sync-fetch Js2's status.io page. Quick (<1 s usually); the 5 min
        cadence means we tolerate occasional flake."""
        try:
            r = requests.get(_JS2_STATUS_API, timeout=8)
            r.raise_for_status()
            result = (r.json() or {}).get("result") or {}
        except Exception as exc:
            self._js2StatusLabel.text = "Js2: status check FAILED"
            self._js2StatusLabel.setStyleSheet("color: #c0392b; padding: 0 6px;")
            self._js2StatusLabel.toolTip = f"{exc}\n\nClick to open {_JS2_STATUS_HTML}"
            return
        overall = (result.get("status_overall") or {})
        op_code = overall.get("status_code") or 0
        op_text = overall.get("status") or "?"
        incidents = result.get("incidents") or []
        maint_blk = result.get("maintenance") or {}
        upcoming = maint_blk.get("upcoming") if isinstance(maint_blk, dict) else []
        # Build a label + tooltip.
        gpu_keywords = ("g3.", "g5.", "gpu", "a100", "h100", "shelv", "unshelv")
        gpu_incidents = []
        for inc in incidents:
            text = (inc.get("name") or "") + " "
            msgs = inc.get("messages") or []
            if msgs:
                text += msgs[0].get("details", "") or ""
            if any(k in text.lower() for k in gpu_keywords):
                gpu_incidents.append(inc)
        if op_code != 100:
            color = "#c0392b"  # red
            label = f"Js2: {op_text}"
        elif gpu_incidents:
            color = "#d35400"  # burnt orange — GPU-specific
            label = f"Js2: {len(gpu_incidents)} GPU incident" + ("s" if len(gpu_incidents) != 1 else "")
        elif incidents:
            color = "#f39c12"  # amber — generic
            label = f"Js2: {len(incidents)} incident" + ("s" if len(incidents) != 1 else "")
        elif upcoming:
            color = "#2980b9"  # blue — upcoming maintenance
            label = f"Js2: OK ({len(upcoming)} upcoming maint.)"
        else:
            color = "#27ae60"  # green
            label = "Js2: OK"
        self._js2StatusLabel.text = label
        self._js2StatusLabel.setStyleSheet(f"color: {color}; padding: 0 6px; font-weight: bold;")

        tooltip_lines = [f"Overall: {op_text}"]
        if incidents:
            tooltip_lines.append("")
            tooltip_lines.append(f"Open incidents ({len(incidents)}):")
            for inc in incidents:
                marker = "  ⚠" if inc in gpu_incidents else "  -"
                name = inc.get("name", "?")
                tooltip_lines.append(f"{marker} {name}")
                msgs = inc.get("messages") or []
                if msgs:
                    detail = (msgs[0].get("details", "") or "").strip().split("\n")[0]
                    if detail:
                        tooltip_lines.append(f"      {detail[:200]}")
        if upcoming:
            tooltip_lines.append("")
            tooltip_lines.append(f"Upcoming maintenance ({len(upcoming)}):")
            for m in upcoming[:5]:
                tooltip_lines.append(f"  - {m.get('name', '?')}  "
                                     f"({m.get('datetime_planned_start', '?')})")
        tooltip_lines.append("")
        tooltip_lines.append(f"Click to open {_JS2_STATUS_HTML}")
        self._js2StatusLabel.toolTip = "\n".join(tooltip_lines)

    # ----- refresh logic -----

    def _refreshAll(self):
        client = self._owner.getClient()
        if client is None:
            return
        try:
            self._projects = client.list_by_type("Project")
            self._annotations_by_project = {}
            # Single fetch + group is cheaper than per-project: do it once.
            all_anns = client.list_by_type("Annotation")
            by_pid = {}
            for a in all_anns:
                by_pid.setdefault(a.get("project_id"), []).append(a)
            # A revised project (v2) has predecessor=v1. Annotations stay
            # attached to whichever project_id they were originally written
            # against, so the head needs to also expose its ancestors' anns.
            proj_by_id = {p["_id"]: p for p in self._projects}
            for p in self._projects:
                merged = []
                cur = p["_id"]
                seen = set()
                while cur and cur not in seen:
                    seen.add(cur)
                    merged.extend(by_pid.get(cur, []))
                    cur = (proj_by_id.get(cur) or {}).get("predecessor")
                self._annotations_by_project[p["_id"]] = merged
            self._jobs = client.list_training_jobs()
            self._jobs_by_id = {j["_id"]: j for j in self._jobs}
        except ChronicleError as exc:
            logging.warning("dashboard refresh failed: %s", exc)
            return

        self._refreshProjectCombo()
        self._refreshReadyTable()
        self._refreshJobsTable()
        self._refreshDetail()

    def _refreshProjectCombo(self):
        current = self._projectCombo.currentData
        self._projectCombo.blockSignals(True)
        self._projectCombo.clear()
        self._projectCombo.addItem("(all projects)", None)
        for p in self._projects:
            self._projectCombo.addItem(
                f"{p.get('name', '?')} (v{p.get('version', '?')})", p["_id"])
        idx = self._projectCombo.findData(current)
        if idx >= 0:
            self._projectCombo.currentIndex = idx
        self._projectCombo.blockSignals(False)

    @staticmethod
    def _head_status_per_case(annotations):
        """For a list of Annotations within one project, return the latest
        (highest-version) status per case_id."""
        head = {}
        for a in annotations:
            cid = a.get("case_id")
            if cid is None:
                continue
            cur = head.get(cid)
            if cur is None or (a.get("version") or 0) > (cur.get("version") or 0):
                head[cid] = a
        return head

    def _approved_annotation_ids(self, project_id):
        head = self._head_status_per_case(self._annotations_by_project.get(project_id, []))
        return [a["_id"] for a in head.values() if a.get("status") == "approved"]

    def _activeJobsForProject(self, project_id):
        return [j for j in self._jobs
                if j.get("project_id") == project_id
                and j.get("status") in ("pending", "running")]

    def _projectFilterId(self):
        return self._projectCombo.currentData

    def _refreshReadyTable(self):
        filter_id = self._projectFilterId()
        rows = []
        for p in self._projects:
            if filter_id and p["_id"] != filter_id:
                continue
            approved = self._approved_annotation_ids(p["_id"])
            if not approved:
                continue
            active_jobs = self._activeJobsForProject(p["_id"])
            rows.append((p, approved, active_jobs))
        table = self._readyTable
        table.setSortingEnabled(False)
        table.rowCount = len(rows)
        for r, (p, approved, active_jobs) in enumerate(rows):
            table.setItem(r, 0, qt.QTableWidgetItem(p.get("name", "?")))
            table.setItem(r, 1, qt.QTableWidgetItem(str(len(approved))))
            table.setItem(r, 2, qt.QTableWidgetItem(
                ", ".join(j.get("name", "?") for j in active_jobs) or "—"))
            btn = qt.QPushButton("Plan training")
            btn.toolTip = ("Create a TrainingJob doc in 'pending' state with "
                           "this project's approved annotations frozen as the "
                           "training set. No Js2 instance is launched.")
            btn.clicked.connect(
                lambda _checked=False, pid=p["_id"], label=_label_from_project(p),
                       ann_ids=approved: self._onPlanTraining(pid, label, ann_ids))
            table.setCellWidget(r, 3, btn)
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(0, qt.QHeaderView.Stretch)

    def _refreshJobsTable(self):
        filter_id = self._projectFilterId()
        jobs = [j for j in self._jobs if not filter_id or j.get("project_id") == filter_id]
        table = self._jobsTable
        table.setSortingEnabled(False)
        table.rowCount = len(jobs)
        project_names = {p["_id"]: p.get("name", "?") for p in self._projects}
        now = datetime.datetime.now(datetime.timezone.utc)
        for r, j in enumerate(jobs):
            status = j.get("status", "?")
            status_item = qt.QTableWidgetItem(status)
            color = _STATUS_COLOR.get(status)
            if color:
                status_item.setBackground(qt.QBrush(color))
                status_item.setForeground(qt.QBrush(qt.QColor("white")))
            status_item.setData(qt.Qt.UserRole, j["_id"])
            table.setItem(r, 0, status_item)
            phase, _level, _hint = _interpret_phase(j, now)
            phase_item = qt.QTableWidgetItem(phase)
            table.setItem(r, 1, phase_item)
            table.setItem(r, 2, qt.QTableWidgetItem(project_names.get(j.get("project_id"), "?")))
            table.setItem(r, 3, qt.QTableWidgetItem(j.get("label", "?")))
            fold = j.get("config", {}).get("fold")
            table.setItem(r, 4, qt.QTableWidgetItem(str(fold) if fold is not None else "—"))
            # "Age" = time-since-training-started, fall back to created_at if
            # the runner hasn't published started_at yet. The doc's created_at
            # is when "Plan training" was clicked (often a day or more before
            # actual training kickoff) and so isn't actionable.
            table.setItem(r, 5, qt.QTableWidgetItem(
                _age_str(j.get("started_at") or j.get("created_at"), now)))
            table.setItem(r, 6, qt.QTableWidgetItem(_age_str(j.get("last_heartbeat_at"), now)))
            ep = j.get("current_epoch")
            tot = (j.get("config") or {}).get("total_epochs")
            table.setItem(r, 7, qt.QTableWidgetItem(_epoch_str(ep, tot)))
            table.setItem(r, 8, qt.QTableWidgetItem(_eta_str(j)))
            # Sticky metrics: cache the last non-None mean_dice so the column
            # doesn't flicker each time the runner's heartbeat writes None.
            metrics = j.get("latest_metrics") or {}
            md = metrics.get("mean_dice")
            cache = self._stickyMetrics.setdefault(j["_id"], {})
            if isinstance(md, (int, float)):
                cache["mean_dice"] = md
            md_show = cache.get("mean_dice")
            table.setItem(r, 9, qt.QTableWidgetItem(
                f"{md_show:.3f}" if isinstance(md_show, (int, float)) else "—"))
            table.setItem(r, 10, qt.QTableWidgetItem(j.get("host") or "—"))
        table.resizeColumnsToContents()
        # Restore selection
        if self._selectedJobId is not None:
            for r in range(table.rowCount):
                if table.item(r, 0).data(qt.Qt.UserRole) == self._selectedJobId:
                    table.blockSignals(True)
                    table.selectRow(r)
                    table.blockSignals(False)
                    break

    def _refreshAgeColumns(self):
        """Re-render just the Age + Heartbeat columns + the phase column +
        the feed-age label. No chronicle round-trip; uses cached self._jobs.
        Fires every 5 s via self._ageTimer so the user sees the clock advance."""
        self._refreshFeedAge()
        now = datetime.datetime.now(datetime.timezone.utc)
        filter_id = self._projectFilterId()
        jobs = [j for j in self._jobs if not filter_id or j.get("project_id") == filter_id]
        table = self._jobsTable
        for r, j in enumerate(jobs):
            if r >= table.rowCount:
                break
            phase, _level, _hint = _interpret_phase(j, now)
            if table.item(r, 1):
                table.item(r, 1).setText(phase)
            if table.item(r, 5):
                table.item(r, 5).setText(
                    _age_str(j.get("started_at") or j.get("created_at"), now))
            if table.item(r, 6):
                table.item(r, 6).setText(_age_str(j.get("last_heartbeat_at"), now))
            if table.item(r, 8):
                table.item(r, 8).setText(_eta_str(j))
        # Selected job's summary line also has age strings + phase hint.
        # Cheap — no chronicle round-trip, just re-render the existing text.
        if self._selectedJobId:
            self._rebuildDetailSummaryOnly()

    def _rebuildDetailSummaryOnly(self):
        """Just the summary header (status / phase / age / heartbeat).
        Cases table, metrics, telemetry block are NOT re-built — those only
        change on a real doc change event."""
        job = self._jobs_by_id.get(self._selectedJobId)
        if job is None:
            return
        project_name = next((p.get("name", "?") for p in self._projects
                             if p["_id"] == job.get("project_id")), "?")
        cfg = job.get("config") or {}
        now = datetime.datetime.now(datetime.timezone.utc)
        phase, level, hint = _interpret_phase(job, now)
        phase_color = {"ok": "#27ae60", "warning": "#f39c12",
                       "bad": "#c0392b", "neutral": "#7f8c8d"}.get(level, "#7f8c8d")
        self._detailSummary.text = (
            f"<b>{job.get('name', '?')}</b>"
            f"<br>project: {project_name}"
            f" &nbsp;|&nbsp; config: {cfg.get('framework', '?')} / "
            f"{cfg.get('config_name', '?')}"
            f" &nbsp;|&nbsp; status: <b>{job.get('status', '?')}</b>"
            f" &nbsp;|&nbsp; <span style='color:{phase_color}; font-weight:bold;'>"
            f"phase: {phase}</span>"
            f"<br>created: {_short_iso(job.get('created_at')) or '—'}"
            f" ({_age_str(job.get('created_at'), now)})"
            f" &nbsp;|&nbsp; started: {_short_iso(job.get('started_at')) or '—'}"
            f"{(' (' + _age_str(job.get('started_at'), now) + ')') if job.get('started_at') else ''}"
            f" &nbsp;|&nbsp; heartbeat: {_age_str(job.get('last_heartbeat_at'), now) or '—'}"
            f"<br><span style='color:{phase_color};'>{hint}</span>"
        )

    def _onJobSelectionChanged(self):
        items = self._jobsTable.selectedItems()
        if not items:
            self._selectedJobId = None
        else:
            self._selectedJobId = items[0].data(qt.Qt.UserRole)
        self._refreshDetail()

    def _refreshDetail(self):
        job = self._jobs_by_id.get(self._selectedJobId) if self._selectedJobId else None
        if job is None:
            self._detailSummary.text = "(no job selected)"
            for lbl in self._metricLabels.values():
                lbl.text = "—"
            self._casesTable.rowCount = 0
            self._validationCaseCombo.clear()
            self._validationCaseCombo.enabled = False
            self._loadValidationButton.enabled = False
            self._openProgressButton.enabled = False
            self._liveStateRows = []
            self._renderLiveState()
            return

        project_name = next((p.get("name", "?") for p in self._projects
                             if p["_id"] == job.get("project_id")), "?")
        cfg = job.get("config") or {}
        now = datetime.datetime.now(datetime.timezone.utc)
        phase, level, hint = _interpret_phase(job, now)
        phase_color = {"ok": "#27ae60", "warning": "#f39c12",
                       "bad": "#c0392b", "neutral": "#7f8c8d"}.get(level, "#7f8c8d")
        # Compact summary header.
        self._detailSummary.text = (
            f"<b>{job.get('name', '?')}</b>"
            f"<br>project: {project_name}"
            f" &nbsp;|&nbsp; config: {cfg.get('framework', '?')} / "
            f"{cfg.get('config_name', '?')}"
            f" &nbsp;|&nbsp; status: <b>{job.get('status', '?')}</b>"
            f" &nbsp;|&nbsp; <span style='color:{phase_color}; font-weight:bold;'>"
            f"phase: {phase}</span>"
            f"<br>created: {_short_iso(job.get('created_at')) or '—'}"
            f" ({_age_str(job.get('created_at'), now)})"
            f" &nbsp;|&nbsp; started: {_short_iso(job.get('started_at')) or '—'}"
            f"{(' (' + _age_str(job.get('started_at'), now) + ')') if job.get('started_at') else ''}"
            f" &nbsp;|&nbsp; heartbeat: {_age_str(job.get('last_heartbeat_at'), now) or '—'}"
            f"<br><span style='color:{phase_color};'>{hint}</span>"
        )

        metrics = job.get("latest_metrics") or {}
        ep = job.get("current_epoch")
        tot = cfg.get("total_epochs")
        self._metricLabels["epoch"].text = _epoch_str(ep, tot)
        for key in ("train_loss", "val_loss", "mean_dice"):
            v = metrics.get(key)
            self._metricLabels[key].text = (f"{v:.4f}" if isinstance(v, (int, float)) else "—")
        self._metricLabels["eta"].text = _eta_str(job)

        # Cases table — pull the approved annotations for this job's set.
        client = self._owner.getClient()
        ann_ids = set(job.get("training_annotation_ids") or [])
        cases_rows = []
        if client is not None:
            project_anns = self._annotations_by_project.get(job.get("project_id"), [])
            for a in project_anns:
                if a["_id"] not in ann_ids:
                    continue
                cases_rows.append(a)
        # Resolve cohort metadata for sex/primary_site per case.
        resolution = None
        try:
            if client is not None:
                project = next((p for p in self._projects if p["_id"] == job["project_id"]), None)
                if project:
                    resolution = client.latest_cohort_resolution(project["cohort_id"])
        except Exception:
            resolution = None
        cases_meta = {}
        if resolution:
            for c in resolution.get("cases", []):
                cases_meta[c["case_id"]] = c
        # Sort cases by case_id so the table + combo are predictable.
        cases_rows = sorted(cases_rows, key=lambda a: a.get("case_id") or "")
        self._casesTable.rowCount = len(cases_rows)
        # Repopulate the validation combo, but preserve the user's current
        # pick across refreshes. _refreshDetail fires on every chronicle
        # change (observer polls every 15 s) — without preservation, any
        # selection gets snapped back to whatever the first item happens to
        # be. Block signals so the clear+repopulate doesn't spuriously
        # currentIndexChanged anything wired downstream.
        prior_choice = self._validationCaseCombo.currentData
        self._validationCaseCombo.blockSignals(True)
        self._validationCaseCombo.clear()
        for r, a in enumerate(cases_rows):
            cid = a.get("case_id", "?")
            meta = cases_meta.get(cid, {})
            producer_label = (a.get("producer") or {}).get("label") or ""
            reviewer, review_text = _parse_reviewer_from_notes(a.get("notes") or "")
            self._casesTable.setItem(r, 0, qt.QTableWidgetItem(cid))
            self._casesTable.setItem(r, 1, qt.QTableWidgetItem((meta.get("sex") or "").strip()))
            self._casesTable.setItem(r, 2, qt.QTableWidgetItem(meta.get("primary_site") or ""))
            self._casesTable.setItem(r, 3, qt.QTableWidgetItem(producer_label))
            self._casesTable.setItem(r, 4, qt.QTableWidgetItem(reviewer))
            notes_item = qt.QTableWidgetItem(review_text)
            if review_text:
                notes_item.setToolTip(review_text)
            self._casesTable.setItem(r, 5, notes_item)
            self._validationCaseCombo.addItem(cid, cid)
        # Restore the prior pick if it's still in the list.
        if prior_choice:
            idx = self._validationCaseCombo.findData(prior_choice)
            if idx >= 0:
                self._validationCaseCombo.currentIndex = idx
        self._validationCaseCombo.blockSignals(False)
        self._casesTable.resizeColumnsToContents()
        self._casesTable.horizontalHeader().setSectionResizeMode(5, qt.QHeaderView.Stretch)

        # Render a combined "live state" block: events timeline + openstack
        # snapshot + VM-side runtime_telemetry. All three sources flow into
        # the same TrainingJob doc so the changes feed updates them in lockstep.
        # Each row carries an anchor timestamp; _renderLiveState colour-fades
        # recently arrived rows from green to black over 15 s. The 1 s timer
        # keeps that fade animating between doc updates.
        self._liveStateRows = _collect_live_state_rows(job)
        self._renderLiveState()

        # Action buttons.
        self._openProgressButton.enabled = bool(job.get("log_ref"))
        self._validationCaseCombo.enabled = bool(cases_rows)
        self._loadValidationButton.enabled = bool(cases_rows)

    # ----- actions -----

    def _onPlanTraining(self, project_id, label, annotation_ids):
        client = self._owner.getClient()
        if client is None:
            return
        # nnU-Net v2 default is 5-fold cross-validation. Create one TrainingJob
        # per fold so each can land on its own GPU box and report independently.
        # The single-fold case is just folds=[0]; we keep that as a manual
        # follow-up if the user wants to redo one fold later.
        folds = [0, 1, 2, 3, 4]
        created = []
        for fold in folds:
            try:
                doc = client.create_training_job(
                    project_id=project_id,
                    label=label,
                    training_annotation_ids=annotation_ids,
                    config={
                        "framework": "nnUNetv2",
                        "planner": "nnUNetPlannerResEncM",
                        "plans_identifier": "nnUNetResEncUNetMPlans",
                        "config_name": "3d_fullres",
                        "fold": fold,
                        "dataset_id": 1,
                        "task_name": _task_name_from_label(label),
                        "total_epochs": 1000,
                    },
                    status="pending",
                )
                created.append(doc["_id"])
            except ChronicleError as exc:
                slicer.util.errorDisplay(
                    f"Could not create TrainingJob for fold {fold}: {exc.reason}")
                break
        if created:
            self._selectedJobId = created[0]
        self._refreshAll()

    def _onOpenProgress(self):
        slicer.util.infoDisplay(
            "Progress.png viewer wires through SSH/Swift to the trainer's "
            "nnUNet_results dir. Plumbing exists; reachable when a trainer "
            "instance is published in log_ref. Not implemented yet.")

    # ----- validation sequence scrub loader -----

    def _onLoadValidationSequence(self):
        """For the selected TrainingJob + case: scp the per-epoch validation
        snapshots down from the trainer instance (created by
        bin/lnq-val-snapshotter.py), load them as a Slicer Sequence, and arm a
        SequenceBrowser pinned to the CT so the user can scrub epoch-by-epoch.

        Uses the trainer's internal IP from openstack_state (the observer
        keeps that fresh). SSH goes via the chronicle FIP as jump host; the
        ProxyCommand wraps the second hop. Falls back gracefully if Manila
        isn't reachable or no snapshots have been written yet."""
        import os, re, glob, subprocess, uuid
        job = self._jobs_by_id.get(self._selectedJobId)
        if job is None:
            slicer.util.errorDisplay("No job selected"); return
        case_id = self._validationCaseCombo.currentData
        if not case_id:
            slicer.util.errorDisplay("Pick a case from the combo first."); return

        cfg = job.get("config") or {}
        fold = cfg.get("fold", 0)
        dataset_id = cfg.get("dataset_id", 1)
        task_name = cfg.get("task_name") or "LNQinguinal"
        plans = cfg.get("plans_identifier") or "nnUNetResEncUNetMPlans"
        config_name = cfg.get("config_name") or "3d_fullres"
        trainer_class = "nnUNetTrainer"
        nnunet_base = cfg.get("training_workspace") or "/media/share/LNQ-data/Inguinal_Syed"

        # Resolve the case's CT via the chronicle so the SEG sequence has
        # something to overlay on. resolve_ref handles both blob_id-keyed and
        # legacy local-uri refs, including the equivalent-volume rewrite that
        # points at the Manila copy when the Mac copy isn't present.
        client = self._owner.getClient()
        ct_path = None
        if client is not None:
            try:
                proj = next((p for p in self._projects
                             if p["_id"] == job.get("project_id")), None)
                if proj is not None:
                    reso = client.latest_cohort_resolution(proj["cohort_id"])
                    case = next((c for c in (reso or {}).get("cases", [])
                                 if c.get("case_id") == case_id), None)
                    if case is not None:
                        ct_path = client.resolve_ref(case.get("ct_ref"))
            except Exception as exc:
                logging.warning("CT ref resolve failed: %s", exc)
        if ct_path and not os.path.exists(ct_path):
            ct_path = None

        # Internal IP from the observer-maintained openstack_state.
        addrs = ((job.get("openstack_state") or {}).get("server") or {}).get("addresses") or {}
        internal_ip = None
        for ips in addrs.values():
            for a in ips:
                if a.startswith("10."):
                    internal_ip = a; break
            if internal_ip: break
        if not internal_ip:
            slicer.util.errorDisplay(
                "No internal IP for this trainer yet — observer hasn't polled, "
                "or the instance has been destroyed."); return

        # Reach into trainer.conf for SSH key + jump host. Same conf trainer.sh uses.
        trainer_conf = os.path.expanduser(
            "~/slicer/latest/SlicerLNQ-Chronicler/trainer.conf")
        if not os.path.isfile(trainer_conf):
            slicer.util.errorDisplay(f"trainer.conf not found at {trainer_conf}"); return
        conf = {}
        for ln in open(trainer_conf):
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln: continue
            k, _, v = ln.partition("=")
            conf[k.strip()] = v.strip().strip('"').strip("'")
        ssh_key = os.path.expandvars(os.path.expanduser(conf.get("SSH_KEY", "")))
        ssh_user = conf.get("SSH_USER", "ubuntu")
        jump_user = conf.get("JUMP_USER", "ubuntu")
        jump_host = conf.get("JUMP_HOST")
        if not (ssh_key and jump_host and os.path.isfile(ssh_key)):
            slicer.util.errorDisplay("SSH_KEY/JUMP_HOST missing in trainer.conf"); return

        # Slicer's PYTHONHOME would corrupt openssh's child shell; clean env.
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith(("PYTHON", "LD_LIBRARY", "DYLD_"))}
        clean_env.setdefault("HOME", os.environ["HOME"])

        proxy = (f"ssh -i {ssh_key} -W %h:%p -o StrictHostKeyChecking=accept-new "
                 f"-o ConnectTimeout=10 {jump_user}@{jump_host}")
        ssh_args = ["-i", ssh_key,
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-o", "ConnectTimeout=10",
                    "-o", f"ProxyCommand={proxy}"]

        ds_dirname = f"Dataset{int(dataset_id):03d}_{task_name}"
        plans_dirname = f"{trainer_class}__{plans}__{config_name}"
        remote_dir = (
            f"{nnunet_base}/nnUNet_results/{ds_dirname}/"
            f"{plans_dirname}/fold_{fold}/validation_raw")

        # 1. Enumerate snapshots on the remote side.
        list_cmd = (["ssh"] + ssh_args
                    + [f"{ssh_user}@{internal_ip}",
                       f"ls -1 {remote_dir}/{case_id}_epoch_*.nrrd 2>/dev/null"])
        try:
            r = subprocess.run(list_cmd, capture_output=True, text=True,
                               env=clean_env, timeout=30)
        except Exception as exc:
            slicer.util.errorDisplay(f"ssh ls failed: {exc}"); return
        remote_files = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        if not remote_files:
            slicer.util.infoDisplay(
                f"No snapshots yet for {case_id} in fold {fold}. The snapshotter "
                f"writes every Nth epoch; check that lnq-val-snapshotter.py is "
                f"running on the trainer, and that the case is in the fold's val "
                f"split."); return

        # 2. SCP them down to a fresh local temp dir.
        local_dir = f"/tmp/lnq-scrub-fold{fold}-{case_id}-{uuid.uuid4().hex[:8]}"
        os.makedirs(local_dir, exist_ok=True)
        scp_src = f"{ssh_user}@{internal_ip}:{remote_dir}/{case_id}_epoch_*.nrrd"
        scp_cmd = ["scp"] + ssh_args + ["-q", scp_src, local_dir + "/"]
        try:
            subprocess.run(scp_cmd, env=clean_env, timeout=600, check=True)
        except subprocess.CalledProcessError as exc:
            slicer.util.errorDisplay(f"scp failed (rc={exc.returncode}); "
                                      f"check connectivity to {internal_ip}"); return
        except Exception as exc:
            slicer.util.errorDisplay(f"scp failed: {exc}"); return

        # 3. Sort by epoch and build a Slicer Sequence.
        local_files = sorted(
            glob.glob(os.path.join(local_dir, f"{case_id}_epoch_*.nrrd")))
        if not local_files:
            slicer.util.errorDisplay(f"No files in {local_dir} after scp"); return

        epoch_of = lambda p: int(re.search(r"_epoch_(\d+)\.nrrd$", p).group(1))
        local_files.sort(key=epoch_of)

        # CT first so the SEG overlay has a backdrop. Set a sensible CT window
        # so soft-tissue contrast is visible immediately.
        if ct_path:
            ct_node = slicer.util.loadVolume(ct_path)
            if ct_node is not None:
                ct_node.SetName(f"{case_id} CT")
                disp = ct_node.GetDisplayNode()
                if disp is not None:
                    disp.SetAutoWindowLevel(False)
                    disp.SetWindow(350.0)
                    disp.SetLevel(40.0)

        seq_name = f"{case_id}_fold{fold}_epochs"
        seq = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLSequenceNode", seq_name)
        seq.SetIndexName("epoch")
        seq.SetIndexUnit("")
        seq.SetIndexType(slicer.vtkMRMLSequenceNode.NumericIndex)

        for f in local_files:
            ep = epoch_of(f)
            seg_node = slicer.util.loadSegmentation(f)
            if seg_node is None: continue
            seg_node.SetName(f"{case_id}_e{ep:05d}")
            # Closed-surface so it shows up in 3D too.
            seg_node.CreateClosedSurfaceRepresentation()
            seq.SetDataNodeAtValue(seg_node, str(ep))
            slicer.mrmlScene.RemoveNode(seg_node)
        print(f"[scrub] {seq_name}: {len(local_files)} epoch snapshots", flush=True)

        # 4. Sequence browser pinned to the sequence so the user can scrub.
        browser = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLSequenceBrowserNode", f"scrub_{seq_name}")
        browser.SetAndObserveMasterSequenceNodeID(seq.GetID())
        # Show the browser toolbar so the slider is right there.
        slicer.modules.sequences.toolBar().setActiveBrowserNode(browser)
        slicer.util.infoDisplay(
            f"Loaded {len(local_files)} epoch snapshots for {case_id} (fold {fold}). "
            f"Use the Sequences toolbar slider to scrub.")

    # ----- training-metrics chart (qSlicerWebWidget + Apache ECharts) -----

    def _refreshMetricsChart(self):
        """Pull <fold_dir>/validation_raw/metrics.jsonl from the trainer and
        render it as an interactive ECharts line chart. Uses the same SSH
        plumbing as the scrub loader."""
        import json, os, subprocess
        job = self._jobs_by_id.get(self._selectedJobId)
        if job is None:
            self._metricsChartWidget.setHtml(
                "<body style='padding:20px; color:#777'>No job selected.</body>")
            return
        cfg = job.get("config") or {}
        fold = cfg.get("fold", 0)
        dataset_id = cfg.get("dataset_id", 1)
        task_name = cfg.get("task_name") or "LNQinguinal"
        plans = cfg.get("plans_identifier") or "nnUNetResEncUNetMPlans"
        config_name = cfg.get("config_name") or "3d_fullres"
        trainer_class = "nnUNetTrainer"
        nnunet_base = cfg.get("training_workspace") or "/media/share/LNQ-data/Inguinal_Syed"

        addrs = ((job.get("openstack_state") or {}).get("server") or {}).get("addresses") or {}
        internal_ip = None
        for ips in addrs.values():
            for a in ips:
                if a.startswith("10."):
                    internal_ip = a; break
            if internal_ip: break
        if not internal_ip:
            self._metricsChartWidget.setHtml(
                "<body style='padding:20px; color:#777'>"
                "Trainer has no internal IP in openstack_state yet.</body>")
            return

        trainer_conf = os.path.expanduser(
            "~/slicer/latest/SlicerLNQ-Chronicler/trainer.conf")
        if not os.path.isfile(trainer_conf):
            self._metricsChartWidget.setHtml(
                f"<body style='padding:20px; color:#a33'>"
                f"Missing trainer.conf at {trainer_conf}</body>")
            return
        conf = {}
        for ln in open(trainer_conf):
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln: continue
            k, _, v = ln.partition("=")
            conf[k.strip()] = v.strip().strip('"').strip("'")
        ssh_key = os.path.expandvars(os.path.expanduser(conf.get("SSH_KEY", "")))
        ssh_user = conf.get("SSH_USER", "ubuntu")
        jump_user = conf.get("JUMP_USER", "ubuntu")
        jump_host = conf.get("JUMP_HOST")
        if not (ssh_key and jump_host and os.path.isfile(ssh_key)):
            self._metricsChartWidget.setHtml(
                "<body style='padding:20px; color:#a33'>SSH_KEY/JUMP_HOST missing.</body>")
            return
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith(("PYTHON", "LD_LIBRARY", "DYLD_"))}
        clean_env.setdefault("HOME", os.environ["HOME"])
        proxy = (f"ssh -i {ssh_key} -W %h:%p -o StrictHostKeyChecking=accept-new "
                 f"-o ConnectTimeout=10 {jump_user}@{jump_host}")
        ssh_args = ["-i", ssh_key,
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-o", "ConnectTimeout=10",
                    "-o", f"ProxyCommand={proxy}"]

        ds_dirname = f"Dataset{int(dataset_id):03d}_{task_name}"
        plans_dirname = f"{trainer_class}__{plans}__{config_name}"
        remote_path = (
            f"{nnunet_base}/nnUNet_results/{ds_dirname}/"
            f"{plans_dirname}/fold_{fold}/validation_raw/metrics.jsonl")

        # SSH-cat the JSONL (it's small — kilobytes).
        cat_cmd = (["ssh"] + ssh_args
                   + [f"{ssh_user}@{internal_ip}",
                      f"sudo cat {remote_path} 2>/dev/null"])
        try:
            r = subprocess.run(cat_cmd, capture_output=True, text=True,
                               env=clean_env, timeout=30)
        except Exception as exc:
            self._metricsChartWidget.setHtml(
                f"<body style='padding:20px; color:#a33'>SSH cat failed: {exc}</body>")
            return
        records = []
        for ln in (r.stdout or "").splitlines():
            ln = ln.strip()
            if not ln: continue
            try: records.append(json.loads(ln))
            except Exception: continue
        if not records:
            self._metricsChartWidget.setHtml(
                "<body style='padding:20px; color:#777'>"
                "No metrics.jsonl yet on the trainer. The snapshotter writes it "
                "after each predict cycle completes.</body>")
            return

        y_choice = self._metricChoiceCombo.currentData or "dice"
        html = self._buildMetricsChartHtml(records, y_choice, fold)
        self._metricsChartWidget.setHtml(html)

    @staticmethod
    def _buildMetricsChartHtml(records, y_key, fold):
        """Group records by case_id and build ECharts series. y_key drives which
        column ends up on the Y axis; epoch is always X. We also overlay a
        thicker dashed line for EMA Dice (per-epoch, fold-level scalar) when
        the y_key is Dice — that anchors the per-case lines to the value
        nnU-Net itself uses to pick checkpoint_best."""
        import json, math
        by_case = {}
        ema_by_epoch = {}
        for r in records:
            cid = r.get("case_id"); ep = r.get("epoch")
            if cid is None or ep is None: continue
            by_case.setdefault(cid, []).append(r)
            if r.get("ema_dice") is not None and ep not in ema_by_epoch:
                ema_by_epoch[ep] = float(r["ema_dice"])

        def y_value(rec):
            if y_key == "volume_residual_ml":
                p = rec.get("pred_volume_ml"); g = rec.get("gt_volume_ml")
                if p is None or g is None: return None
                return p - g
            if y_key == "lr_log":
                v = rec.get("lr")
                if v is None or v <= 0: return None
                return math.log10(v)
            v = rec.get(y_key)
            return None if v is None else float(v)

        series = []
        for cid in sorted(by_case.keys()):
            rs = sorted(by_case[cid], key=lambda x: x["epoch"])
            data = []
            for r in rs:
                y = y_value(r)
                if y is None: continue
                data.append([r["epoch"], y])
            if data:
                series.append({
                    "name": cid,
                    "type": "line",
                    "showSymbol": True,
                    "symbolSize": 6,
                    "data": data,
                    "smooth": False,
                })
        # Overlay EMA Dice when relevant.
        if y_key == "dice" and ema_by_epoch:
            series.append({
                "name": "EMA Dice (fold)",
                "type": "line",
                "data": sorted([[e, v] for e, v in ema_by_epoch.items()]),
                "lineStyle": {"type": "dashed", "width": 3, "color": "#222"},
                "itemStyle": {"color": "#222"},
                "symbol": "none",
            })

        legend_names = [s["name"] for s in series]
        y_axis = {
            "type": "value", "name": y_key,
            "nameLocation": "middle", "nameGap": 40,
            "axisLabel": {"color": "#333"},
            "splitLine": {"lineStyle": {"color": "#eee"}},
        }
        if y_key == "dice":
            y_axis["min"] = 0.0; y_axis["max"] = 1.0

        page_html = """<!doctype html>
<html><body style="margin:0; padding:0; background:#fff; font-family: -apple-system, sans-serif;">
<div id="container" style="width:100%; height:360px;"></div>
<script src="https://fastly.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script>
const dom = document.getElementById('container');
const chart = echarts.init(dom, null, { renderer: 'canvas' });
chart.setOption({
  title: { text: 'fold %%FOLD%% — %%YKEY%% over epoch',
           left: 'center', top: 6,
           textStyle: { fontSize: 13, fontWeight: 'normal' } },
  tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
  legend: { top: 28, type: 'scroll', data: %%LEGEND%% },
  grid: { left: 60, right: 30, top: 70, bottom: 40 },
  xAxis: { type: 'value', name: 'epoch', nameLocation: 'middle', nameGap: 25,
           axisLabel: { color: '#333' },
           splitLine: { lineStyle: { color: '#eee' } } },
  yAxis: %%YAXIS%%,
  series: %%SERIES%%,
});
window.addEventListener('resize', () => chart.resize());
// Click a point → load that epoch's snapshot for that case in the scrub view.
chart.on('click', (params) => {
  if (window.slicerPython && params.componentType === 'series') {
    const cid = params.seriesName;
    const ep = params.data[0];
    window.slicerPython.evalPython(
      `slicer.modules.LNQStudioWidget._dashboardWindow._loadOneSnapshot(${ep}, '${cid}')`);
  }
});
</script>
</body></html>"""
        html = (page_html
                .replace("%%FOLD%%", str(fold))
                .replace("%%YKEY%%", y_key)
                .replace("%%LEGEND%%", json.dumps(legend_names))
                .replace("%%YAXIS%%", json.dumps(y_axis))
                .replace("%%SERIES%%", json.dumps(series)))
        return html

    def _loadOneSnapshot(self, epoch, case_id):
        """Called from the ECharts chart's click handler via window.slicerPython.
        Picks the case in the combo and triggers the scrub loader — which now
        only has one frame, but lets the user inspect that exact epoch."""
        idx = self._validationCaseCombo.findData(case_id)
        if idx >= 0:
            self._validationCaseCombo.currentIndex = idx
        # Reuse the existing loader.
        self._onLoadValidationSequence()

    # ----- live state rendering / pop-out -----

    def _renderLiveState(self):
        """Convert self._liveStateRows -> coloured HTML and push it into the
        embedded view + (if open) the pop-out window. Called whenever the
        backing job doc changes AND on a 1 s timer so the green→black fade
        animates smoothly between doc updates."""
        rows = self._liveStateRows or []
        now = datetime.datetime.now(datetime.timezone.utc)
        html = _render_live_state_html(rows, now)
        for view in (self._telemetryText, self._liveStateWindowView):
            if view is None:
                continue
            sb = view.verticalScrollBar()
            # PythonQt exposes Qt properties (value, maximum) as attributes,
            # not callables — parens here would raise 'int is not callable'.
            prev = sb.value
            at_bottom = (prev >= sb.maximum - 4)
            view.setHtml(html)
            # Preserve the user's read position; auto-tail if they were pinned
            # to the bottom (the usual "watching live output" mode).
            sb.setValue(sb.maximum if at_bottom else prev)

    def _onPopOutLiveState(self):
        """Open (or re-show) a separate window mirroring the live state log.
        Sized large for a second monitor; user can move/resize freely."""
        if self._liveStateWindow is None:
            win = qt.QWidget()
            win.setWindowTitle("LNQ — Live state")
            winLayout = qt.QVBoxLayout(win)
            view = qt.QTextEdit()
            view.readOnly = True
            font = qt.QFont("Menlo")
            font.setStyleHint(qt.QFont.Monospace)
            view.setFont(font)
            winLayout.addWidget(view)
            win.resize(960, 640)
            self._liveStateWindow = win
            self._liveStateWindowView = view
        self._liveStateWindow.show()
        self._liveStateWindow.raise_()
        self._liveStateWindow.activateWindow()
        self._renderLiveState()


def _interpret_phase(job, now):
    """Plain-language interpretation of where a job is in its lifecycle,
    derived from status + heartbeat + runtime_telemetry. Returns (phase,
    level, hint). level is one of ok | warning | bad | neutral."""
    status = job.get("status")
    hb = job.get("last_heartbeat_at")
    rt = job.get("runtime_telemetry") or {}
    ci = (rt.get("cloud_init") or {}).get("status")
    gpu = rt.get("gpu") or {}
    gpu_ok = gpu.get("ok")
    svc = (rt.get("service") or {}).get("active")
    ep = job.get("current_epoch")

    if status == "converged":
        return ("complete", "ok", "Model registered; ready for inference.")
    if status == "failed":
        notes_head = (job.get("notes") or "").split("\n")[0][:160]
        return ("failed", "bad", f"Failed: {notes_head}")
    if status == "cancelled":
        return ("cancelled", "neutral", "Run was cancelled.")

    # status in {pending, running}
    if not hb:
        return ("waiting for instance",
                "neutral",
                "No heartbeat yet. Either no instance has been launched for "
                "this job, OR cloud-init is in its first ~5 min installing "
                "CUDA + Python + nnU-Net. First telemetry typically lands "
                "2–5 min after `bin/trainer.sh create`.")
    try:
        hb_dt = datetime.datetime.fromisoformat(hb.replace("Z", "+00:00"))
        hb_age_s = (now - hb_dt).total_seconds()
    except Exception:
        hb_age_s = None

    if hb_age_s is not None and hb_age_s > 5 * 60:
        return ("STALE (no heartbeat)",
                "bad",
                f"Last heartbeat {int(hb_age_s/60)} min ago. The runner may "
                f"be hung or the instance unreachable. Check telemetry below.")

    if ci and ci != "done":
        return (f"cloud-init: {ci}",
                "neutral",
                "Instance is alive (telemetry arriving) but cloud-init is "
                "still running. Installing CUDA + driver + Python venv + "
                "nnU-Net. Total ~5–10 min from create. Reboot follows.")
    if not gpu_ok:
        err = (gpu.get("error") or "").split("\n")[0][:160]
        return ("GPU not ready",
                "warning",
                f"cloud-init done but driver not bound: {err}. "
                "Usually clears within 1–2 min of the reboot — if it "
                "persists past 5 min, the flavor's MIG drivers may be "
                "broken (see Js2 status badge).")
    if svc != "active":
        return ("runner not started",
                "warning",
                "GPU healthy but the trainer-runner systemd service isn't "
                "active. Should auto-start within 30 s; if persistent, "
                "service may have crashed — check the journal tail below.")
    if status == "pending":
        return ("runner starting",
                "neutral",
                "All systems go; runner should PUT status=running and emit "
                "first epoch within ~30 s.")
    # running
    if ep is None:
        return ("preprocessing",
                "neutral",
                "Running nnUNetv2_plan_and_preprocess. One-time per dataset, "
                "~5–15 min. After this, training epochs start.")
    # Epoch progress is its own column; don't duplicate it here.
    cfg = job.get("config") or {}
    return ("training",
            "ok",
            f"Training fold {cfg.get('fold')}. Metrics update every ~30 s.")


def _collect_live_state_rows(job):
    """Return [(text, anchor_iso_or_None)] for the live-state block.

    Each anchor is the ISO timestamp the row originated at. The renderer fades
    its colour from green to black as anchor_age → 15 s. Rows with anchor=None
    (section headers, fallback text) stay black and do not pulse.

    Order is most-actionable-first:
       1. recent events  (each event carries its own anchor → independent fade)
       2. openstack snapshot  (all rows share polled_at — re-flashes green when
          the Mac-side observer puts the next snapshot, every ~15 s)
       3. VM-side runtime_telemetry  (rows share t['at'] — re-flashes every ~60 s)
    """
    rows = []
    # ----- events timeline -----
    events = job.get("runtime_events") or []
    if events:
        rows.append((f"=== runtime events (last 30 of {len(events)}) ===", None))
        for e in events[-30:]:
            ts = (e.get("at") or "?")[-9:-1] if e.get("at") else "?"
            src = (e.get("source") or "?")[:10]
            msg = e.get("msg") or ""
            rows.append((f"  {ts}  [{src:<10}] {msg}", e.get("at")))
        rows.append(("", None))
    # ----- openstack snapshot -----
    osstate = job.get("openstack_state") or {}
    if osstate:
        polled = osstate.get("polled_at")
        srv = osstate.get("server") or {}
        fip = osstate.get("fip") or {}
        rows.append((f"=== openstack snapshot (polled {polled or '?'}) ===", None))
        if srv.get("error"):
            rows.append((f"  ! {srv['error']}", polled))
        else:
            rows.append((f"  server: {srv.get('name', '?')}  status={srv.get('status', '?')}"
                         f"  vm_state={srv.get('vm_state', '?')}  "
                         f"power={srv.get('power_state', '?')}", polled))
            if srv.get("task_state"):
                rows.append((f"  task: {srv['task_state']}", polled))
            rows.append((f"  flavor: {srv.get('flavor', '?')}  "
                         f"launched_at: {srv.get('launched_at', '?')}", polled))
            for net, ips in (srv.get("addresses") or {}).items():
                rows.append((f"  addr: {net} = {', '.join(ips)}", polled))
            if srv.get("floating_ip"):
                rows.append((f"  floating ip: {srv['floating_ip']}", polled))
        if fip:
            rows.append((f"  fip {fip.get('address', '?')}: status={fip.get('status', '?')}"
                         f"  port_id={(fip.get('port_id') or '∅')[:12]}…"
                         f"  fixed={fip.get('fixed_ip_address', '?')}", polled))
        ctail = osstate.get("console_log_tail") or []
        if ctail:
            rows.append((f"  --- console log tail (last {len(ctail)}) ---", None))
            for ln in ctail[-15:]:
                rows.append((f"    {ln[:200]}", polled))
        rows.append(("", None))
    # ----- VM-side telemetry -----
    t = job.get("runtime_telemetry") or {}
    if not t:
        rows.append(("=== VM telemetry === (none yet)", None))
    else:
        tel_at = t.get("at")
        for ln in _format_telemetry(t).splitlines():
            rows.append((ln, tel_at))
    if not rows:
        rows.append(("(no observations yet — start `bin/trainer-observe.py` for "
                     "openstack-side state, and the in-VM trainer-health daemon "
                     "auto-publishes once cloud-init brings it up)", None))
    return rows


def _fade_color(anchor_iso, now, fade_seconds=15.0):
    """Linear interpolate #27ae60 (green) → #000000 (black) over fade_seconds.
    Anchors without a timestamp render black."""
    if not anchor_iso:
        return "#000000"
    try:
        a = datetime.datetime.fromisoformat(anchor_iso.replace("Z", "+00:00"))
    except Exception:
        return "#000000"
    age = (now - a).total_seconds()
    if age <= 0:
        return "#27ae60"
    if age >= fade_seconds:
        return "#000000"
    t = age / fade_seconds
    r = int(39 * (1.0 - t))
    g = int(174 * (1.0 - t))
    b = int(96 * (1.0 - t))
    return f"#{r:02x}{g:02x}{b:02x}"


def _render_live_state_html(rows, now):
    """Build the HTML body for a QTextEdit. Each row is wrapped in a <span>
    coloured by its anchor age. <pre> preserves whitespace so column alignment
    in the original text-table rows is kept."""
    import html as _html
    lines = ["<pre style=\"margin:0; font-family: Menlo, monospace;\">"]
    for text, anchor in rows:
        color = _fade_color(anchor, now)
        safe = _html.escape(text)
        if not safe:
            safe = " "  # keep the blank line visible inside the <pre>
        lines.append(f'<span style="color: {color};">{safe}</span>')
    lines.append("</pre>")
    return "\n".join(lines)


def _format_telemetry(t):
    """Render the runtime_telemetry dict written by trainer-health.py as a
    compact monospace block for the dashboard. Designed to make a bad state
    (driver missing, mount gone, service dead) visible at a glance."""
    if not t or not isinstance(t, dict):
        return "(no telemetry yet — instance hasn't reported)"
    lines = []
    host = t.get("host") or {}
    lines.append(
        f"host: {host.get('hostname', '?')}  "
        f"uptime: {host.get('uptime_seconds', '?')}s  "
        f"kernel: {host.get('kernel', '?')}  "
        f"reported: {t.get('at', '?')}"
    )
    ci = t.get("cloud_init") or {}
    ci_line = f"cloud-init: {ci.get('status', '?')}"
    if ci.get("errors"):
        ci_line += f"  errors={len(ci['errors'])}"
    lines.append(ci_line)
    manila = t.get("manila") or {}
    mark = "OK" if manila.get("mounted") else "NOT MOUNTED"
    lines.append(f"manila ({manila.get('mount_point', '?')}): {mark}")
    gpu = t.get("gpu") or {}
    if gpu.get("ok"):
        devs = gpu.get("devices") or []
        if devs:
            d = devs[0]
            lines.append(
                f"gpu: OK  {d.get('name')}  driver {d.get('driver_version')}  "
                f"mem {d.get('memory_used_mb', '?')}/{d.get('memory_total_mb', '?')} MB  "
                f"util {d.get('gpu_util_pct', '?')}%"
            )
        else:
            lines.append("gpu: OK  (no devices reported)")
    else:
        err = (gpu.get("error") or "")[:140] or "(no error string)"
        lines.append(f"gpu: FAIL  {err}")
        if gpu.get("dev_nodes"):
            lines.append(f"     /dev: {gpu['dev_nodes']}")
        if gpu.get("lsmod"):
            lines.append(f"     lsmod: {gpu['lsmod']}")
    svc = t.get("service") or {}
    lines.append(f"service {svc.get('unit', 'lnq-trainer-runner.service')}: {svc.get('active', '?')}")
    for c in svc.get("condition_lines") or []:
        lines.append(f"     {c}")
    # Less-actionable, mostly-static blocks first; runner journal LAST so the
    # widget's auto-tail-to-bottom behaviour lands on what the user actually
    # cares about during a live run (the trainer process output).
    if ci.get("tail"):
        lines.append("--- recent cloud-init ---")
        for ln in ci["tail"][-5:]:
            lines.append(f"  {ln[:240]}")
    apt = t.get("apt") or {}
    if apt.get("term"):
        lines.append("--- apt term.log (last 10) ---")
        for ln in apt["term"][-10:]:
            lines.append(f"  {ln[:240]}")
    sj = t.get("system_journal") or {}
    sj_tail = sj.get("tail") if sj.get("available") else None
    if sj_tail:
        lines.append("--- system journal (last 20) ---")
        for ln in sj_tail[-20:]:
            lines.append(f"  {ln[:240]}")
    journal = svc.get("journal_tail") or []
    if journal:
        lines.append("--- runner journal (last 10) ---")
        for ln in journal[-10:]:
            lines.append(f"  {ln[:240]}")
    return "\n".join(lines)


def _parse_reviewer_from_notes(notes):
    """The import-inguinal importer writes notes like:
        Tagwa: Remove right external iliac LN ...
        2nd review: OK
    Pull the first 'Name:' prefix out as the reviewer; return the remainder
    (with any subsequent '2nd review' line preserved) for the notes column.
    Special-case '2nd review: OK' as the sole line — credit it as a 2nd-pass
    review (Tagwa in the current workflow). If no prefix matches, return
    ('', notes) and leave parsing alone."""
    if not notes:
        return "", ""
    first, sep, rest = notes.partition("\n")
    # 'Name: feedback' — Name must be a short, single-token identifier.
    if ":" in first:
        name, _, body = first.partition(":")
        name = name.strip()
        if name and len(name) <= 24 and " " not in name:
            remainder = body.strip()
            if rest:
                remainder = (remainder + "\n" + rest).strip()
            return name, remainder
    # Just '2nd review: ...' — sole review note. Credit the second-pass
    # reviewer (Tagwa in the LNQ workflow).
    low = first.lower()
    if low.startswith("2nd review"):
        return "Tagwa (2nd)", first.partition(":")[2].strip()
    return "", notes


def _label_from_project(project):
    name = project.get("name", "project")
    return name.lower().replace(" ", "-")


def _task_name_from_label(label):
    """Produce an nnU-Net-friendly task name from the project label.
    nnU-Net wants CamelCase-ish, no spaces or hyphens."""
    return "".join(part.capitalize() for part in
                   label.replace("-", " ").replace("_", " ").split()) or "LNQTask"


def _age_str(iso_ts, now=None):
    """Time elapsed since iso_ts (e.g. '2026-05-29T13:21:00Z') as 'Ns', 'Nmin',
    'Nh', etc. Returns '—' on parse failure or None input. Anchored at `now`
    so a UI clock can re-render age strings without re-querying."""
    if not iso_ts:
        return "—"
    try:
        t = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception:
        return "—"
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    secs = (now - t).total_seconds()
    if secs < 0:
        return "future?"
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 90 * 60:
        return f"{int(secs / 60)}m ago"
    if secs < 36 * 3600:
        return f"{int(secs / 3600)}h ago"
    return f"{int(secs / 86400)}d ago"


def _refreshDetailLiveBits_unused():
    pass


def _short_iso(s):
    if not s:
        return ""
    # 2026-05-27T14:33:02Z -> 05-27 14:33
    try:
        if "T" not in s:
            return s
        date, time = s.split("T", 1)
        time = time.split("Z", 1)[0].split(".", 1)[0]
        # date = YYYY-MM-DD; show MM-DD
        return f"{date[5:]} {time[:5]}"
    except Exception:
        return s


def _epoch_str(epoch, total):
    if epoch is None:
        return "—"
    if total:
        return f"{epoch} / {total}"
    return str(epoch)


def _eta_str(job):
    """Estimated time remaining until the run finishes, extrapolated from
    elapsed-since-started_at / current_epoch (rolling average). Just the
    duration — absolute clock-time is in whatever zone the user's brain
    happens to be in, so it isn't useful here."""
    import datetime
    started = job.get("started_at")
    ep = job.get("current_epoch")
    total = (job.get("config") or {}).get("total_epochs")
    if not started or ep is None or not total or ep < 1:
        return "—"
    try:
        t0 = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        elapsed = (now - t0).total_seconds()
        rate = elapsed / ep
        remaining = rate * (total - ep)
        if remaining < 90:
            return f"{int(remaining)}s left"
        if remaining < 90 * 60:
            return f"{int(remaining / 60)}m left"
        if remaining < 36 * 3600:
            h = remaining / 3600.0
            return f"{h:.1f}h left"
        return f"{remaining / 86400.0:.1f}d left"
    except Exception:
        return "—"
