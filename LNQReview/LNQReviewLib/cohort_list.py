"""Cohort case-list table for LNQReview.

This is the entry point a reviewer actually starts at: a sortable
spreadsheet of every case in the current review project, driven by
the qc.csv that idc-batch-qc.py produces. Each row has the case_id +
Dice + sensitivity + precision + per-anatomy volumes, plus a tooltip
that shows the per-case overlay PNG (so the reviewer can scan-with-
their-eyes which cases to triage first without loading each one), and
a double-click handler that switches the LNQReview module into its
focused single-case mode pointed at the row's case.

The expected directory layout matches what ingest-idc-cohort.py +
idc-batch-qc.py produce on Manila — same conventions on any
mounted/staged copy:

    <data_root>/
      nrrd/<case_id>_0000.nrrd                     # CT
      nrrd/<case_id>.nrrd                          # ground-truth SEG (optional)
      predictions/<model>/<case_id>.nrrd           # model SEG
      predictions/<model>/<case_id>-prob.nrrd      # model probability
      qc/<model>/qc.csv                            # this table
      qc/<model>/<case_id>.png                     # per-case tooltip preview
"""
from __future__ import annotations

import csv
import logging
import os
from typing import Optional

import qt
import slicer


# Columns shown in the table, in display order. The list of (header,
# csv_key, alignment, kind) is the single source of truth — adding a
# column here is the only change needed to surface a new QC stat.
# The trailing non-mediastinal anatomy columns are populated by running
# idc-batch-qc.py with --extra-anatomies; cells stay blank for cohorts
# that haven't had those models run yet (the QC writer emits "" then).
# The Notes column is special: editable in-place; on edit, the new text
# is persisted to Chronicle via chronicle_notes.write_note().
COLUMNS = [
    ("Case",                "case_id",                       qt.Qt.AlignLeft,   "str"),
    ("Δ rescue",            "_rescue",                       qt.Qt.AlignRight,  "delta"),
    ("Dice p≥0.5",          "dice_p0.5",                     qt.Qt.AlignRight,  "num4"),
    ("Dice p≥0.001",        "dice_p0.001",                   qt.Qt.AlignRight,  "num4"),
    ("Sens p≥0.5",          "sensitivity_p0.5",              qt.Qt.AlignRight,  "num3"),
    ("Prec p≥0.5",          "precision_p0.5",                qt.Qt.AlignRight,  "num3"),
    ("GT (mL)",             "gt_volume_mL",                  qt.Qt.AlignRight,  "num2"),
    ("Pred p≥0.5 (mL)",     "pred_volume_mL_p0.5",           qt.Qt.AlignRight,  "num2"),
    ("Missed > p≥0.001",    "missed_above_p0.001_frac",      qt.Qt.AlignRight,  "num3"),
    ("Abd/pelv (mL)",       "abdominopelvic-v1_volume_mL",   qt.Qt.AlignRight,  "num2"),
    ("Axillary (mL)",       "axillary-v1_volume_mL",         qt.Qt.AlignRight,  "num2"),
    ("Inguinal (mL)",       "inguinal-v1_volume_mL",         qt.Qt.AlignRight,  "num2"),
    ("Notes",               "_notes",                        qt.Qt.AlignLeft,   "notes"),
]


def discover_qc_csv(data_root, model_name):
    """Return the path to qc.csv for a given cohort root + model name,
    or None if it isn't there yet."""
    path = os.path.join(data_root, "qc", model_name, "qc.csv")
    return path if os.path.isfile(path) else None


