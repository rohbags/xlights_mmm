#!/usr/bin/env python3
"""xLights Media Metadata Manager - graphical, read-only XSQ scanner.

Run: python app.py
XSQ files are NEVER opened for writing. Only referenced MP3/M4A files can
be modified, and only after clicking Apply Metadata and confirming.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QSignalBlocker
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QCheckBox, QLineEdit, QPushButton, QFileDialog, QLabel, QTableWidget,
    QTableWidgetItem, QAbstractItemView, QProgressBar, QMessageBox, QDialog,
    QPlainTextEdit, QDialogButtonBox, QHeaderView, QGroupBox,
)

from core import (
    FIELDS, Record, apply_one, differences, discover_sequences,
    prepare_jobs, read_tags, scan_sequence, status_from_values,
)

COL_USE = 0
COL_SEQUENCE = 1
COL_MEDIA = 2
COL_SONG = 3
COL_ARTIST = 4
COL_ALBUM = 5
COL_COMMENT = 6
COL_STATUS = 7


class ScanThread(QThread):
    item_ready = Signal(object)
    advance = Signal(int, int, str)

    def __init__(self, folder: Path, recursive: bool, clear_blanks: bool):
        super().__init__()
        self.folder = folder
        self.recursive = recursive
        self.clear_blanks = clear_blanks
        self.outcome = (0, 0, "Scan did not complete")

    def run(self):
        try:
            paths = discover_sequences(self.folder, self.recursive)
            accepted = 0
            for count, path in enumerate(paths, 1):
                result = scan_sequence(path, self.clear_blanks)
                if result is not None:
                    self.item_ready.emit(result)
                    accepted += 1
                self.advance.emit(count, len(paths), path.name)
            self.outcome = (len(paths), accepted, "")
        except Exception as exc:
            self.outcome = (0, 0, f"Scan failed: {exc}")


class OperationThread(QThread):
    result_ready = Signal(int, str, str, object)
    advance = Signal(int, int, str)

    def __init__(self, action: str, records: list[Record], indices: list[int], clear_blanks: bool):
        super().__init__()
        self.action = action
        self.records = records
        self.indices = indices
        self.clear_blanks = clear_blanks
        self.outcome = None

    def run(self):
        stats = {"written": 0, "matched": 0, "different": 0, "failed": 0, "files": 0}
        if self.action == "verify":
            total = len(self.indices)
            for count, idx in enumerate(self.indices, 1):
                record = self.records[idx]
                try:
                    if record.status in {"XSQ error", "Missing media", "Invalid media path", "Unsupported media"}:
                        raise ValueError(record.detail or record.status)
                    actual = read_tags(Path(record.media))
                    diff = differences(record.expected, actual, self.clear_blanks)
                    status = "Different" if diff else "Match"
                    detail = ("; ".join(f"{k.title()}: XSQ={v[0]!r}, audio={v[1]!r}"
                                        for k, v in diff.items()) if diff else "All applicable audio tags match.")
                    stats["different" if diff else "matched"] += 1
                    self.result_ready.emit(idx, status, detail, actual)
                except Exception as exc:
                    stats["failed"] += 1
                    self.result_ready.emit(idx, "Media error", str(exc), {})
                self.advance.emit(count, total, Path(record.media).name or Path(record.sequence).name)
            stats["files"] = total
            self.outcome = stats
            return

        jobs, initial_failures = prepare_jobs(self.records, self.indices, self.clear_blanks)
        for idx, (status, detail) in initial_failures.items():
            self.result_ready.emit(idx, status, detail, {})
        stats["failed"] += len(initial_failures)
        stats["files"] = len(jobs)
        for count, (media, merged_wanted, members) in enumerate(jobs, 1):
            try:
                operation_status, actual = apply_one(Path(media), merged_wanted)
                stats["written" if operation_status == "Written & verified" else "matched"] += 1
                for idx in members:
                    diff = differences(self.records[idx].expected, actual, self.clear_blanks)
                    status = "Different" if diff else operation_status
                    detail = ("Verification mismatch: " + str(diff) if diff
                              else "Tags were written and read back successfully." if status == "Written & verified"
                              else "Already matches XSQ metadata; no write needed.")
                    self.result_ready.emit(idx, status, detail, actual)
            except Exception as exc:
                stats["failed"] += 1
                for idx in members:
                    self.result_ready.emit(idx, "Media error", f"Write or verification failed: {exc}", {})
            self.advance.emit(count, len(jobs), Path(media).name)
        self.outcome = stats


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("xLights Media Metadata Manager")
        self.resize(1460, 760)
        self.records: list[Record] = []
        self.worker: QThread | None = None
        self.last_action = ""
        self.build_ui()

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(9)

        title = QLabel("xLights Media Metadata Manager")
        title_font = QFont()
        title_font.setPointSize(15)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)
        note = QLabel("XSQ files are READ-ONLY. Only MP3/M4A audio tags can be modified, after confirmation.")
        note.setStyleSheet("color: #286c46; font-weight: 600;")
        layout.addWidget(note)

        directory_group = QGroupBox("Scan xLights sequences")
        directory_layout = QVBoxLayout(directory_group)
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Directory:"))
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Choose a directory containing .xsq sequences")
        path_row.addWidget(self.path_edit, 1)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self.browse)
        path_row.addWidget(self.browse_button)
        directory_layout.addLayout(path_row)
        option_row = QHBoxLayout()
        self.recursive_check = QCheckBox("Scan subdirectories recursively")
        self.recursive_check.setChecked(True)
        option_row.addWidget(self.recursive_check)
        self.blank_check = QCheckBox("Write blank XSQ fields (clear existing audio tags)")
        self.blank_check.setToolTip("When disabled, empty XSQ fields do not change or count against matching audio tags.")
        self.blank_check.toggled.connect(self.refresh_comparisons)
        option_row.addWidget(self.blank_check)
        option_row.addStretch()
        self.scan_button = QPushButton("Scan")
        self.scan_button.clicked.connect(self.scan)
        option_row.addWidget(self.scan_button)
        directory_layout.addLayout(option_row)
        layout.addWidget(directory_group)

        controls = QHBoxLayout()
        self.count_label = QLabel("No sequences scanned")
        controls.addWidget(self.count_label)
        controls.addStretch()
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(lambda: self.set_checks(True))
        controls.addWidget(self.select_all_button)
        self.select_none_button = QPushButton("Select none")
        self.select_none_button.clicked.connect(lambda: self.set_checks(False))
        controls.addWidget(self.select_none_button)
        self.select_diff_button = QPushButton("Select differences")
        self.select_diff_button.clicked.connect(self.select_differences)
        controls.addWidget(self.select_diff_button)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "Use", "XSQ sequence filename", "Media filename", "Song / Title", "Artist", "Album", "Comment", "Status"
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, width in enumerate((52, 240, 280, 190, 170, 170, 200, 155)):
            self.table.setColumnWidth(col, width)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self.on_item_changed)
        self.table.cellDoubleClicked.connect(self.details_for_row)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.selection_label = QLabel("0 selected")
        actions.addWidget(self.selection_label)
        actions.addStretch()
        self.verify_button = QPushButton("Confirm Metadata (read only)")
        self.verify_button.clicked.connect(lambda: self.start_operation("verify"))
        actions.addWidget(self.verify_button)
        self.apply_button = QPushButton("Apply Metadata")
        self.apply_button.setStyleSheet("font-weight: 600; padding: 7px 14px;")
        self.apply_button.clicked.connect(lambda: self.start_operation("apply"))
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        layout.addWidget(self.progress)
        self.info_label = QLabel("Double-click any row to view its full XSQ path, absolute media path and tag comparison.")
        layout.addWidget(self.info_label)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        self.log.setPlaceholderText("Scan and operation log")
        layout.addWidget(self.log)
        self.update_controls()

    def browse(self):
        chosen = QFileDialog.getExistingDirectory(self, "Select xLights sequence directory", self.path_edit.text() or str(Path.home()))
        if chosen:
            self.path_edit.setText(chosen)

    def busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def update_controls(self):
        idle = not self.busy()
        has_rows = bool(self.records)
        for widget in (self.browse_button, self.scan_button, self.path_edit, self.recursive_check,
                       self.blank_check, self.select_all_button, self.select_none_button,
                       self.select_diff_button):
            widget.setEnabled(idle)
        self.verify_button.setEnabled(idle and has_rows)
        self.apply_button.setEnabled(idle and has_rows)
        self.table.setEnabled(idle)
        self.update_selection_label()

    def scan(self):
        directory = Path(self.path_edit.text().strip()).expanduser()
        if not directory.is_dir():
            QMessageBox.warning(self, "Invalid directory", "Choose an existing directory to scan.")
            return
        self.records.clear()
        with QSignalBlocker(self.table):
            self.table.setSortingEnabled(False)
            self.table.setRowCount(0)
        self.progress.setValue(0)
        self.progress.setFormat("Scanning…")
        self.info_label.setText("Scanning .xsq files (Media sequences only)…")
        self.log.clear()
        self.last_action = "scan"
        thread = ScanThread(directory, self.recursive_check.isChecked(), self.blank_check.isChecked())
        self.worker = thread
        thread.item_ready.connect(self.add_record)
        thread.advance.connect(self.set_progress)
        thread.finished.connect(self.worker_finished)
        thread.start()
        self.update_controls()

    def add_record(self, record: Record):
        idx = len(self.records)
        self.records.append(record)
        row = self.table.rowCount()
        # Populating checkable cells emits itemChanged before the sequence
        # index cell exists. Suppress signals until the row is complete.
        blocker = QSignalBlocker(self.table)
        self.table.insertRow(row)
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
        check.setCheckState(Qt.CheckState.Checked)
        self.table.setItem(row, COL_USE, check)
        values = (Path(record.sequence).name, Path(record.media).name if record.media else "",
                  record.expected["song"], record.expected["artist"], record.expected["album"],
                  record.expected["comment"], record.status)
        for col, val in enumerate(values, start=1):
            item = QTableWidgetItem(val)
            if col == COL_SEQUENCE:
                item.setData(Qt.ItemDataRole.UserRole, idx)
                item.setToolTip(record.sequence)
            elif col == COL_MEDIA:
                item.setToolTip(record.media)
            self.table.setItem(row, col, item)
        self.paint_status(row)
        del blocker
        self.update_selection_label()

    def paint_status(self, row: int):
        item = self.table.item(row, COL_STATUS)
        if item is None:
            return
        status = item.text()
        if status in ("Match", "Written & verified"):
            item.setForeground(QColor("#16804a"))
        elif status in ("Different",):
            item.setForeground(QColor("#a15e00"))
        elif status in ("Conflict", "Media error", "XSQ error", "Missing media", "Invalid media path", "Unsupported media"):
            item.setForeground(QColor("#bc3333"))
        else:
            item.setForeground(QColor("#555555"))

    def record_index_at(self, row: int) -> int | None:
        item = self.table.item(row, COL_SEQUENCE)
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else None

    def row_for_index(self, idx: int) -> int | None:
        for row in range(self.table.rowCount()):
            if self.record_index_at(row) == idx:
                return row
        return None

    def on_item_changed(self, item: QTableWidgetItem):
        if item.column() == COL_USE:
            self.update_selection_label()

    def selected_indices(self) -> list[int]:
        result = []
        for row in range(self.table.rowCount()):
            checkbox = self.table.item(row, COL_USE)
            idx = self.record_index_at(row)
            if checkbox is not None and idx is not None and checkbox.checkState() == Qt.CheckState.Checked:
                result.append(idx)
        return result

    def update_selection_label(self):
        selected = len(self.selected_indices())
        self.selection_label.setText(f"{selected} selected")
        self.count_label.setText(f"{len(self.records)} Media sequences found")

    def set_checks(self, checked: bool):
        with QSignalBlocker(self.table):
            for row in range(self.table.rowCount()):
                item = self.table.item(row, COL_USE)
                if item is not None:
                    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.update_selection_label()

    def select_differences(self):
        with QSignalBlocker(self.table):
            for row in range(self.table.rowCount()):
                idx = self.record_index_at(row)
                item = self.table.item(row, COL_USE)
                if idx is not None and item is not None:
                    checked = self.records[idx].status == "Different"
                    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.update_selection_label()

    def refresh_comparisons(self):
        if self.busy():
            return
        for idx, rec in enumerate(self.records):
            if not rec.readable:
                continue
            status, detail = status_from_values(rec, self.blank_check.isChecked())
            rec.status, rec.detail = status, detail
            self.set_row_status(idx, status, detail, rec.actual)

    def set_progress(self, count: int, total: int, name: str):
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(count)
        self.progress.setFormat(f"{count} of {total} files")
        self.info_label.setText(f"Processing: {name}")

    def worker_finished(self):
        """Release a QThread only after Qt confirms it has actually finished.

        The previous implementation cleared the last Python reference on a
        custom `completed` signal emitted *inside* run(). A zero-write batch
        can return immediately, letting Qt/Python destroy a running QThread.
        Calling wait() here (in the GUI thread after finished) closes that
        race, including when the result dialog starts a nested event loop.
        """
        thread = self.worker
        if thread is None:
            return
        thread.wait()  # finished was signalled; thread is now fully stopped
        outcome = thread.outcome
        self.worker = None
        thread.deleteLater()
        if isinstance(thread, ScanThread):
            self.scan_done(*outcome)
        else:
            if outcome is None:
                self.log.appendPlainText("Operation ended without a result.")
                self.update_controls()
                QMessageBox.critical(self, "Operation error", "Operation ended without a result.")
            else:
                self.operation_done(outcome)

    def scan_done(self, scanned: int, accepted: int, error: str):
        self.worker = None
        self.table.setSortingEnabled(True)
        if error:
            self.log.appendPlainText(error)
            QMessageBox.critical(self, "Scan error", error)
        else:
            self.log.appendPlainText(f"Found {scanned} XSQ files; displayed {accepted} Media sequences (excluding Animation and other types).")
            self.info_label.setText(f"Scan complete. {accepted} Media sequences found in {scanned} XSQ files. Double-click a row for details.")
            self.progress.setFormat(f"Scan complete: {accepted} Media sequences")
        self.update_controls()

    def start_operation(self, action: str):
        indices = self.selected_indices()
        if not indices:
            QMessageBox.information(self, "Nothing selected", "Select at least one sequence first.")
            return
        if action == "apply":
            jobs, failures = prepare_jobs(self.records, indices, self.blank_check.isChecked())
            message = (f"Apply metadata to {len(jobs)} distinct audio file(s) referenced by "
                       f"{len(indices)} selected sequence(s)?\n\n"
                       f"{len(failures)} selected sequence(s) have missing, invalid or conflicting media metadata and will not be written.\n\n"
                       "Only MP3/M4A tags will be modified. XSQ files will NEVER be modified.")
            if self.blank_check.isChecked():
                message += "\n\nWARNING: Blank XSQ fields WILL REMOVE corresponding audio tags."
            if QMessageBox.question(self, "Confirm audio metadata changes", message,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            if not jobs:
                QMessageBox.warning(self, "Nothing to write", "No writable audio files were found among the selected rows.")
                return
        self.last_action = action
        self.progress.setValue(0)
        self.progress.setFormat("Starting…")
        thread = OperationThread(action, self.records, indices, self.blank_check.isChecked())
        self.worker = thread
        thread.result_ready.connect(self.set_row_status)
        thread.advance.connect(self.set_progress)
        thread.finished.connect(self.worker_finished)
        thread.start()
        self.update_controls()

    def set_row_status(self, idx: int, status: str, detail: str, actual: dict):
        record = self.records[idx]
        record.status = status
        record.detail = detail
        if actual:
            record.actual = dict(actual)
            record.readable = True
        row = self.row_for_index(idx)
        if row is not None:
            item = self.table.item(row, COL_STATUS)
            item.setText(status)
            item.setToolTip(detail)
            self.paint_status(row)
        if status in {"Media error", "Conflict", "Different"}:
            self.log.appendPlainText(f"[{status}] {Path(record.sequence).name}: {detail}")

    def operation_done(self, stats: dict):
        self.worker = None
        if self.last_action == "apply":
            message = (f"Processed {stats['files']} distinct audio files: "
                       f"{stats['written']} written and verified, {stats['matched']} already matched, "
                       f"{stats['failed']} errors/conflicts.")
        else:
            message = (f"Checked {stats['files']} sequence references: "
                       f"{stats['matched']} matched, {stats['different']} differed, "
                       f"{stats['failed']} errors.")
        self.log.appendPlainText(message)
        self.info_label.setText(message)
        self.progress.setFormat(message)
        self.update_controls()
        QMessageBox.information(self, "Operation complete", message)

    def details_for_row(self, row: int, column: int):
        rec = self.records[self.record_index_at(row)]
        dialog = QDialog(self)
        dialog.setWindowTitle("Sequence and audio metadata comparison")
        dialog.resize(800, 470)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        lines = [f"XSQ file: {rec.sequence}", f"Media file: {rec.media or '(none)'}",
                 f"Status: {rec.status}", f"Details: {rec.detail}", "", "Metadata comparison:",
                 f"{'Field':<12} {'XSQ':<35} Audio file", "-" * 90]
        for name in FIELDS:
            wanted = rec.expected.get(name, "")
            current = rec.actual.get(name, "")
            ignored = not self.blank_check.isChecked() and not wanted
            lines.append(f"{name.title():<12} {wanted[:35]:<35} {current}" + ("  [blank XSQ ignored]" if ignored else ""))
        text.setPlainText("\n".join(lines))
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def closeEvent(self, event):
        if self.busy():
            QMessageBox.warning(self, "Operation in progress", "Finish the current scan or operation before closing.")
            event.ignore()
            return
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
