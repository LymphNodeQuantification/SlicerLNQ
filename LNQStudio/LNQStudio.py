"""LNQStudio: the SlicerLNQ extension's main module.

A worklist manager for expert annotators working against a SlicerLNQ-Chronicle
backend (CouchDB + dicomweb-server on Jetstream2).

Cohorts, Protocols, and Projects are *authored* by an LLM agent in a separate
session (see SlicerLNQ-Chronicler/lnq-skill/). This module reads them and
presents a per-user worklist of cases to annotate or review, in the spirit of
SlicerCaseIterator but with case state persisted in the Chronicle.

Phase-2 scope: connect, list assigned projects, show a worklist with case
status, navigate cases (Prev / Next / Ctrl+N / Ctrl+P), record per-case
status + notes. Status persistence to Chronicle ships in the Annotation
schema next iteration; for now status lives in-memory.

See ../docs/architecture.md and ../docs/plans.md for the broader design.
"""

import datetime
import json
import logging
import os
import re

import ctk
import qt
import slicer
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModule,
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleTest,
    ScriptedLoadableModuleWidget,
)

try:
    import requests  # noqa: F401
except ImportError:
    slicer.util.pip_install("requests")

# Import the module, not the names, so attribute access re-resolves through
# the module's __dict__ at each call site. importlib.reload() updates that
# __dict__ in place, so after setup()'s force-reload loop a plain `Reload`
# of LNQStudio picks up new classes here without needing a full Slicer restart.
from LNQStudioLib import chronicle_client


_SETTINGS_PREFIX = "LNQStudio/"
_DEFAULT_URL = "https://lnq-chronicle.isomics.dev"

# Status enum mirrors the validator in SlicerLNQ-Chronicler/design/validate_doc_update.js.
_STATUSES = (
    "todo",
    "in_progress",
    "submitted_for_review",
    "needs_changes",
    "approved",
    "needs_consultation",
)
_STATUS_LABEL = {
    "todo": "Todo",
    "in_progress": "In progress",
    "submitted_for_review": "Submitted for review",
    "needs_changes": "Needs changes",
    "approved": "Approved",
    "needs_consultation": "Needs consultation",
}
_STATUS_COLOR = {
    "todo": qt.QColor("#c0392b"),               # red
    "in_progress": qt.QColor("#f39c12"),        # amber
    "submitted_for_review": qt.QColor("#2980b9"),  # blue
    "needs_changes": qt.QColor("#d35400"),      # burnt orange
    "approved": qt.QColor("#27ae60"),           # green
    "needs_consultation": qt.QColor("#8e44ad"), # purple
}

# Distinct colors for cycling through the segmentation history (oldest -> newest).
_HISTORY_COLORS = [
    (0.20, 0.20, 0.90),  # blue
    (0.90, 0.20, 0.20),  # red
    (0.10, 0.80, 0.80),  # cyan
    (0.20, 0.50, 0.50),  # teal
    (0.00, 0.90, 0.20),  # green
    (0.90, 0.90, 0.20),  # yellow (latest)
]


def _manilaRewrite(path):
    """If `path` looks like a Manila-canonical absolute path that doesn't exist
    locally, return the local-mount equivalent. Otherwise return the path
    unchanged."""
    settings = qt.QSettings()
    canonical = settings.value(_SETTINGS_PREFIX + "manila_canonical", "/media/share/LNQ-data")
    local = settings.value(_SETTINGS_PREFIX + "manila_local",
                           "/private/tmp/media/share/LNQ-data")
    if canonical and local and path.startswith(canonical):
        candidate = local + path[len(canonical):]
        if os.path.lexists(candidate):
            return candidate
    return path


def _resolveSymlinks(path):
    """Manually walk a symlink chain, rewriting Manila-canonical link targets
    to the local mount as we go. The OS's normal symlink resolution can't do
    this because targets like `/media/share/LNQ-data/...` don't exist on macOS;
    only `/private/tmp/media/share/LNQ-data/...` does."""
    visited = set()
    current = path
    while True:
        if current in visited:
            return current  # cycle; bail
        visited.add(current)
        if not os.path.islink(current):
            return current
        target = os.readlink(current)
        if not os.path.isabs(target):
            target = os.path.normpath(os.path.join(os.path.dirname(current), target))
        current = _manilaRewrite(target)


def _localPathFromRef(ref):
    """Convert a `local-uri` ref's value into a filesystem path the OS can open.
    Strips file:// prefix, applies the Manila override, and follows any symlink
    chain — rewriting in-target Manila references to the local mount."""
    if not ref or not ref.get("value"):
        return None
    value = ref["value"]
    if value.startswith("file://"):
        value = value[len("file://"):]
    value = _manilaRewrite(value)
    if os.path.lexists(value):
        value = _resolveSymlinks(value)
    return value


# =============================================================================
# Module
# =============================================================================