def derive_case_paths(data_root, model_name, case_id):
    """Apply the standard directory convention to find this case's
    four-NRRD set + tooltip PNG plus any *additional* per-anatomy
    predictions sitting under predictions/<other-model>/. Missing files
    are returned as None so the caller can warn rather than crash.

    `extra_anatomies` is the list of non-primary models that have a SEG
    on disk for this case — each entry is
        {"name": ..., "seg_path": ..., "prob_path": ... or None}
    The reviewer needs all of them loaded at once so they can spot
    abdominopelvic / axillary / inguinal nodes the primary model would
    have missed."""
    nrrd_dir = os.path.join(data_root, "nrrd")
    pred_root = os.path.join(data_root, "predictions")
    pred_dir = os.path.join(pred_root, model_name)
    qc_dir   = os.path.join(data_root, "qc", model_name)
    extra_anatomies = []
    if os.path.isdir(pred_root):
        for sub in sorted(os.listdir(pred_root)):
            if sub == model_name:
                continue
            seg_path  = os.path.join(pred_root, sub, f"{case_id}.nrrd")
            prob_path = os.path.join(pred_root, sub, f"{case_id}-prob.nrrd")
            if not os.path.isfile(seg_path):
                continue
            extra_anatomies.append({
                "name": sub,
                "seg_path": seg_path,
                "prob_path": prob_path if os.path.isfile(prob_path) else None,
            })
    return {
        "ct":         _opt(os.path.join(nrrd_dir, f"{case_id}_0000.nrrd")),
        "gt":         _opt(os.path.join(nrrd_dir, f"{case_id}.nrrd")),
        "model_seg":  _opt(os.path.join(pred_dir, f"{case_id}.nrrd")),
        "model_prob": _opt(os.path.join(pred_dir, f"{case_id}-prob.nrrd")),
        "tooltip_png": _opt(os.path.join(qc_dir, f"{case_id}.png")),
        "extra_anatomies": extra_anatomies,
    }


def _opt(path):
    return path if os.path.isfile(path) else None


def load_qc_rows(csv_path):
    """Parse qc.csv and tack on a derived 'rescue Δ' column so the
    table can sort by 'biggest calibration win' out of the box."""
    rows = []
    with open(csv_path, newline="") as f:
        for r in csv.DictReader(f):
            d_low  = _maybe_float(r.get("dice_p0.001"))
            d_high = _maybe_float(r.get("dice_p0.5"))
            r["_rescue"] = (d_low - d_high) if (d_low is not None and d_high is not None) else None
            rows.append(r)
    return rows


def _maybe_float(s):
    try:
        return float(s) if s not in (None, "", "True", "False") else None
    except (TypeError, ValueError):
        return None


# Sort role for numeric columns. Qt's QSortFilterProxyModel compares
# this role via QVariant — so a Python float stashed here sorts
# numerically, while the displayed cell text stays human-readable.
# Subclassing QTableWidgetItem and overriding __lt__ does NOT work
# under PythonQt (Slicer's binding) — operator< is C++-only there,
# which is why the previous _SortableItem class sorted lexically.
_SORT_ROLE = qt.Qt.UserRole + 100


