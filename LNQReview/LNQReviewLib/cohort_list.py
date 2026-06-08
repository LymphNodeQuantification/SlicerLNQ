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
]


def discover_qc_csv(data_root, model_name):
    """Return the path to qc.csv for a given cohort root + model name,
    or None if it isn't there yet."""
    path = os.path.join(data_root, "qc", model_name, "qc.csv")
    return path if os.path.isfile(path) else None


def derive_case_paths(data_root, model_name, case_id):
    """Apply the standard directory convention to find this case's
    four-NRRD set + tooltip PNG. Missing files are returned as None
    so the caller can warn rather than crash."""
    nrrd_dir = os.path.join(data_root, "nrrd")
    pred_dir = os.path.join(data_root, "predictions", model_name)
    qc_dir   = os.path.join(data_root, "qc", model_name)
    return {
        "ct":         _opt(os.path.join(nrrd_dir, f"{case_id}_0000.nrrd")),
        "gt":         _opt(os.path.join(nrrd_dir, f"{case_id}.nrrd")),
        "model_seg":  _opt(os.path.join(pred_dir, f"{case_id}.nrrd")),
        "model_prob": _opt(os.path.join(pred_dir, f"{case_id}-prob.nrrd")),
        "tooltip_png": _opt(os.path.join(qc_dir, f"{case_id}.png")),
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


class _SortableItem(qt.QTableWidgetItem):
    """QTableWidgetItem that sorts by a stashed UserRole numeric value
    rather than the displayed string. Crucial — without this Qt sorts
    "0.7" > "0.45" lexically and you get the wrong order."""
    def __lt__(self, other):
        a = self.data(qt.Qt.UserRole)
        b = other.data(qt.Qt.UserRole) if isinstance(other, qt.QTableWidgetItem) else None
        if a is None and b is None: return False
        if a is None: return True
        if b is None: return False
        return a < b


class CohortListSection(qt.QObject):
    """The cohort-list widget + its model. Connect to caseActivated
    to be notified when the reviewer picks a case to load."""

    caseActivated = qt.Signal(str)   # case_id

    def __init__(self, parent=None):
        qt.QObject.__init__(self, parent)
        self._dataRoot = ""
        self._modelName = "mediastinal-v1"
        self._rows = []
        self._widget = qt.QWidget()
        self._buildUI()

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

        # Cohort picker row.
        row = qt.QHBoxLayout()
        self._rootEdit = qt.QLineEdit()
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

        self._table = qt.QTableWidget()
        self._table.setColumnCount(len(COLUMNS))
        self._table.setHorizontalHeaderLabels([c[0] for c in COLUMNS])
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self._table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.connect("itemDoubleClicked(QTableWidgetItem*)",
                            self._onRowDoubleClicked)
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
        self._table.setRowCount(len(rows))
        for r_idx, row in enumerate(rows):
            for c_idx, (_label, key, align, kind) in enumerate(COLUMNS):
                item = self._buildCell(row, key, kind)
                if item is None:
                    item = _SortableItem("")
                item.setTextAlignment(align | qt.Qt.AlignVCenter)
                # First column also carries the case_id payload for the
                # double-click handler.
                if c_idx == 0:
                    item.setData(qt.Qt.UserRole + 1, row.get("case_id"))
                # Tooltip on every cell in the row points at the PNG.
                tooltip = self._buildTooltip(row.get("case_id"))
                if tooltip:
                    item.setToolTip(tooltip)
                self._table.setItem(r_idx, c_idx, item)
        self._table.resizeColumnsToContents()
        # Stretch the last column so the visual edges are clean.
        self._table.horizontalHeader().setSectionResizeMode(
            len(COLUMNS) - 1, qt.QHeaderView.Stretch)
        self._table.setSortingEnabled(True)

    def _buildCell(self, row, key, kind):
        val = row.get(key)
        if kind == "str":
            it = _SortableItem(str(val or ""))
            it.setData(qt.Qt.UserRole, str(val or ""))
            return it
        # numeric kinds
        try:
            f = float(val) if val not in (None, "", "True", "False") else None
        except (TypeError, ValueError):
            f = None
        if f is None:
            it = _SortableItem("")
            return it
        fmt = {"num2": "{:.2f}", "num3": "{:.3f}", "num4": "{:.4f}",
                "delta": "{:+.4f}"}.get(kind, "{}")
        it = _SortableItem(fmt.format(f))
        it.setData(qt.Qt.UserRole, f)
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

    def _onRowDoubleClicked(self, item):
        case_id_item = self._table.item(item.row(), 0)
        case_id = case_id_item.data(qt.Qt.UserRole + 1) if case_id_item else None
        if case_id:
            logging.info("LNQReview: case activated %s", case_id)
            self.caseActivated.emit(case_id)