class LNQStudio(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        parent.title = "LNQ Studio"
        parent.categories = ["LNQ"]
        parent.dependencies = []
        parent.contributors = ["Steve Pieper (Isomics)"]
        parent.helpText = (
            "Worklist manager for SlicerLNQ-Chronicle. Lists annotation and "
            "review projects assigned to the current Slicer user, presents "
            "their cases in a sortable table, and supports prev/next navigation "
            "with status + notes per case. Cohorts, protocols, and projects "
            "themselves are created via an LLM agent using the lnq-skill."
        )
        parent.acknowledgementText = (
            "This work is part of the SlicerLNQ project; see https://lnqproject.org."
        )


# =============================================================================
# Logic — connection state + per-session case status
# =============================================================================

class LNQStudioLogic(ScriptedLoadableModuleLogic):
    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
        self.client = None

    @staticmethod
    def slicerUserLogin():
        """Return the current Slicer user's login string, falling back to
        their display name, falling back to the OS user."""
        try:
            info = slicer.app.applicationLogic().GetUserInformation()
            login = info.GetLogin() or info.GetName()
            if login:
                return login
        except Exception:
            pass
        import os
        return os.environ.get("USER") or "anonymous"


# =============================================================================
# Widget
# =============================================================================

_WORKLIST_WINDOW_OBJ = "LNQ-WorklistWindow"
_WORKLIST_TOOLBAR_OBJ = "LNQ-Toolbar"
_DASHBOARD_WINDOW_OBJ = "LNQ-DashboardWindow"


def _makeLnqIcon(size=22, bg="#1abc9c", fg="white"):
    """Render an 'LNQ' label as a QIcon at runtime. Avoids shipping a PNG."""
    pix = qt.QPixmap(size, size)
    pix.fill(qt.QColor(bg))
    painter = qt.QPainter(pix)
    painter.setRenderHint(qt.QPainter.TextAntialiasing, True)
    painter.setPen(qt.QColor(fg))
    font = qt.QFont()
    font.setBold(True)
    font.setPointSize(max(7, int(size * 0.4)))
    painter.setFont(font)
    painter.drawText(pix.rect(), qt.Qt.AlignCenter, "LNQ")
    painter.end()
    return qt.QIcon(pix)


class LNQStudioWidget(ScriptedLoadableModuleWidget):
    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        self.logic = None
        self._connection = None
        self._annotate = None
        self._review = None
        self._worklistWindow = None
        self._worklistAction = None
        self._dashboardWindow = None
        self._dashboardAction = None

    def setup(self):
        # Slicer's Reload re-imports this file but leaves LNQStudioLib.*
        # cached in sys.modules, so edits to dashboard.py / worklist_window.py
        # don't land until restart. Force-reload our submodules here so a
        # plain Reload click is enough.
        import importlib, sys
        for _name in list(sys.modules):
            if _name.startswith("LNQStudioLib."):
                try:
                    importlib.reload(sys.modules[_name])
                except Exception:
                    pass
        # Tell any in-flight volume workers from a previous module load to
        # stop emitting — their signal targets (old WorklistWindow's tables)
        # have been C++-destroyed by Slicer's widget teardown, so a stale
        # emit crashes with "destroyed QTableWidget object". The stop-flag
        # registry hangs off the `slicer` python module (a real module, not
        # a Qt object, so attribute assignment works) and persists across
        # LNQStudio module reloads.
        for flag in getattr(slicer, "_lnq_volume_stop_flags", []) or []:
            try:
                flag["stop"] = True
            except Exception:
                pass
        slicer._lnq_volume_stop_flags = []
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = LNQStudioLogic()

        self._connection = ConnectionSection(self)
        self.layout.addWidget(self._connection)

        self._annotate = WorklistSection(self, role="annotator", title="Annotate")
        self.layout.addWidget(self._annotate)

        self._review = WorklistSection(self, role="reviewer", title="Review")
        self.layout.addWidget(self._review)

        phase3 = ctk.ctkCollapsibleButton()
        phase3.text = "Train / Infer / Deploy / Dashboard"
        phase3.collapsed = True
        phase3Layout = qt.QVBoxLayout(phase3)
        phase3Label = qt.QLabel(
            "Phase 3: launches Js2 GPU instances for training, runs inference "
            "against cohorts, deploys reviewed inferences as OHIF sites, and "
            "summarises status in a dashboard. Not yet implemented."
        )
        phase3Label.wordWrap = True
        phase3Layout.addWidget(phase3Label)
        self.layout.addWidget(phase3)

        self.layout.addStretch(1)

        # Top-level worklist window + toolbar toggle. Both are reused across
        # module reloads via objectName lookup on the main window.
        self._installWorklistToolbar()
        self._reattachExistingWindow()

    def cleanup(self):
        # Don't destroy windows — leaving them alive across module switches is
        # the point. Just unhook our actions and stop the dashboard's
        # background changes-feed thread so reload doesn't leak it.
        if self._worklistAction is not None:
            try:
                self._worklistAction.toggled.disconnect(self.setWorklistVisible)
            except Exception:
                pass
        if self._dashboardAction is not None:
            try:
                self._dashboardAction.toggled.disconnect(self.setDashboardVisible)
            except Exception:
                pass
        if self._dashboardWindow is not None:
            try:
                self._dashboardWindow.detach()
            except Exception:
                pass

    # ----- worklist window & toolbar -----

    def _installWorklistToolbar(self):
        """Create or re-attach the LNQ toolbar's actions: jump-to-module and
        worklist toggle. Survives module reloads via objectName lookup."""
        mw = slicer.util.mainWindow()
        toolbar = mw.findChild(qt.QToolBar, _WORKLIST_TOOLBAR_OBJ)
        if toolbar is None:
            toolbar = qt.QToolBar("LNQ")
            toolbar.setObjectName(_WORKLIST_TOOLBAR_OBJ)
            mw.addToolBar(toolbar)
        else:
            toolbar.clear()
        # Jump back to the LNQ Studio module (icon: drawn 'LNQ' letters).
        jumpAction = toolbar.addAction(_makeLnqIcon(), "LNQ Studio")
        jumpAction.toolTip = "Switch to the LNQ Studio module"
        jumpAction.triggered.connect(lambda: slicer.util.selectModule("LNQStudio"))
        # Toggle the top-level worklist window.
        action = toolbar.addAction("Worklist")
        action.checkable = True
        action.toolTip = "Show / hide the LNQ worklist window (top-level)."
        action.toggled.connect(self.setWorklistVisible)
        self._worklistAction = action
        # Toggle the top-level dashboard window.
        dashAction = toolbar.addAction("Dashboard")
        dashAction.checkable = True
        dashAction.toolTip = ("Show / hide the LNQ dashboard. "
                              "Reacts live to TrainingJob changes via the "
                              "CouchDB _changes feed.")
        dashAction.toggled.connect(self.setDashboardVisible)
        self._dashboardAction = dashAction

    def _reattachExistingWindow(self):
        """Re-bind to windows left over from a prior reload."""
        for objname in (_WORKLIST_WINDOW_OBJ, _DASHBOARD_WINDOW_OBJ):
            existing = slicer.util.mainWindow().findChild(qt.QWidget, objname)
            if existing is not None:
                existing.deleteLater()
        # Defer creation until requested.

    def worklistWindow(self, create=True):
        if self._worklistWindow is None and create:
            self._worklistWindow = WorklistWindow(self)
            self._worklistWindow.setObjectName(_WORKLIST_WINDOW_OBJ)
        return self._worklistWindow

    def setWorklistVisible(self, visible):
        win = self.worklistWindow(create=visible)
        if win is None:
            return
        if visible:
            win.show()
            win.raise_()
            win.activateWindow()
            # Push current project + cases to whichever role tabs are active.
            for section in (self._annotate, self._review):
                if section is not None:
                    section._propagateToWorklist()
        else:
            win.hide()
        self._syncWorklistVisibilityState(visible)

    def _syncWorklistVisibilityState(self, visible):
        """Reflect window-visible state on toolbar action + section buttons,
        without re-triggering the toggle handlers."""
        if self._worklistAction is not None:
            self._worklistAction.blockSignals(True)
            self._worklistAction.checked = visible
            self._worklistAction.blockSignals(False)
        for section in (self._annotate, self._review):
            if section is None or not hasattr(section, "_worklistButton"):
                continue
            section._worklistButton.blockSignals(True)
            section._worklistButton.checked = visible
            section._worklistButton.blockSignals(False)

    # ----- dashboard window -----

    def dashboardWindow(self, create=True):
        if self._dashboardWindow is None and create:
            from LNQStudioLib.dashboard import DashboardWindow
            self._dashboardWindow = DashboardWindow(self)
        return self._dashboardWindow

    def setDashboardVisible(self, visible):
        win = self.dashboardWindow(create=visible)
        if win is None:
            return
        if visible:
            win.show()
            win.raise_()
            win.activateWindow()
            win.attach()  # subscribes to _changes feed + initial load
        else:
            win.detach()
            win.hide()
        self._syncDashboardVisibilityState(visible)

    def _syncDashboardVisibilityState(self, visible):
        if self._dashboardAction is not None:
            self._dashboardAction.blockSignals(True)
            self._dashboardAction.checked = visible
            self._dashboardAction.blockSignals(False)

    # ----- shared client access -----

    def setClient(self, client):
        self.logic.client = client
        self._annotate.onClientChanged(client)
        self._review.onClientChanged(client)
        # Auto-collapse the connection section after a successful connect.
        if self._connection is not None and client is not None:
            self._connection.collapsed = True

    def getClient(self):
        return self.logic.client

# =============================================================================
# Connection section
# =============================================================================

class ConnectionSection(ctk.ctkCollapsibleButton):
    def __init__(self, owner):
        ctk.ctkCollapsibleButton.__init__(self)
        self.text = "Connection"
        self._owner = owner

        layout = qt.QFormLayout(self)
        layout.fieldGrowthPolicy = qt.QFormLayout.ExpandingFieldsGrow

        self._urlEdit = qt.QLineEdit()
        layout.addRow("Chronicle URL:", self._urlEdit)

        # Slicer user identity — used for project membership matching and
        # `created_by`. Read-only; edit in Application Settings.
        self._userLabel = qt.QLabel(LNQStudioLogic.slicerUserLogin())
        self._userLabel.toolTip = (
            "User identity from Application Settings -> User Information "
            "(Login field). Edit it there if you need to change it. Used for "
            "project membership filtering and the 'created_by' field on docs."
        )
        layout.addRow("User:", self._userLabel)

        # CouchDB credentials — separate from the user identity in Phase 2
        # (admin-party). Will collapse into the user's own credentials once
        # Phase 2a `_users` auth ships.
        self._authUserEdit = qt.QLineEdit()
        self._authUserEdit.toolTip = (
            "CouchDB credential for HTTP auth. While the Chronicle is in "
            "admin-party, this is 'admin'. After Phase 2a auth, this matches "
            "the user above."
        )
        layout.addRow("CouchDB user:", self._authUserEdit)

        self._passEdit = qt.QLineEdit()
        self._passEdit.echoMode = qt.QLineEdit.Password
        layout.addRow("Password:", self._passEdit)

        self._dbEdit = qt.QLineEdit()
        layout.addRow("Database:", self._dbEdit)

        self._connectButton = qt.QPushButton("Connect")
        self._connectButton.clicked.connect(self._onConnect)
        layout.addRow(self._connectButton)

        self._statusLabel = qt.QLabel("Not connected.")
        self._statusLabel.wordWrap = True
        layout.addRow(self._statusLabel)

        # Restore from QSettings.
        settings = qt.QSettings()
        self._urlEdit.text = settings.value(_SETTINGS_PREFIX + "url", _DEFAULT_URL)
        self._authUserEdit.text = settings.value(_SETTINGS_PREFIX + "auth_user", "admin")
        self._passEdit.text = settings.value(_SETTINGS_PREFIX + "password", "")
        self._dbEdit.text = settings.value(_SETTINGS_PREFIX + "db", "lnq")

    def _onConnect(self):
        url = self._urlEdit.text.strip()
        auth_user = self._authUserEdit.text.strip() or "admin"
        password = self._passEdit.text
        db = self._dbEdit.text.strip() or "lnq"
        actor = self._userLabel.text

        settings = qt.QSettings()
        settings.setValue(_SETTINGS_PREFIX + "url", url)
        settings.setValue(_SETTINGS_PREFIX + "auth_user", auth_user)
        settings.setValue(_SETTINGS_PREFIX + "password", password)
        settings.setValue(_SETTINGS_PREFIX + "db", db)

        settings_for_client = qt.QSettings()
        manila_canonical = settings_for_client.value(_SETTINGS_PREFIX + "manila_canonical",
                                                      "/media/share/LNQ-data")
        manila_local = settings_for_client.value(_SETTINGS_PREFIX + "manila_local",
                                                  "/private/tmp/media/share/LNQ-data")
        try:
            client = chronicle_client.ChronicleClient(url, auth_user, password, db_name=db, actor=actor,
                                     manila_canonical=manila_canonical,
                                     manila_local=manila_local)
            up = client.ping()
            info = client.server_info()
            if not client.database_exists():
                self._statusLabel.text = (
                    f"Connected, but database '{db}' does not exist. "
                    "Run bin/deploy-design.sh in SlicerLNQ-Chronicler to create it."
                )
                self._owner.setClient(None)
                return
            self._statusLabel.text = (
                f"Connected as '{actor}' (auth: {auth_user}). "
                f"CouchDB {info.get('version', '?')}, "
                f"status={up.get('status', '?')}, db='{db}'."
            )
            self._owner.setClient(client)
        except Exception as exc:
            self._statusLabel.text = f"Connection failed: {exc}"
            self._owner.setClient(None)


# =============================================================================
# Worklist section (used twice: once for Annotate role, once for Review)
# =============================================================================

class _AnnotationChangesProxy(qt.QObject):
    """Qt object that owns a thread-safe signal so a background changes-feed
    watcher can fire UI updates on the main thread (Qt forbids touching
    widgets off-thread)."""
    changed = qt.Signal()


class _VolumeReadyProxy(qt.QObject):
    """Carries per-case lymph-node volume updates from the background
    compute thread to the GUI thread. Emits (case_id, volume_ml)."""
    ready = qt.Signal(str, float)


# Volume column is shown for any project whose Protocol declares a single
# foreground label in its color_table — that covers all current LNQ projects
# (inguinal, abdominopelvic, axillary, mediastinal) and any future per-anatomy
# single-class LN project, without needing to hardcode project_ids.
def _projectHasSingleLabelProtocol(client, project_doc):
    """True iff the project's Protocol has exactly one foreground label
    (label > 0 in color_table). Volume = count of nonzero voxels × voxel
    volume only makes sense for single-foreground SEGs."""
    if not project_doc:
        return False
    proto_id = project_doc.get("protocol_id")
    if not proto_id:
        return False
    try:
        proto = client.get(proto_id)
    except Exception:
        return False
    fg = [e for e in (proto.get("color_table") or []) if (e.get("label") or 0) > 0]
    return len(fg) == 1


class WorklistSection(ctk.ctkCollapsibleButton):
    def __init__(self, owner, role, title):
        ctk.ctkCollapsibleButton.__init__(self)
        self.text = title
        self.collapsed = True
        self._owner = owner
        self._role = role
        self._currentProject = None
        self._currentCaseId = None
        self._cases = []        # CohortResolution.cases for the active project
        self._chains = {}       # case_id -> [Annotation oldest..newest]
        self._cohortName = None
        self._protocolName = None

        # Background watcher on the chronicle's _changes feed. Fires whenever
        # an Annotation doc lands so the worklist table reflects new
        # inference outputs / status updates without the user clicking Refresh.
        # Re-armed inside _onProjectChanged so the project_id is current.
        self._annotationsWatcher = None
        self._annotationsChangesProxy = _AnnotationChangesProxy()
        self._annotationsChangesProxy.changed.connect(self._onAnnotationsChanged)

        # Lymph-node volume cache + background compute. _segVolumes maps
        # (case_id, blob_id) -> volume_ml; the worklist shows the max across
        # the chain via _maxVolumeForCase. First time a (case, blob) pair is
        # seen we compute the volume from the resolved SEG file with
        # SimpleITK and PUT the value back onto the Blob's derived_metrics
        # so the next session (and any other workstation) can read it
        # without recomputing.
        self._segVolumes = {}
        self._volumeWorker = None
        self._volumeStopFlag = {"stop": False}
        self._volumeProxy = _VolumeReadyProxy()
        self._volumeProxy.ready.connect(self._onVolumeReady)

        layout = qt.QVBoxLayout(self)

        # ----- project picker -----
        projectRow = qt.QHBoxLayout()
        projectRow.addWidget(qt.QLabel("Project:"))
        self._projectCombo = qt.QComboBox()
        self._projectCombo.sizeAdjustPolicy = qt.QComboBox.AdjustToMinimumContentsLengthWithIcon
        self._projectCombo.minimumContentsLength = 12
        self._projectCombo.currentIndexChanged.connect(self._onProjectChanged)
        projectRow.addWidget(self._projectCombo, 1)
        self._refreshProjectsButton = qt.QPushButton("Refresh")
        self._refreshProjectsButton.clicked.connect(self._refreshProjects)
        projectRow.addWidget(self._refreshProjectsButton)
        layout.addLayout(projectRow)

        # ----- project context line -----
        self._contextLabel = qt.QLabel("(no project selected)")
        self._contextLabel.wordWrap = True
        self._contextLabel.setStyleSheet("color: gray;")
        layout.addWidget(self._contextLabel)

        # ----- worklist toggle -----
        self._worklistButton = qt.QPushButton("Show worklist")
        self._worklistButton.checkable = True
        self._worklistButton.clicked.connect(self._onWorklistButton)
        layout.addWidget(self._worklistButton)

        # ----- current-case detail (collapsed sub-section) -----
        caseBox = ctk.ctkCollapsibleGroupBox()
        caseBox.title = "Current case"
        caseBox.collapsed = False
        caseLayout = qt.QFormLayout(caseBox)
        self._caseSummaryLabel = qt.QLabel("(no case)")
        self._caseSummaryLabel.wordWrap = True
        caseLayout.addRow(self._caseSummaryLabel)

        self._statusCombo = qt.QComboBox()
        for s in _STATUSES:
            self._statusCombo.addItem(_STATUS_LABEL[s], s)
        caseLayout.addRow("Status:", self._statusCombo)

        self._notesEdit = qt.QPlainTextEdit()
        self._notesEdit.setMinimumWidth(0)
        self._notesEdit.setMaximumHeight(80)
        self._notesEdit.placeholderText = "Notes (markdown OK). Saved per case."
        caseLayout.addRow("Notes:", self._notesEdit)

        # Save row — two-button design: status alone vs status + new segmentation.
        # Separate rows so a stray Ctrl+S can't accidentally write a SEG file.
        saveRow = qt.QHBoxLayout()
        self._saveStatusButton = qt.QPushButton("Save status")
        self._saveStatusButton.toolTip = (
            "Write a new Annotation with the current status + notes "
            "(no segmentation change). Shortcut: Ctrl+S"
        )
        self._saveStatusButton.clicked.connect(self._saveStatusOnly)
        self._saveSegButton = qt.QPushButton("Save status + segmentation")
        self._saveSegButton.toolTip = (
            "Write a new Annotation AND save the currently-visible "
            "segmentation as a new file referenced by the Annotation. "
            "Shortcut: Ctrl+Shift+S"
        )
        self._saveSegButton.clicked.connect(self._saveStatusAndSegmentation)
        saveRow.addWidget(self._saveStatusButton)
        saveRow.addWidget(self._saveSegButton)
        caseLayout.addRow(saveRow)

        # Navigation row.
        navRow = qt.QHBoxLayout()
        self._prevButton = qt.QPushButton("◀  Prev")
        self._nextButton = qt.QPushButton("Next  ▶")
        self._prevButton.clicked.connect(lambda: self._step(-1))
        self._nextButton.clicked.connect(lambda: self._step(1))
        navRow.addWidget(self._prevButton)
        navRow.addWidget(self._nextButton)
        caseLayout.addRow(navRow)

        layout.addWidget(caseBox)

        self._setEnabledForCase(False)
        self.enabled = False  # disabled until a client is connected

        # ----- shortcuts (only fire when an LNQ tab is visible) -----
        for keyseq, slot in (
            ("Ctrl+N", lambda: self._step(1)),
            ("Ctrl+P", lambda: self._step(-1)),
            ("Ctrl+S", self._saveStatusOnly),
            ("Ctrl+Shift+S", self._saveStatusAndSegmentation),
        ):
            sc = qt.QShortcut(qt.QKeySequence(keyseq), self)
            sc.setContext(qt.Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)

    # ----- client lifecycle -----

    def onClientChanged(self, client):
        self.enabled = client is not None
        self._refreshProjects()

    def _refreshProjects(self):
        self._projectCombo.blockSignals(True)
        self._projectCombo.clear()
        client = self._owner.getClient()
        if client is None:
            self._projectCombo.addItem("(connect first)", None)
            self._projectCombo.blockSignals(False)
            self._onProjectChanged(0)
            return
        try:
            projects = client.list_heads_by_type("Project")
        except chronicle_client.ChronicleError as exc:
            self._contextLabel.text = f"Failed to list projects: {exc}"
            self._projectCombo.blockSignals(False)
            return

        user = LNQStudioLogic.slicerUserLogin()
        mine = [p for p in projects if _hasMemberRole(p, user, self._role)]
        if not mine:
            self._projectCombo.addItem(
                f"(no {self._role} projects for '{user}')", None
            )
        for proj in mine:
            label = f"{proj.get('name', '?')} (v{proj.get('version', '?')})"
            self._projectCombo.addItem(label, proj["_id"])

        self._projectCombo.blockSignals(False)
        self._onProjectChanged(self._projectCombo.currentIndex)

    def _onProjectChanged(self, _index):
        proj_id = self._projectCombo.currentData
        client = self._owner.getClient()
        if client is None or not proj_id:
            self._currentProject = None
            self._cases = []
            self._chains = {}
            self._contextLabel.text = "(no project selected)"
            if self._annotationsWatcher is not None:
                try: self._annotationsWatcher.stop()
                except Exception: pass
                self._annotationsWatcher = None
            self._loadCase(None)
            self._propagateToWorklist()
            return
        try:
            proj = client.get(proj_id)
        except chronicle_client.ChronicleError as exc:
            self._contextLabel.text = f"Failed to load project: {exc}"
            return
        self._currentProject = proj

        # Cohort + Protocol metadata for context.
        try:
            cohort = client.get(proj["cohort_id"])
            self._cohortName = cohort.get("name", "?")
        except Exception:
            self._cohortName = proj.get("cohort_id", "?")
        try:
            protocol = client.get(proj["protocol_id"])
            self._protocolName = protocol.get("name", "?")
        except Exception:
            self._protocolName = proj.get("protocol_id", "?")

        # Resolve cases from the latest CohortResolution; fetch annotation chains.
        try:
            resolution = client.latest_cohort_resolution(proj["cohort_id"])
            self._cases = (resolution or {}).get("cases", [])
            self._chains = client.annotation_chains_by_case(proj["_id"])
        except chronicle_client.ChronicleError as exc:
            self._contextLabel.text = f"Failed to load resolution/annotations: {exc}"
            return

        self._contextLabel.text = (
            f"Cohort: {self._cohortName}  /  Protocol: {self._protocolName}  "
            f"/  {len(self._cases)} cases"
        )
        # (Re-)arm the changes-feed watcher for this project. We restart it on
        # every project switch so the watcher is bound to whichever project's
        # annotations matter right now.
        if self._annotationsWatcher is not None:
            try: self._annotationsWatcher.stop()
            except Exception: pass
            self._annotationsWatcher = None
        try:
            self._annotationsWatcher = chronicle_client.ChronicleChangesWatcher(
                client,
                on_changes=lambda _ch: self._annotationsChangesProxy.changed.emit(),
                doc_types={"Annotation"},
            )
            self._annotationsWatcher.start()
        except Exception as exc:
            logging.warning("changes watcher start failed: %s", exc)
        # Pre-populate the volume cache from chronicle BEFORE the table is
        # painted so already-computed values show up on first load instead
        # of trickling in. (Keys are (case_id, blob_id) tuples; the blob_id
        # is content-addressed so cross-project collisions are impossible
        # — we don't need to clear the cache when switching projects.)
        self._readCachedVolumesSync()
        self._propagateToWorklist()
        # Async worker fills in any volumes that aren't cached yet (e.g.
        # SEG blobs that landed after the last visit to this project).
        self._kickVolumeWorker()
        # No auto-load. Reset the case-detail panel and leave the scene alone
        # until the user clicks a row, presses Next, etc.
        self._currentCaseId = None
        self._caseSummaryLabel.text = "(no case loaded — pick one from the worklist)"
        self._setEnabledForCase(False)

    # ----- case navigation -----

    def _firstUnfinishedIndex(self):
        for i, case in enumerate(self._cases):
            if self._statusFor(case["case_id"]) != "approved":
                return i
        return None

    def _step(self, delta):
        if not self._cases:
            return
        ids = [c["case_id"] for c in self._cases]
        if self._currentCaseId not in ids:
            self._loadCase(ids[0])
            return
        idx = ids.index(self._currentCaseId)
        new_idx = max(0, min(len(ids) - 1, idx + delta))
        self._loadCase(ids[new_idx])

    def _loadCase(self, case_id):
        self._currentCaseId = case_id
        if case_id is None:
            self._caseSummaryLabel.text = "(no case)"
            self._setEnabledForCase(False)
            return
        case = self._caseFor(case_id)
        if case is None:
            self._caseSummaryLabel.text = f"<i>case '{case_id}' not in resolution</i>"
            self._setEnabledForCase(False)
            return
        chain = self._chains.get(case_id, [])
        head = chain[-1] if chain else None
        status = (head or {}).get("status", "todo")
        notes = (head or {}).get("notes", "") if head else ""
        primary = case.get("primary_site") or "?"
        sex = case.get("sex") or "?"
        history_summary = " → ".join(
            f"{a.get('producer', {}).get('label') or '?'}={a.get('status', '?')[:3]}"
            for a in chain
        ) or "(no annotations)"
        self._caseSummaryLabel.text = (
            f"<b>{case_id}</b>  &mdash;  {primary} ({sex})"
            f"<br><small>history: {history_summary}</small>"
        )
        idx = self._statusCombo.findData(status)
        self._statusCombo.currentIndex = max(0, idx)
        self._notesEdit.plainText = notes
        self._setEnabledForCase(True)

        # Load CT + segmentation chain into Slicer.
        try:
            self._loadIntoScene(case, chain)
        except Exception as exc:
            slicer.util.errorDisplay(f"Failed to load '{case_id}': {exc}")

        win = self._owner.worklistWindow(create=False)
        if win is not None:
            win.highlightCase(self._role, case_id)

    def _loadIntoScene(self, case, chain):
        """Clear the scene, load the CT, and load each Annotation's seg_ref as
        a separate segmentation node — colored by history position, only the
        latest visible by default. Mirrors the lnq-retro6.py pattern."""
        slicer.mrmlScene.Clear()
        slicer.app.processEvents(qt.QEventLoop.ExcludeUserInputEvents)

        client = self._owner.getClient()
        ct_path = client.resolve_ref(case.get("ct_ref")) if client else None
        if ct_path and os.path.exists(ct_path):
            ct_node = slicer.util.loadVolume(ct_path)
            ct_node.SetName(case["case_id"])
            ct_node.GetDisplayNode().SetAutoWindowLevel(False)
            ct_node.GetDisplayNode().SetWindow(350.0)
            ct_node.GetDisplayNode().SetLevel(40.0)
        else:
            logging.warning("CT could not be resolved for case_ref %s", case.get("ct_ref"))

        # Visibility rule: show the latest manual annotation (if any) AND every
        # model-produced annotation. Two inference runs against the same case
        # (e.g. preview vs final checkpoints) therefore overlay automatically
        # so the reviewer can compare them. Manual chain stays uncluttered:
        # only the latest manual edit is shown by default.
        annotated_with_seg = [a for a in chain if a.get("seg_ref")]
        manual_indices = [i for i, a in enumerate(annotated_with_seg)
                          if (a.get("producer") or {}).get("kind") != "model"]
        latest_manual_idx = manual_indices[-1] if manual_indices else None

        loaded = 0
        latest_visible = None
        for i, ann in enumerate(annotated_with_seg):
            ref = ann.get("seg_ref")
            seg_path = client.resolve_ref(ref) if client else None
            if not seg_path or not os.path.exists(seg_path):
                logging.warning("seg could not be resolved: %s", ref)
                continue
            seg_node = slicer.util.loadSegmentation(seg_path)
            label = ann.get("producer", {}).get("label") or f"v{ann.get('version', '?')}"
            seg_node.SetName(f"{case['case_id']} / {label}")
            display = seg_node.GetDisplayNode()
            kind = (ann.get("producer") or {}).get("kind")
            # Inference SEGs render yellow with a thicker, full-opacity
            # outline so they pop against the CT (default labelmap-outline
            # opacity is muted, which made the previous red barely visible).
            # Manual annotations keep the history-color cycle so multiple
            # editor passes are visually distinguishable.
            if kind == "model":
                color = (1.0, 1.0, 0.0)
            else:
                color = _HISTORY_COLORS[loaded % len(_HISTORY_COLORS)]
            seg_ids = seg_node.GetSegmentation().GetSegmentIDs()
            if seg_ids:
                seg_node.GetSegmentation().GetSegment(seg_ids[0]).SetColor(*color)
            display.SetAllSegmentsVisibility2DFill(False)
            display.SetAllSegmentsVisibility2DOutline(True)
            display.SetOpacity2DOutline(1.0)
            display.SetSliceIntersectionThickness(2)
            # Closed surface so 3D view renders the segment.
            seg_node.CreateClosedSurfaceRepresentation()
            visible = (kind == "model") or (i == latest_manual_idx)
            display.SetVisibility(visible)
            if visible:
                latest_visible = seg_node
            loaded += 1
        slicer.app.processEvents(qt.QEventLoop.ExcludeUserInputEvents)

        # Park the views on the segmentation so the reviewer doesn't have to
        # hunt for it. Jump-slices uses centered=False (only the through-plane
        # offset changes; in-plane position stays put). The 3D view's focal
        # point gets pinned to the segmentation centroid.
        # Pick a target whose bounds are valid (non-empty). Prefer a non-empty
        # segmentation; fall back to any other loaded seg, then to the CT.
        # Empty segs (zero foreground voxels) report degenerate bounds where
        # min > max — we skip those and keep looking, so the reviewer still
        # gets a usefully-framed view even when the model predicted nothing.
        def _validBounds(node):
            b = [0.0] * 6
            try: node.GetBounds(b)
            except Exception: return None
            if b[0] < b[1] and b[2] < b[3] and b[4] < b[5]:
                return b
            return None

        target_bounds = None
        target_label = None
        if latest_visible is not None:
            target_bounds = _validBounds(latest_visible)
            target_label = latest_visible.GetName() if target_bounds else None
        if target_bounds is None:
            # Latest was empty; scan every loaded seg in the scene for one
            # with content.
            for n in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
                b = _validBounds(n)
                if b is not None:
                    target_bounds = b; target_label = n.GetName(); break
        if target_bounds is None:
            # Still nothing — center on the CT volume so the camera at least
            # frames the patient.
            for v in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
                b = _validBounds(v)
                if b is not None:
                    target_bounds = b; target_label = f"CT ({v.GetName()})"
                    logging.info("no non-empty segmentation; "
                                 "framing camera on CT volume")
                    break
        if target_bounds is None:
            return   # truly nothing to frame; leave default camera as-is

        cx = 0.5 * (target_bounds[0] + target_bounds[1])
        cy = 0.5 * (target_bounds[2] + target_bounds[3])
        cz = 0.5 * (target_bounds[4] + target_bounds[5])
        slicer.modules.markups.logic().JumpSlicesToLocation(cx, cy, cz, False)
        # Frame the 3D view as an anterior projection so the reviewer sees
        # the lymph nodes from the front without rotating manually. RAS axes:
        #   +X = patient Right, +Y = Anterior, +Z = Superior.
        # Camera goes anterior of the centroid (cy + distance), focal point
        # at the centroid, view-up pointing superior. Distance scales with
        # the target's extent so small clusters don't fill the viewport and
        # whole-CT framing doesn't disappear behind the near clip plane.
        extent = max(target_bounds[1] - target_bounds[0],
                     target_bounds[3] - target_bounds[2],
                     target_bounds[5] - target_bounds[4])
        distance = max(300.0, 3.0 * extent)
        layoutManager = slicer.app.layoutManager()
        for i in range(layoutManager.threeDViewCount):
            view = layoutManager.threeDWidget(i).threeDView()
            cameraNode = view.cameraNode()
            if cameraNode is None:
                continue
            camera = cameraNode.GetCamera()
            camera.SetPosition(cx, cy + distance, cz)
            camera.SetFocalPoint(cx, cy, cz)
            camera.SetViewUp(0.0, 0.0, 1.0)
            rw = view.renderWindow()
            renderer = rw.GetRenderers().GetFirstRenderer() if rw else None
            if renderer is not None:
                renderer.ResetCameraClippingRange()
            cameraNode.Modified()
            view.scheduleRender()

    def _saveStatusOnly(self):
        """Write a new Annotation reflecting the current status+notes; no SEG."""
        if self._currentCaseId is None or self._currentProject is None:
            return
        self._writeAnnotation(seg_ref=None, producer={
            "kind": "review",
            "label": "in-slicer",
            "model_generation_id": None,
        })

    def _saveStatusAndSegmentation(self):
        """Write the visible segmentation as a new file, then a new Annotation
        referencing it. Status + notes are taken from the form."""
        if self._currentCaseId is None or self._currentProject is None:
            return
        seg_node = self._findSegmentationToSave()
        if seg_node is None:
            slicer.util.errorDisplay(
                "No segmentation is visible. In the Data module (or via the "
                "eye icons on the segmentation nodes) make the segmentation "
                "you want to save visible, then try again."
            )
            return
        chain = self._chains.get(self._currentCaseId, [])
        head = chain[-1] if chain else None
        new_version = (head["version"] + 1) if head else 1
        save_path = self._segSavePath(self._currentCaseId, new_version)
        btn = qt.QMessageBox.question(
            slicer.util.mainWindow(), "Save segmentation",
            f"Save segmentation <b>{seg_node.GetName()}</b> to:<br>"
            f"<code>{save_path}</code><br><br>"
            f"and write a new Annotation referencing it?",
            qt.QMessageBox.Ok | qt.QMessageBox.Cancel, qt.QMessageBox.Ok,
        )
        if btn != qt.QMessageBox.Ok:
            return
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
        except OSError as exc:
            slicer.util.errorDisplay(f"Can't create save dir: {exc}")
            return
        if not slicer.util.saveNode(seg_node, save_path):
            slicer.util.errorDisplay(f"Failed to save segmentation to {save_path}")
            return
        # Register the saved file as a Blob and reference it by blob_id so the
        # annotation travels well across machines.
        client = self._owner.getClient()
        try:
            blob = client.register_blob(save_path)
        except chronicle_client.ChronicleError as exc:
            slicer.util.errorDisplay(f"Could not register blob: {exc.reason}")
            return
        login = LNQStudioLogic.slicerUserLogin()
        self._writeAnnotation(seg_ref={"blob_id": blob["_id"]}, producer={
            "kind": "manual",
            "label": f"edit-{login}",
            "model_generation_id": None,
        })

    def _writeAnnotation(self, seg_ref, producer):
        """Shared save path: write a new Annotation chained off the current
        head, then refresh chains + UI."""
        client = self._owner.getClient()
        if client is None:
            return
        case = self._caseFor(self._currentCaseId)
        chain = self._chains.get(self._currentCaseId, [])
        head = chain[-1] if chain else None
        try:
            client.create_annotation(
                project_id=self._currentProject["_id"],
                case_id=self._currentCaseId,
                study_uid=(case or {}).get("study_uid"),
                status=self._statusCombo.currentData,
                notes=self._notesEdit.plainText,
                predecessor=head,
                producer=producer,
                seg_ref=seg_ref,
            )
        except chronicle_client.ChronicleError as exc:
            slicer.util.errorDisplay(f"Save rejected: {exc.reason}")
            return
        try:
            self._chains = client.annotation_chains_by_case(self._currentProject["_id"])
        except chronicle_client.ChronicleError as exc:
            slicer.util.errorDisplay(f"Refresh failed: {exc}")
            return
        self._propagateToWorklist()
        chain = self._chains.get(self._currentCaseId, [])
        head = chain[-1] if chain else None
        if head:
            self._notesEdit.plainText = head.get("notes", "")
            idx = self._statusCombo.findData(head.get("status", "todo"))
            self._statusCombo.currentIndex = max(0, idx)

    def _findSegmentationToSave(self):
        """Pick the segmentation node to write. Prefers nodes whose name
        starts with the current case_id, then visible nodes, then most
        recently modified."""
        segs = slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
        if not segs:
            return None
        matching = [s for s in segs if s.GetName().startswith(self._currentCaseId)]
        candidates = matching or segs
        visible = []
        for s in candidates:
            dn = s.GetDisplayNode()
            if dn is not None and dn.GetVisibility():
                visible.append(s)
        pool = visible or candidates
        return max(pool, key=lambda s: s.GetMTime())

    def _segSavePath(self, case_id, new_version):
        """Compute the file path for a saved segmentation."""
        settings = qt.QSettings()
        manila_local = settings.value(_SETTINGS_PREFIX + "manila_local",
                                       "/private/tmp/media/share/LNQ-data")
        project_name = self._currentProject.get("name", "project")
        project_slug = re.sub(r'[^a-zA-Z0-9_.-]', '_', project_name)[:60]
        login = LNQStudioLogic.slicerUserLogin()
        return os.path.join(manila_local, "lnq-edits", project_slug,
                            f"{case_id}_v{new_version}_{login}.seg.nrrd")

    # ----- lymph-node volume column (background compute + chronicle cache) -----

    def _readCachedVolumesSync(self):
        """Single bulk Blob fetch on the GUI thread so the worklist's first
        paint already shows every value that the chronicle has cached on
        Blob.derived_metrics.lymph_node_volume_ml. ~one round-trip total —
        much cheaper than 100+ individual GETs and avoids the 'all dashes
        for 30s, then values trickle in' UX. Async worker (below) still
        handles any blob that has no cached value yet."""
        client = self._owner.getClient()
        if client is None:
            return
        if not _projectHasSingleLabelProtocol(client, self._currentProject):
            return
        needed = []
        for cid, chain in self._chains.items():
            for ann in chain:
                bid = (ann.get("seg_ref") or {}).get("blob_id")
                if not bid or (cid, bid) in self._segVolumes: continue
                needed.append((cid, bid))
        if not needed:
            return
        try:
            blobs_by_id = {b["_id"]: b for b in client.list_by_type("Blob")}
        except Exception as exc:
            logging.warning("bulk Blob fetch failed: %s", exc); return
        for cid, bid in needed:
            b = blobs_by_id.get(bid)
            if not b: continue
            v = (b.get("derived_metrics") or {}).get("lymph_node_volume_ml")
            if v is not None:
                self._segVolumes[(cid, bid)] = float(v)

    def _maxVolumeForCase(self, case_id):
        """Max already-computed seg volume across this case's chain. Returns
        None if no segs in the chain have a volume yet — the column shows
        '—' and the background worker will fill it in shortly."""
        chain = self._chains.get(case_id, [])
        vols = []
        for ann in chain:
            ref = (ann.get("seg_ref") or {})
            bid = ref.get("blob_id")
            if not bid: continue
            v = self._segVolumes.get((case_id, bid))
            if v is not None:
                vols.append(v)
        return max(vols) if vols else None

    def _kickVolumeWorker(self):
        """Spawn a background thread that, for each (case, blob_id) in the
        chains that doesn't yet have a cached volume:
          1. Tries the Blob's existing derived_metrics.lymph_node_volume_ml.
          2. Failing that, resolves the SEG file, computes via SimpleITK,
             writes back to the Blob (PUT), caches in-memory.
        Each completion emits a Qt signal so the GUI updates that row.

        Active for any project whose Protocol declares a single foreground
        label (covers all LNQ per-anatomy projects without needing a
        hardcoded project_id list)."""
        client = self._owner.getClient()
        if client is None:
            return
        if not _projectHasSingleLabelProtocol(client, self._currentProject):
            return

        # Snapshot work list: (case_id, blob_id) pairs not yet in cache.
        work = []
        for cid, chain in self._chains.items():
            seen = set()
            for ann in chain:
                bid = (ann.get("seg_ref") or {}).get("blob_id")
                if not bid or bid in seen: continue
                seen.add(bid)
                if (cid, bid) in self._segVolumes: continue
                work.append((cid, bid))
        if not work:
            return

        # Stop the previous worker if it's still grinding on stale work.
        self._volumeStopFlag["stop"] = True
        new_stop = {"stop": False}
        self._volumeStopFlag = new_stop
        # Also register the new stop flag on the `slicer` python module so a
        # Reload of LNQStudio can drain in-flight workers spawned by the
        # prior load. Without this, the stale worker keeps emitting into a
        # torn-down WorklistWindow and crashes.
        if not hasattr(slicer, "_lnq_volume_stop_flags"):
            slicer._lnq_volume_stop_flags = []
        slicer._lnq_volume_stop_flags.append(new_stop)

        import threading
        def _run(work, stop_flag, client, proxy):
            import os
            try:
                import SimpleITK as sitk
            except Exception as exc:
                logging.warning("SimpleITK unavailable: %s", exc); return
            for case_id, blob_id in work:
                if stop_flag["stop"]: return
                # One blanket try/except per case: any chronicle / network /
                # filesystem hiccup just gets logged and skipped so the
                # worker thread can't die mid-iteration. The TimeoutError on
                # resolve_ref was previously crashing the thread because the
                # try only wrapped client.get.
                try:
                    try:
                        blob = client.get(blob_id)
                    except Exception as exc:
                        logging.warning("blob %s: %s", blob_id, exc); continue
                    derived = blob.get("derived_metrics") or {}
                    vol_ml = derived.get("lymph_node_volume_ml")
                    if vol_ml is None:
                        # Need to compute from the file. Synthesize a ref so
                        # resolve_ref handles legacy + blob-keyed paths uniformly.
                        try:
                            path = client.resolve_ref({"blob_id": blob_id})
                        except Exception as exc:
                            logging.warning("resolve_ref %s: %s", blob_id, exc)
                            continue
                        if not path or not os.path.exists(path):
                            continue
                        try:
                            img = sitk.ReadImage(path)
                            import numpy as np
                            arr = sitk.GetArrayFromImage(img)
                            voxels = int((arr != 0).sum())
                            sx, sy, sz = img.GetSpacing()
                            vol_ml = voxels * sx * sy * sz / 1000.0
                        except Exception as exc:
                            logging.warning("vol compute %s: %s", blob_id, exc)
                            continue
                        derived["lymph_node_volume_ml"] = float(vol_ml)
                        derived["computed_by"] = "LNQStudio.WorklistSection"
                        derived["computed_at"] = datetime.datetime.now(
                            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        blob["derived_metrics"] = derived
                        try:
                            client.put(blob)
                        except Exception as exc:
                            logging.warning("blob PUT %s: %s", blob_id, exc)
                    # Marshal back to the GUI thread.
                    proxy.ready.emit(case_id, float(vol_ml))
                except Exception as exc:
                    logging.warning("volume worker case %s: %s", case_id, exc)
                    continue
        self._volumeWorker = threading.Thread(
            target=_run,
            args=(work, new_stop, client, self._volumeProxy),
            daemon=True,
            name="LNQ-volume-worker")
        self._volumeWorker.start()

    def _onVolumeReady(self, case_id, vol_ml):
        """GUI-thread slot. Records the result in the per-blob cache (paths
        through the chain to find which blob produced this case's value are
        re-derived by _maxVolumeForCase), then re-pushes just the affected
        row's cells to the worklist table."""
        chain = self._chains.get(case_id, [])
        # Find the blob_id we just got a value for. The worker emits one
        # signal per (case, blob), but we only carry case_id over the Qt
        # signal. Cheapest: store volume keyed by every blob_id in the
        # chain that hasn't been recorded yet — the value is the same and
        # the worker will overwrite with the per-blob result later if it
        # differs. For typical use (one seg per annotation) this is exact.
        for ann in chain:
            bid = (ann.get("seg_ref") or {}).get("blob_id")
            if not bid: continue
            if (case_id, bid) not in self._segVolumes:
                self._segVolumes[(case_id, bid)] = vol_ml
                break
        # Re-render the worklist row for this case so the cell flips from
        # "—" to the numeric value.
        win = self._owner.worklistWindow(create=False)
        if win is not None:
            win._updateVolumeCell(case_id, self._maxVolumeForCase(case_id))

    def _onAnnotationsChanged(self):
        """Fires (on the GUI thread) when the changes-feed watcher saw an
        Annotation change. Re-pulls chains for the active project and pushes
        the new state into the worklist + current-case panel. Cheap — a
        single `list_by_type('Annotation')` round-trip."""
        if self._currentProject is None:
            return
        client = self._owner.getClient()
        if client is None:
            return
        try:
            self._chains = client.annotation_chains_by_case(
                self._currentProject["_id"])
        except chronicle_client.ChronicleError as exc:
            logging.warning("changes-feed refresh failed: %s", exc)
            return
        # Re-pull cached volumes synchronously so the new annotations'
        # already-computed values surface immediately.
        self._readCachedVolumesSync()
        self._propagateToWorklist()
        # New annotations may have introduced new seg blobs; compute their
        # volumes opportunistically (worker no-ops for already-cached pairs).
        self._kickVolumeWorker()
        # If a case is currently displayed, refresh its summary header (status
        # + history) without reloading the scene. Avoid clobbering whatever
        # the user has unsaved in the status combo / notes box.
        if self._currentCaseId is not None:
            chain = self._chains.get(self._currentCaseId, [])
            history_summary = " → ".join(
                f"{a.get('producer', {}).get('label') or '?'}={a.get('status', '?')[:3]}"
                for a in chain
            ) or "(no annotations)"
            case = self._caseFor(self._currentCaseId)
            if case is not None:
                primary = case.get("primary_site") or "?"
                sex = case.get("sex") or "?"
                self._caseSummaryLabel.text = (
                    f"<b>{self._currentCaseId}</b>  &mdash;  {primary} ({sex})"
                    f"<br><small>history: {history_summary}</small>"
                )

    # ----- helpers -----

    def _caseFor(self, case_id):
        for case in self._cases:
            if case["case_id"] == case_id:
                return case
        return None

    def _statusFor(self, case_id):
        chain = self._chains.get(case_id, [])
        return chain[-1].get("status", "todo") if chain else "todo"

    def _notesFor(self, case_id):
        chain = self._chains.get(case_id, [])
        return chain[-1].get("notes", "") if chain else ""

    def _setEnabledForCase(self, on):
        for w in (self._statusCombo, self._notesEdit, self._prevButton,
                  self._nextButton, self._saveStatusButton, self._saveSegButton):
            w.enabled = on

    def _propagateToWorklist(self):
        dock = self._owner.worklistWindow(create=False)
        if dock is not None:
            dock.setCases(self._role, self._currentProject, self._cases)

    def _onWorklistButton(self, _checked=False):
        self._owner.setWorklistVisible(self._worklistButton.checked)
        win = self._owner.worklistWindow(create=False)
        if win is None:
            return
        win.setCases(self._role, self._currentProject, self._cases)
        win.activateRole(self._role)
        if not win.isVisible():
            win.show()
            win.raise_()
        else:
            win.raise_()


def _hasMemberRole(project_doc, user, role):
    for member in project_doc.get("members", []):
        if member.get("user") == user and member.get("role") == role:
            return True
    return False


# =============================================================================
# Worklist popout dock — shared between Annotate and Review
# =============================================================================

class WorklistWindow(qt.QWidget):
    """Top-level window (own taskbar entry) with one tab per role
    (annotator / reviewer). Each tab holds a sortable table of cases for the
    currently-selected project in that role's section. Visibility is toggled
    from the LNQ toolbar and from the section buttons; closing the window
    syncs both."""

    _COLS = ("status", "case_id", "vol_ml", "primary_site", "sex", "history", "notes")
    _COL_HEADERS = {
        "status": "Status",
        "case_id": "Case",
        "vol_ml": "Vol (mL)",
        "primary_site": "Site / Disease",
        "sex": "Sex",
        "history": "Versions",
        "notes": "Notes",
    }

    _GEOMETRY_KEY = _SETTINGS_PREFIX + "worklist_geometry"

    def __init__(self, owner):
        # Parent on the main window so we share its lifetime, but Qt.Window
        # makes us a real top-level window: own taskbar entry, lives on its
        # own when the main window is backgrounded.
        qt.QWidget.__init__(self, slicer.util.mainWindow(), qt.Qt.Window)
        self.setWindowTitle("LNQ Worklist")
        self._owner = owner

        outerLayout = qt.QVBoxLayout(self)

        self._roleTabs = qt.QTabWidget()
        outerLayout.addWidget(self._roleTabs)

        # One sub-tab per role; each contains a header label + QTableWidget.
        self._tables = {}
        self._headers = {}
        for role, title in (("annotator", "Annotate"), ("reviewer", "Review")):
            page = qt.QWidget()
            pageLayout = qt.QVBoxLayout(page)
            header = qt.QLabel("(no project)")
            header.wordWrap = True
            pageLayout.addWidget(header)
            self._headers[role] = header

            table = qt.QTableWidget()
            table.columnCount = len(self._COLS)
            for col, name in enumerate(self._COLS):
                table.setHorizontalHeaderItem(
                    col, qt.QTableWidgetItem(self._COL_HEADERS[name])
                )
            table.horizontalHeader().setStretchLastSection(True)
            table.verticalHeader().visible = False
            table.editTriggers = qt.QAbstractItemView.NoEditTriggers
            table.selectionBehavior = qt.QAbstractItemView.SelectRows
            table.selectionMode = qt.QAbstractItemView.SingleSelection
            table.setSortingEnabled(True)
            # Double-click loads (with confirmation). Single-click only changes
            # the visible selection — does NOT touch the scene.
            table.cellDoubleClicked.connect(
                lambda row, _col, r=role: self._onCellDoubleClicked(r, row)
            )
            pageLayout.addWidget(table)
            self._tables[role] = table
            self._roleTabs.addTab(page, title)

        # ---- Inference Review tab ----
        # Driven by idc-batch-qc.py's qc.csv (not chronicle Annotations).
        # Each row is one case in an IDC-ingested cohort with the model's
        # SEG + probability volume pre-computed. Double-click activates
        # LNQReview module + loads the four NRRDs.
        self._inferenceCohortSection = None
        try:
            from LNQReviewLib.cohort_list import CohortListSection
            inferencePage = qt.QWidget()
            inferenceLayout = qt.QVBoxLayout(inferencePage)
            self._inferenceCohortSection = CohortListSection()
            self._inferenceCohortSection.caseActivated.connect(
                self._onInferenceCaseActivated)
            inferenceLayout.addWidget(self._inferenceCohortSection.widget)
            self._roleTabs.addTab(inferencePage, "Inference Review")
        except Exception as exc:
            logging.warning(
                "LNQ Worklist: Inference Review tab unavailable (%s). "
                "Ensure the LNQReview module is on the Slicer module path.",
                exc)

        # Restore prior geometry (size + position, including which screen) if
        # we've been shown before. Falls back to a sensible default size, but
        # *no explicit position* so the window manager can place it.
        if not self._restoreGeometry():
            self.resize(900, 600)

    # ----- inference-review bridge -----

    def _onInferenceCaseActivated(self, case_id):
        """Cohort table row double-clicked. Switch to LNQReview module
        and hand it the case to load. Raise the Slicer main window on
        top of the worklist so the reviewer's eye lands on the slice
        views they just summoned (the worklist is a separate top-level
        QMainWindow and otherwise stays above Slicer on macOS)."""
        if self._inferenceCohortSection is None:
            return
        data_root = self._inferenceCohortSection.dataRoot
        model = self._inferenceCohortSection.modelName
        try:
            slicer.util.selectModule("LNQReview")
            review_widget = slicer.modules.lnqreview.widgetRepresentation().self()
            if hasattr(review_widget, "loadFromCohort"):
                review_widget.loadFromCohort(data_root, model, case_id)
            else:
                slicer.util.errorDisplay(
                    "LNQReview module is registered but doesn't expose "
                    "loadFromCohort() — check the SlicerLNQ install.")
            # Raise Slicer over the worklist after the double-click
            # signal handler returns. Direct raise_() from inside the
            # handler is fighting macOS's focus state — the worklist
            # received the click and Qt re-activates it as the handler
            # unwinds, so the raise never sticks. Defer to the next
            # event-loop tick and lower the worklist to break the tie.
            main = slicer.util.mainWindow()
            worklist = self
            def _bringSlicerForward():
                if main is not None:
                    main.show()
                    main.raise_()
                    main.activateWindow()
                if worklist is not None:
                    worklist.lower()
            qt.QTimer.singleShot(0, _bringSlicerForward)
        except Exception as exc:
            logging.exception("Inference Review activation failed")
            slicer.util.errorDisplay(
                f"Could not switch to LNQReview module: {exc}\n\n"
                f"Make sure LNQReview is on the Slicer module path.")

    def _restoreGeometry(self):
        geom = qt.QSettings().value(self._GEOMETRY_KEY)
        if geom:
            try:
                return self.restoreGeometry(geom)
            except Exception:
                return False
        return False

    def _persistGeometry(self):
        qt.QSettings().setValue(self._GEOMETRY_KEY, self.saveGeometry())

    def hideEvent(self, event):
        # Catches both X-close and programmatic hide via toolbar / section
        # button. We persist on every hide so geometry survives crashes too.
        self._persistGeometry()
        qt.QWidget.hideEvent(self, event)

    def closeEvent(self, event):
        # X-close path: also keep the toolbar action + section buttons in
        # sync so reopening via either path works.
        self._persistGeometry()
        qt.QWidget.closeEvent(self, event)
        if self._owner is not None:
            self._owner._syncWorklistVisibilityState(False)

    # ----- public API used by WorklistSection -----

    def activateRole(self, role):
        for i in range(self._roleTabs.count):
            if (role == "annotator" and i == 0) or (role == "reviewer" and i == 1):
                self._roleTabs.currentIndex = i
                return

    def setCases(self, role, project, cases):
        if role not in self._tables:
            return
        header = self._headers[role]
        table = self._tables[role]

        if project is None:
            header.text = "(no project selected)"
            table.rowCount = 0
            return

        section = self._sectionForRole(role)
        counts = {s: 0 for s in _STATUSES}
        for case in cases:
            counts[section._statusFor(case["case_id"])] += 1
        header.text = (
            f"<b>{project.get('name', '?')}</b>  &nbsp;  "
            f"<span style='color:#27ae60'>{counts['approved']} approved</span> / "
            f"<span style='color:#2980b9'>{counts['submitted_for_review']} submitted</span> / "
            f"<span style='color:#d35400'>{counts['needs_changes']} needs changes</span> / "
            f"<span style='color:#f39c12'>{counts['in_progress']} in progress</span> / "
            f"<span style='color:#8e44ad'>{counts['needs_consultation']} consult</span> / "
            f"<span style='color:#c0392b'>{counts['todo']} todo</span>"
            f"  &mdash; {len(cases)} total"
        )

        table.setSortingEnabled(False)
        table.rowCount = len(cases)
        for r, case in enumerate(cases):
            case_id = case["case_id"]
            status = section._statusFor(case_id)
            chain = section._chains.get(case_id, [])
            history_str = " ".join(
                (a.get("producer", {}).get("label") or "?")
                for a in chain
            ) or "-"
            row_values = {
                "status": _STATUS_LABEL[status],
                "case_id": case_id,
                "primary_site": case.get("primary_site") or "",
                "sex": case.get("sex") or "",
                "history": history_str,
                "notes": (section._notesFor(case_id) or "").split("\n", 1)[0][:80],
            }
            vol_ml = section._maxVolumeForCase(case_id)
            for c, name in enumerate(self._COLS):
                if name == "vol_ml":
                    # Use DisplayRole=float so Qt sorts numerically (lexical
                    # sort on text would put "10.0" before "2.0"). Show "—"
                    # when the volume hasn't landed yet — the background
                    # compute thread will rewrite the cell when it does.
                    item = qt.QTableWidgetItem()
                    if vol_ml is None:
                        item.setText("—")
                    else:
                        item.setData(qt.Qt.DisplayRole, round(vol_ml, 2))
                else:
                    item = qt.QTableWidgetItem(row_values[name])
                item.setData(qt.Qt.UserRole, case_id)
                if name == "status":
                    item.setBackground(qt.QBrush(_STATUS_COLOR[status]))
                    item.setForeground(qt.QBrush(qt.QColor("white")))
                if name == "case_id" and case.get("study_uid"):
                    item.setToolTip(case["study_uid"])
                table.setItem(r, c, item)
        table.setSortingEnabled(True)
        table.resizeColumnsToContents()

    def _updateVolumeCell(self, case_id, vol_ml):
        """Worker-thread callback (marshalled via _VolumeReadyProxy.ready).
        Find the row(s) for this case_id across all role tabs and rewrite
        the Vol (mL) cell numerically so the sort works."""
        try:
            col = self._COLS.index("vol_ml")
        except ValueError:
            return
        for table in self._tables.values():
            # Suspend sorting while we mutate the cell so the row doesn't
            # jump out from under any active selection.
            try:
                was_sorting = table.isSortingEnabled()
            except (ValueError, RuntimeError):
                # PythonQt raises when the C++ QTableWidget has been
                # destroyed — happens after a Reload while the volume
                # worker still holds a Python ref to the old window.
                continue
            table.setSortingEnabled(False)
            try:
                for r in range(table.rowCount):
                    id_item = table.item(r, 1)  # case_id column
                    if id_item is None or id_item.text() != case_id:
                        continue
                    item = qt.QTableWidgetItem()
                    item.setData(qt.Qt.UserRole, case_id)
                    if vol_ml is None:
                        item.setText("—")
                    else:
                        item.setData(qt.Qt.DisplayRole, round(vol_ml, 2))
                    table.setItem(r, col, item)
            except (ValueError, RuntimeError):
                continue
            finally:
                try:
                    table.setSortingEnabled(was_sorting)
                except (ValueError, RuntimeError):
                    pass

    def highlightCase(self, role, case_id):
        table = self._tables.get(role)
        if table is None:
            return
        for r in range(table.rowCount):
            it = table.item(r, 0)
            if it is not None and it.data(qt.Qt.UserRole) == case_id:
                table.blockSignals(True)
                table.selectRow(r)
                table.scrollToItem(it, qt.QAbstractItemView.PositionAtCenter)
                table.blockSignals(False)
                return

    # ----- internal -----

    def _sectionForRole(self, role):
        return self._owner._annotate if role == "annotator" else self._owner._review

    def _onCellDoubleClicked(self, role, row):
        table = self._tables[role]
        item = table.item(row, 0)
        if item is None:
            return
        case_id = item.data(qt.Qt.UserRole)
        btn = qt.QMessageBox.question(
            self,
            "Load case",
            f"Close the current scene and load case <b>{case_id}</b>?",
            qt.QMessageBox.Ok | qt.QMessageBox.Cancel,
            qt.QMessageBox.Ok,
        )
        if btn != qt.QMessageBox.Ok:
            return
        self._sectionForRole(role)._loadCase(case_id)


# =============================================================================
# Tests (smoke)
# =============================================================================

class LNQStudioTest(ScriptedLoadableModuleTest):
    def setUp(self):
        slicer.mrmlScene.Clear(0)

    def runTest(self):
        self.setUp()
        self.test_LoadModule()

    def test_LoadModule(self):
        self.delayDisplay("Loading LNQStudio module")
        slicer.util.selectModule("LNQStudio")
        self.delayDisplay("LNQStudio loaded")
