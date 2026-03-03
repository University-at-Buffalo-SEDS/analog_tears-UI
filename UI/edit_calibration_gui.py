#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from PyQt6 import QtCore, QtWidgets


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2))


def _get_float(text: str) -> float | None:
    s = text.strip()
    if not s:
        return None
    return float(s)


class CalibrationEditor(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Load Cell Calibration Editor")
        self.resize(520, 240)

        self._data: dict[str, Any] | None = None
        self._path: Path | None = None

        layout = QtWidgets.QVBoxLayout(self)

        file_row = QtWidgets.QHBoxLayout()
        layout.addLayout(file_row)
        file_row.addWidget(QtWidgets.QLabel("Calibration file:"))
        self.file_edit = QtWidgets.QLineEdit("loadcell_calibration.json")
        file_row.addWidget(self.file_edit, stretch=1)
        self.browse_btn = QtWidgets.QPushButton("Browse")
        self.browse_btn.clicked.connect(self._browse)
        file_row.addWidget(self.browse_btn)
        self.load_btn = QtWidgets.QPushButton("Load")
        self.load_btn.clicked.connect(self._load)
        file_row.addWidget(self.load_btn)

        grid = QtWidgets.QGridLayout()
        layout.addLayout(grid)

        grid.addWidget(QtWidgets.QLabel("Channel"), 0, 0)
        grid.addWidget(QtWidgets.QLabel("Slope (m)"), 0, 1)
        grid.addWidget(QtWidgets.QLabel("Intercept (b)"), 0, 2)

        grid.addWidget(QtWidgets.QLabel("Ch0"), 1, 0)
        self.ch0_m = QtWidgets.QLineEdit()
        self.ch0_b = QtWidgets.QLineEdit()
        grid.addWidget(self.ch0_m, 1, 1)
        grid.addWidget(self.ch0_b, 1, 2)

        grid.addWidget(QtWidgets.QLabel("Ch1"), 2, 0)
        self.ch1_m = QtWidgets.QLineEdit()
        self.ch1_b = QtWidgets.QLineEdit()
        grid.addWidget(self.ch1_m, 2, 1)
        grid.addWidget(self.ch1_b, 2, 2)

        zero_row = QtWidgets.QHBoxLayout()
        layout.addLayout(zero_row)
        zero_row.addWidget(QtWidgets.QLabel("Zero raw Ch0:"))
        self.ch0_zero = QtWidgets.QLineEdit()
        self.ch0_zero.setFixedWidth(160)
        zero_row.addWidget(self.ch0_zero)
        zero_row.addSpacing(20)
        zero_row.addWidget(QtWidgets.QLabel("Zero raw Ch1:"))
        self.ch1_zero = QtWidgets.QLineEdit()
        self.ch1_zero.setFixedWidth(160)
        zero_row.addWidget(self.ch1_zero)
        zero_row.addStretch(1)

        info_row = QtWidgets.QHBoxLayout()
        layout.addLayout(info_row)
        self.weights_lbl = QtWidgets.QLabel("Weights: —")
        info_row.addWidget(self.weights_lbl)
        info_row.addStretch(1)

        points_row = QtWidgets.QHBoxLayout()
        layout.addLayout(points_row)
        self.points_ch0_lbl = QtWidgets.QLabel("Ch0 points: —")
        self.points_ch1_lbl = QtWidgets.QLabel("Ch1 points: —")
        points_row.addWidget(self.points_ch0_lbl)
        points_row.addWidget(self.points_ch1_lbl)
        points_row.addStretch(1)

        fit_row = QtWidgets.QHBoxLayout()
        layout.addLayout(fit_row)
        self.ch0_fit_lbl = QtWidgets.QLabel("Ch0 fit: —")
        fit_row.addWidget(self.ch0_fit_lbl)
        self.ch1_fit_lbl = QtWidgets.QLabel("Ch1 fit: —")
        fit_row.addWidget(self.ch1_fit_lbl)
        fit_row.addStretch(1)

        lists_row = QtWidgets.QHBoxLayout()
        layout.addLayout(lists_row)
        self.points_ch0_list = QtWidgets.QListWidget()
        self.points_ch1_list = QtWidgets.QListWidget()
        self.points_ch0_list.setFixedHeight(110)
        self.points_ch1_list.setFixedHeight(110)
        self.points_ch0_list.setMinimumWidth(240)
        self.points_ch1_list.setMinimumWidth(240)
        lists_row.addWidget(self.points_ch0_list)
        lists_row.addWidget(self.points_ch1_list)

        opts_row = QtWidgets.QHBoxLayout()
        layout.addLayout(opts_row)
        self.backup_chk = QtWidgets.QCheckBox("Write .bak backup")
        self.backup_chk.setChecked(True)
        opts_row.addWidget(self.backup_chk)
        opts_row.addStretch(1)

        action_row = QtWidgets.QHBoxLayout()
        layout.addLayout(action_row)
        self.save_btn = QtWidgets.QPushButton("Save")
        self.save_btn.clicked.connect(self._save)
        action_row.addWidget(self.save_btn)
        action_row.addStretch(1)
        self.status_lbl = QtWidgets.QLabel("Status: —")
        action_row.addWidget(self.status_lbl)

        # Auto-load default file on startup (best-effort)
        QtCore.QTimer.singleShot(0, self._load)

    def _browse(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select calibration file", "", "JSON Files (*.json);;All Files (*)"
        )
        if path:
            self.file_edit.setText(path)

    def _load(self) -> None:
        path = Path(self.file_edit.text().strip())
        if not path.exists():
            self.status_lbl.setText(f"Status: file not found: {path}")
            return
        try:
            data = _read_json(path)
        except Exception as e:
            self.status_lbl.setText(f"Status: load failed ({e})")
            return

        self._data = data
        self._path = path

        ch0 = data.get("ch0", {})
        ch1 = data.get("ch1", {})

        self.ch0_m.setText("" if "m" not in ch0 or ch0["m"] is None else str(ch0["m"]))
        self.ch0_b.setText("" if "b" not in ch0 or ch0["b"] is None else str(ch0["b"]))
        self.ch1_m.setText("" if "m" not in ch1 or ch1["m"] is None else str(ch1["m"]))
        self.ch1_b.setText("" if "b" not in ch1 or ch1["b"] is None else str(ch1["b"]))

        self.ch0_zero.setText("" if data.get("ch0_zero_raw") is None else str(data.get("ch0_zero_raw")))
        self.ch1_zero.setText("" if data.get("ch1_zero_raw") is None else str(data.get("ch1_zero_raw")))

        weights = data.get("weights_kg", [])
        if isinstance(weights, list) and weights:
            self.weights_lbl.setText(f"Weights: {weights}")
        else:
            self.weights_lbl.setText("Weights: —")

        pts0 = data.get("points", [])
        pts1 = data.get("points_ch1", [])
        self.points_ch0_lbl.setText(f"Ch0 points: {len(pts0) if isinstance(pts0, list) else 0}")
        self.points_ch1_lbl.setText(f"Ch1 points: {len(pts1) if isinstance(pts1, list) else 0}")

        ch1_fit = data.get("ch1_fit", {})
        ch0_fit = data.get("ch0_fit", {})
        if isinstance(ch0_fit, dict) and ch0_fit.get("type") == "poly2":
            a = ch0_fit.get("a")
            b = ch0_fit.get("b")
            c = ch0_fit.get("c")
            self.ch0_fit_lbl.setText(f"Ch0 fit: poly2 (a={a}, b={b}, c={c})")
        else:
            self.ch0_fit_lbl.setText("Ch0 fit: —")
        if isinstance(ch1_fit, dict) and ch1_fit.get("type") == "poly2":
            a = ch1_fit.get("a")
            b = ch1_fit.get("b")
            c = ch1_fit.get("c")
            self.ch1_fit_lbl.setText(f"Ch1 fit: poly2 (a={a}, b={b}, c={c})")
        else:
            self.ch1_fit_lbl.setText("Ch1 fit: —")

        self.points_ch0_list.clear()
        self.points_ch1_list.clear()
        if isinstance(pts0, list) and pts0:
            for p in pts0:
                try:
                    self.points_ch0_list.addItem(f"{p.get('kg')} kg → {p.get('ch0_raw')}")
                except Exception:
                    self.points_ch0_list.addItem(str(p))
        else:
            self.points_ch0_list.addItem("(no points)")

        if isinstance(pts1, list) and pts1:
            for p in pts1:
                try:
                    self.points_ch1_list.addItem(f"{p.get('kg')} kg → {p.get('ch1_raw')}")
                except Exception:
                    self.points_ch1_list.addItem(str(p))
        else:
            self.points_ch1_list.addItem("(no points)")

        self.status_lbl.setText(f"Status: loaded {path}")

    def _save(self) -> None:
        if self._data is None or self._path is None:
            self.status_lbl.setText("Status: load a file first")
            return

        try:
            ch0_m = _get_float(self.ch0_m.text())
            ch0_b = _get_float(self.ch0_b.text())
            ch1_m = _get_float(self.ch1_m.text())
            ch1_b = _get_float(self.ch1_b.text())
            ch0_zero = _get_float(self.ch0_zero.text())
            ch1_zero = _get_float(self.ch1_zero.text())
        except Exception as e:
            self.status_lbl.setText(f"Status: invalid number ({e})")
            return

        self._data.setdefault("ch0", {})
        self._data.setdefault("ch1", {})
        self._data["ch0"]["m"] = ch0_m
        self._data["ch0"]["b"] = ch0_b
        self._data["ch1"]["m"] = ch1_m
        self._data["ch1"]["b"] = ch1_b
        self._data["ch0_zero_raw"] = ch0_zero
        self._data["ch1_zero_raw"] = ch1_zero

        try:
            if self.backup_chk.isChecked():
                backup_path = self._path.with_suffix(self._path.suffix + ".bak")
                _write_json(backup_path, self._data)
            _write_json(self._path, self._data)
        except Exception as e:
            self.status_lbl.setText(f"Status: save failed ({e})")
            return

        self.status_lbl.setText(f"Status: saved {self._path}")


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    win = CalibrationEditor()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