class CohortListSection(qt.QObject):
    """The cohort-list widget + its model. Connect to caseActivated
    to be notified when the reviewer picks a case to load."""

    caseActivated = qt.Signal(str)   # case_id

    # QSettings keys for the most recently loaded cohort. Restored on
    # construction + saved on every successful Load click so reviewers
    # don't have to re-browse on each Slicer restart.
    _SETTINGS_DATA_ROOT_KEY = "LNQReview/cohortDataRoot"
    _SETTINGS_MODEL_KEY = "LNQReview/cohortModelName"

    def __init__(self, parent=None):
        qt.QObject.__init__(self, parent)
        s = qt.QSettings()
        self._dataRoot = s.value(self._SETTINGS_DATA_ROOT_KEY, "") or ""
        self._modelName = s.value(self._SETTINGS_MODEL_KEY, "") or "mediastinal-v1"
        self._rows = []
        # Chronicle-backed per-case notes. fetch_notes() on cohort load,
        # write_note() on each edit. Missing / unreachable Chronicle is
        # tolerated (the client silently degrades to in-memory).
        try:
            from LNQReviewLib.chronicle_notes import ChronicleNotesClient
            self._notesClient = ChronicleNotesClient()
        except Exception as exc:
            logging.warning("ChronicleNotesClient init failed: %s", exc)
            self._notesClient = None
        self._notes = {}            # {case_id: text}
        self._suspendNotesWrite = False
        self._widget = qt.QWidget()
        self._buildUI()
        # Auto-load if the remembered cohort still exists on disk. Falls
        # through silently otherwise — the user sees the picker.
        if self._dataRoot and os.path.isdir(self._dataRoot):
            try:
                self._onLoadCohort()
            except Exception as exc:
                logging.warning("auto-load of remembered cohort failed: %s",
                                exc)

    @property
    def widget(self):
        return self._widget

    @property
    def dataRoot(self):
        return self._dataRoot

    @property
    def modelName(self):
        return self._modelName

    # ----- UI -----

    def _buildUI(self):
        v = qt.QVBoxLayout(self._widget)

        # Cohort picker row. Pre-fills from QSettings so the most-recently
        # loaded cohort is one click away on the next Slicer launch.
        row = qt.QHBoxLayout()
        self._rootEdit = qt.QLineEdit(self._dataRoot)
        self._rootEdit.setPlaceholderText(
            "Data root (e.g. /media/share/LNQ-data/idc/ct_lymph_nodes)")
        browse = qt.QPushButton("Browse…")
        browse.connect("clicked()", self._onBrowse)
        row.addWidget(qt.QLabel("Cohort:"), 0)
        row.addWidget(self._rootEdit, 1)
        row.addWidget(browse, 0)
        v.addLayout(row)

        row2 = qt.QHBoxLayout()
        self._modelEdit = qt.QLineEdit(self._modelName)
        self._modelEdit.setMaximumWidth(180)
        self._loadButton = qt.QPushButton("Load")
        self._loadButton.connect("clicked()", self._onLoadCohort)
        self._statusLabel = qt.QLabel("(no cohort loaded)")
        self._statusLabel.setStyleSheet("color: gray;")
        row2.addWidget(qt.QLabel("Model:"), 0)
        row2.addWidget(self._modelEdit, 0)
        row2.addWidget(self._loadButton, 0)
        row2.addWidget(self._statusLabel, 1)
        v.addLayout(row2)

        # Sort hint + the table.
        self._hintLabel = qt.QLabel(
            "Click column headers to sort. Hover a row to preview "
            "the per-case overlay PNG. Double-click to load.")
        self._hintLabel.setStyleSheet("color: gray; padding-bottom: 4px;")
        v.addWidget(self._hintLabel)

        # QTableView + QStandardItemModel + QSortFilterProxyModel so
        # numeric sort honors the float stashed in _SORT_ROLE. The older
        # QTableWidget + __lt__ override approach silently fell back to
        # lexical sort under PythonQt.
        self._model = qt.QStandardItemModel()
        self._model.setColumnCount(len(COLUMNS))
        self._model.setHorizontalHeaderLabels([c[0] for c in COLUMNS])
        self._proxy = qt.QSortFilterProxyModel()
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(_SORT_ROLE)
        self._table = qt.QTableView()
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self._table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        # Notes column is editable on a single click. All other columns
        # stay read-only; per-cell flags are applied in _buildCell.
        self._table.setEditTriggers(
            qt.QAbstractItemView.SelectedClicked
            | qt.QAbstractItemView.EditKeyPressed
            | qt.QAbstractItemView.AnyKeyPressed)
        self._table.setSortingEnabled(True)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.doubleClicked.connect(self._onRowDoubleClicked)
        # Persist edits as they happen. dataChanged fires for every
        # itemChanged path including programmatic setItem, so we gate
        # write-back via _suspendNotesWrite during populate.
        self._model.dataChanged.connect(self._onModelDataChanged)
        v.addWidget(self._table, 1)

    # ----- cohort load -----

    def setDataRoot(self, path, model_name=None):
        """Set the cohort path + model name and reload the table."""
        self._dataRoot = path
        if model_name:
            self._modelName = model_name
            self._modelEdit.setText(model_name)
        self._rootEdit.setText(path)
        self._onLoadCohort()

    def _onBrowse(self):
        chooser = qt.QFileDialog()
        chooser.setFileMode(qt.QFileDialog.Directory)
        chooser.setOption(qt.QFileDialog.ShowDirsOnly, True)
        if self._rootEdit.text:
            chooser.setDirectory(self._rootEdit.text)
        if chooser.exec_() == qt.QDialog.Accepted:
            self._rootEdit.setText(chooser.selectedFiles()[0])

    def _onLoadCohort(self):
        root = self._rootEdit.text.strip()
        model = self._modelEdit.text.strip() or "mediastinal-v1"
        if not root or not os.path.isdir(root):
            self._statusLabel.setText(f"data root not a directory: {root}")
            return
        csv_path = discover_qc_csv(root, model)
        if csv_path is None:
            self._statusLabel.setText(
                f"no qc/{model}/qc.csv under {root} — "
                f"run idc-batch-qc.py first")
            self._populateTable([])
            return
        try:
            rows = load_qc_rows(csv_path)
        except Exception as exc:
            self._statusLabel.setText(f"qc.csv read failed: {exc}")
            self._populateTable([])
            return
        self._dataRoot = root
        self._modelName = model
        self._rows = rows
        # Persist for the next Slicer launch.
        s = qt.QSettings()
        s.setValue(self._SETTINGS_DATA_ROOT_KEY, root)
        s.setValue(self._SETTINGS_MODEL_KEY, model)
        # Pull stored notes for this cohort so the Notes column comes up
        # populated. One bulk POST against _all_docs?include_docs=true.
        self._notes = {}
        if self._notesClient is not None and self._notesClient.configured:
            try:
                case_ids = [r.get("case_id") for r in rows if r.get("case_id")]
                self._notes = self._notesClient.fetch_notes(
                    root, model, case_ids) or {}
            except Exception as exc:
                logging.warning("fetch_notes failed: %s", exc)
        self._populateTable(rows)
        self._statusLabel.setText(
            f"{len(rows)} cases from {csv_path}. Sorted by rescue Δ; "
            f"click any column to re-sort.")
        # Default sort: rescue Δ descending so the calibration-rescue
        # archetypes surface at the top.
        for i, c in enumerate(COLUMNS):
            if c[1] == "_rescue":
                self._table.sortByColumn(i, qt.Qt.DescendingOrder)
                break

    # ----- table population -----

    def _populateTable(self, rows):
        self._table.setSortingEnabled(False)
        self._suspendNotesWrite = True
        try:
            self._model.removeRows(0, self._model.rowCount())
            self._model.setRowCount(len(rows))
            for r_idx, row in enumerate(rows):
                case_id = row.get("case_id")
                # Surface the stored note inline so _buildCell can pick
                # it up via the row dict.
                row["_notes"] = self._notes.get(case_id, "")
                for c_idx, (_label, key, align, kind) in enumerate(COLUMNS):
                    item = self._buildCell(row, key, kind)
                    item.setTextAlignment(int(align | qt.Qt.AlignVCenter))
                    if c_idx == 0:
                        # Case-id payload for the double-click handler.
                        item.setData(case_id, qt.Qt.UserRole + 1)
                    if kind == "notes":
                        # Stash the case_id on this cell so the on-edit
                        # slot knows which note to write back.
                        item.setData(case_id, qt.Qt.UserRole + 1)
                    tooltip = self._buildTooltip(case_id)
                    if tooltip:
                        item.setToolTip(tooltip)
                    self._model.setItem(r_idx, c_idx, item)
        finally:
            self._suspendNotesWrite = False
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(
            len(COLUMNS) - 1, qt.QHeaderView.Stretch)
        self._table.setSortingEnabled(True)

    def _buildCell(self, row, key, kind):
        """Return a QStandardItem with the displayed text in DisplayRole
        and the *sortable* value in _SORT_ROLE (float for numeric kinds,
        string for case_id). Empty cells get a -inf sort value so they
        sink to the bottom of any ascending numeric sort.

        kind=="notes" cells are the only writable ones — they keep the
        default ItemIsEditable flag; every other kind clears it so the
        rest of the table stays read-only."""
        val = row.get(key)
        if kind == "notes":
            text = str(val or "")
            it = qt.QStandardItem(text)
            it.setData(text, _SORT_ROLE)
            it.setEditable(True)
            return it
        if kind == "str":
            it = qt.QStandardItem(str(val or ""))
            it.setData(str(val or ""), _SORT_ROLE)
            it.setEditable(False)
            return it
        try:
            f = float(val) if val not in (None, "", "True", "False") else None
        except (TypeError, ValueError):
            f = None
        if f is None:
            it = qt.QStandardItem("")
            # NaN sentinel — QSortFilterProxyModel treats NaN consistently
            # (bottom in both directions) so empty cells don't elbow real
            # data when the user sorts.
            it.setData(float("-inf"), _SORT_ROLE)
            return it
        fmt = {"num2": "{:.2f}", "num3": "{:.3f}", "num4": "{:.4f}",
                "delta": "{:+.4f}"}.get(kind, "{}")
        it = qt.QStandardItem(fmt.format(f))
        it.setData(f, _SORT_ROLE)
        return it

    def _buildTooltip(self, case_id):
        """Qt rich-text tooltip with the case's overlay PNG embedded so
        the reviewer can preview without clicking. Returns None if the
        PNG isn't present (the row still works for double-click load)."""
        if not case_id or not self._dataRoot:
            return None
        png = os.path.join(self._dataRoot, "qc", self._modelName,
                            f"{case_id}.png")
        if not os.path.isfile(png):
            return None
        # qt rich-text tooltips honor <img src="file://...">. Cap the
        # width so it fits any screen.
        return (
            f"<div><b>{case_id}</b><br>"
            f"<img src=\"file://{png}\" width=\"320\"></div>")

    # ----- notes editing -----

    def _onModelDataChanged(self, topLeft, bottomRight, _roles=None):
        """Persist the Notes cell to Chronicle when the user finishes
        editing. Other cells are read-only so won't reach here, but we
        guard on column index defensively + skip programmatic populate
        via _suspendNotesWrite."""
        if self._suspendNotesWrite:
            return
        # Identify the Notes column from COLUMNS.
        notes_col = None
        for i, c in enumerate(COLUMNS):
            if c[3] == "notes":
                notes_col = i
                break
        if notes_col is None:
            return
        for r in range(topLeft.row(), bottomRight.row() + 1):
            if topLeft.column() > notes_col or bottomRight.column() < notes_col:
                continue
            item = self._model.item(r, notes_col)
            if item is None:
                continue
            case_id = item.data(qt.Qt.UserRole + 1)
            if not case_id:
                continue
            text = item.text() or ""
            # Keep the sort key in sync with the display text.
            item.setData(text, _SORT_ROLE)
            prior = self._notes.get(case_id, "")
            if text == prior:
                continue
            self._notes[case_id] = text
            if self._notesClient is not None and self._notesClient.configured:
                try:
                    ok = self._notesClient.write_note(
                        self._dataRoot, self._modelName, case_id, text)
                    if not ok:
                        logging.warning(
                            "Chronicle write_note returned non-ok for %s",
                            case_id)
                except Exception as exc:
                    logging.warning("write_note failed for %s: %s",
                                    case_id, exc)

    def _onRowDoubleClicked(self, proxyIndex):
        # proxyIndex is in the QSortFilterProxyModel's coordinates; map
        # back to the source QStandardItemModel before pulling the cell.
        sourceIndex = self._proxy.mapToSource(proxyIndex)
        row = sourceIndex.row()
        col = sourceIndex.column()
        # Don't treat a double-click on the Notes column as "activate
        # case" — the user is trying to enter the editor instead. Qt
        # will start the edit because the cell is ItemIsEditable.
        if col < len(COLUMNS) and COLUMNS[col][3] == "notes":
            return
        case_id_item = self._model.item(row, 0)
        case_id = case_id_item.data(qt.Qt.UserRole + 1) if case_id_item else None
        if case_id:
            logging.info("LNQReview: case activated %s", case_id)
            self.caseActivated.emit(case_id)
