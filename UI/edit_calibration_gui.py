#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import socket
import sys
from collections import deque
from pathlib import Path
from typing import Any

import pyqtgraph as pg
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


class _PointValueDialog(QtWidgets.QDialog):
    def __init__(self, parent: QtWidgets.QWidget, *, title: str, weight: float = 0.0, raw: float = 0.0, show_raw: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(340, 130 if show_raw else 110)

        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        layout.addLayout(form)

        self.weight_spin = QtWidgets.QDoubleSpinBox()
        self.weight_spin.setRange(0.0, 10000.0)
        self.weight_spin.setDecimals(3)
        self.weight_spin.setSingleStep(0.1)
        self.weight_spin.setValue(float(weight))
        form.addRow("Expected kg:", self.weight_spin)

        self.raw_spin: QtWidgets.QDoubleSpinBox | None = None
        if show_raw:
            raw_spin = QtWidgets.QDoubleSpinBox()
            raw_spin.setRange(-1e12, 1e12)
            raw_spin.setDecimals(6)
            raw_spin.setSingleStep(0.1)
            raw_spin.setValue(float(raw))
            form.addRow("Raw value:", raw_spin)
            self.raw_spin = raw_spin

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[float, float | None]:
        raw_v = float(self.raw_spin.value()) if self.raw_spin is not None else None
        return float(self.weight_spin.value()), raw_v


class _PointsSequenceDialog(QtWidgets.QDialog):
    def __init__(self, editor: "CalibrationEditor") -> None:
        super().__init__(editor)
        self._editor = editor
        self.setWindowTitle("Calibration Points / Sequence")
        self.setModal(True)
        self.resize(620, 420)

        layout = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        layout.addLayout(top)
        top.addWidget(QtWidgets.QLabel("Channel:"))
        self.channel_combo = QtWidgets.QComboBox()
        self.channel_combo.addItems(["Ch0", "Ch1"])
        self.channel_combo.currentIndexChanged.connect(self._reload_list)
        top.addWidget(self.channel_combo)
        self.sequence_lbl = QtWidgets.QLabel("Sequence: —")
        top.addWidget(self.sequence_lbl)
        top.addStretch(1)

        self.points_list = QtWidgets.QListWidget()
        layout.addWidget(self.points_list, stretch=1)

        btns = QtWidgets.QHBoxLayout()
        layout.addLayout(btns)
        self.add_btn = QtWidgets.QPushButton("Add point")
        self.add_btn.clicked.connect(self._on_add)
        btns.addWidget(self.add_btn)
        self.edit_btn = QtWidgets.QPushButton("Edit selected")
        self.edit_btn.clicked.connect(self._on_edit)
        btns.addWidget(self.edit_btn)
        self.remove_btn = QtWidgets.QPushButton("Remove selected")
        self.remove_btn.clicked.connect(self._on_remove)
        btns.addWidget(self.remove_btn)
        self.reset_btn = QtWidgets.QPushButton("Reset channel")
        self.reset_btn.clicked.connect(self._on_reset)
        btns.addWidget(self.reset_btn)

        seq_btns = QtWidgets.QHBoxLayout()
        layout.addLayout(seq_btns)
        self.continue_btn = QtWidgets.QPushButton("Continue sequence")
        self.continue_btn.clicked.connect(self._on_continue_sequence)
        seq_btns.addWidget(self.continue_btn)
        self.new_btn = QtWidgets.QPushButton("Start new sequence")
        self.new_btn.clicked.connect(self._on_new_sequence)
        seq_btns.addWidget(self.new_btn)
        seq_btns.addStretch(1)

        close_row = QtWidgets.QHBoxLayout()
        layout.addLayout(close_row)
        close_row.addStretch(1)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)

        self.status_lbl = QtWidgets.QLabel("Status: —")
        layout.addWidget(self.status_lbl)

        self._reload_list()

    def _channel(self) -> int:
        return int(self.channel_combo.currentIndex())

    def _reload_list(self) -> None:
        ch = self._channel()
        pts = self._editor._points0_xy if ch == 0 else self._editor._points1_xy
        self.points_list.clear()
        if pts:
            for raw, kg in pts:
                self.points_list.addItem(f"{kg:g} kg -> {raw:.6g}")
        else:
            self.points_list.addItem("(no points)")
        started = self._editor._sequence_started.get(ch, False)
        self.sequence_lbl.setText(f"Sequence: Ch{ch} {'started' if started else 'not started'}")

    def _on_add(self) -> None:
        dlg = _PointValueDialog(self, title="Add calibration point", weight=0.0, raw=0.0, show_raw=True)
        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return
        kg, raw = dlg.values()
        if raw is None:
            return
        self._editor.add_manual_point(self._channel(), kg, raw)
        self.status_lbl.setText("Status: point added")
        self._reload_list()

    def _on_edit(self) -> None:
        ch = self._channel()
        row = self.points_list.currentRow()
        pts = self._editor._points0_xy if ch == 0 else self._editor._points1_xy
        if row < 0 or row >= len(pts):
            self.status_lbl.setText("Status: select a point first")
            return
        raw, kg = pts[row]
        dlg = _PointValueDialog(self, title="Edit calibration point", weight=kg, raw=raw, show_raw=True)
        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return
        kg_new, raw_new = dlg.values()
        if raw_new is None:
            return
        self._editor.update_manual_point(ch, row, kg_new, raw_new)
        self.status_lbl.setText("Status: point updated")
        self._reload_list()

    def _on_remove(self) -> None:
        ch = self._channel()
        row = self.points_list.currentRow()
        if not self._editor.remove_point(ch, row):
            self.status_lbl.setText("Status: select a point first")
            return
        self.status_lbl.setText("Status: point removed")
        self._reload_list()

    def _on_reset(self) -> None:
        ch = self._channel()
        if QtWidgets.QMessageBox.question(
            self,
            "Reset channel",
            f"Reset Ch{ch} points and sequence?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._editor.reset_channel_points(ch)
        self.status_lbl.setText(f"Status: Ch{ch} reset")
        self._reload_list()

    def _on_continue_sequence(self) -> None:
        dlg = _PointValueDialog(self, title="Continue sequence", weight=1.0, show_raw=False)
        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return
        kg, _ = dlg.values()
        if self._editor.start_sequence_capture(self._channel(), kg, start_new=False):
            self.status_lbl.setText(f"Status: capture started at {kg:g} kg")
        else:
            self.status_lbl.setText(self._editor.status_lbl.text())

    def _on_new_sequence(self) -> None:
        dlg = _PointValueDialog(self, title="Start new sequence", weight=0.0, show_raw=False)
        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return
        kg, _ = dlg.values()
        if self._editor.start_sequence_capture(self._channel(), kg, start_new=True):
            self.status_lbl.setText("Status: new sequence started")
        else:
            self.status_lbl.setText(self._editor.status_lbl.text())


class CalibrationEditor(QtWidgets.QWidget):
    def __init__(self, initial_file: str | None = None, raw_port: int | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Load Cell Calibration Editor")
        self.resize(1500, 980)

        self._data: dict[str, Any] | None = None
        self._path: Path | None = None
        self._points0_xy: list[tuple[float, float]] = []
        self._points1_xy: list[tuple[float, float]] = []
        self._ch0_fit_meta: dict[str, Any] | None = None
        self._ch1_fit_meta: dict[str, Any] | None = None
        self._raw_port = raw_port
        self._raw_sock: socket.socket | None = None
        self._raw_timer: QtCore.QTimer | None = None
        self._raw_recent: deque[tuple[float, float, float]] = deque(maxlen=512)
        self._live_raw_ch0 = 0.0
        self._live_raw_ch1 = 0.0
        self._capture_active = False
        self._capture_channel = 0
        self._capture_weight = 0.0
        self._capture_target = 200
        self._capture_vals: list[float] = []
        self._capture_mode = "manual"
        self._sequence_started: dict[int, bool] = {0: False, 1: False}

        layout = QtWidgets.QVBoxLayout(self)

        file_row = QtWidgets.QHBoxLayout()
        layout.addLayout(file_row)
        file_row.addWidget(QtWidgets.QLabel("Calibration file:"))
        self.file_edit = QtWidgets.QLineEdit(initial_file or "loadcell_calibration.json")
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

        ops_row = QtWidgets.QHBoxLayout()
        layout.addLayout(ops_row)
        self.points_modal_btn = QtWidgets.QPushButton("Edit points / sequence")
        self.points_modal_btn.clicked.connect(self._open_points_modal)
        ops_row.addWidget(self.points_modal_btn)
        self.refit_btn = QtWidgets.QPushButton("Refit from points")
        self.refit_btn.clicked.connect(self._refit_from_points)
        ops_row.addWidget(self.refit_btn)
        self.seq_state_lbl = QtWidgets.QLabel("Sequence: Ch0 not started, Ch1 not started")
        ops_row.addWidget(self.seq_state_lbl)
        ops_row.addStretch(1)

        plots_row = QtWidgets.QHBoxLayout()
        layout.addLayout(plots_row)
        self.plot_widget = pg.GraphicsLayoutWidget()
        plots_row.addWidget(self.plot_widget, stretch=1)

        self.p0 = self.plot_widget.addPlot(row=0, col=0, title="Ch0 Calibration")
        self.p1 = self.plot_widget.addPlot(row=0, col=1, title="Ch1 Calibration")
        for p in (self.p0, self.p1):
            p.showGrid(x=True, y=True)
            p.setLabel("bottom", "Raw")
            p.setLabel("left", "Expected (kg)")
        self.p0_points = self.p0.plot([], [], pen=None, symbol="o", symbolSize=7)
        self.p0_fit = self.p0.plot([], [], pen=pg.mkPen(width=2))
        self.p1_points = self.p1.plot([], [], pen=None, symbol="o", symbolSize=7)
        self.p1_fit = self.p1.plot([], [], pen=pg.mkPen(width=2))

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

        live_row = QtWidgets.QHBoxLayout()
        layout.addLayout(live_row)
        self.live_lbl = QtWidgets.QLabel("Live raw: —")
        live_row.addWidget(self.live_lbl)
        live_row.addStretch(1)

        for w in (self.ch0_m, self.ch0_b, self.ch1_m, self.ch1_b):
            w.textChanged.connect(self._update_regression_plots)

        if self._raw_port is not None:
            try:
                self._raw_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._raw_sock.bind(("127.0.0.1", int(self._raw_port)))
                self._raw_sock.setblocking(False)
                self._raw_timer = QtCore.QTimer(self)
                self._raw_timer.setInterval(10)
                self._raw_timer.timeout.connect(self._poll_raw_stream)
                self._raw_timer.start()
                self.live_lbl.setText(f"Live raw: listening on 127.0.0.1:{int(self._raw_port)}")
            except Exception as e:
                self.live_lbl.setText(f"Live raw: socket error ({e})")

        self._update_sequence_status_label()
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
        self._sequence_started[0] = data.get("ch0_zero_raw") is not None
        self._sequence_started[1] = data.get("ch1_zero_raw") is not None
        self._update_sequence_status_label()

        pts0 = data.get("points", [])
        pts1 = data.get("points_ch1", [])
        ch1_fit = data.get("ch1_fit", {})
        ch0_fit = data.get("ch0_fit", {})
        self._ch0_fit_meta = ch0_fit if isinstance(ch0_fit, dict) else None
        self._ch1_fit_meta = ch1_fit if isinstance(ch1_fit, dict) else None
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

        self._points0_xy = []
        self._points1_xy = []
        if isinstance(pts0, list) and pts0:
            for p in pts0:
                try:
                    raw = p.get("ch0_raw")
                    kg = p.get("kg")
                    if raw is not None and kg is not None:
                        self._points0_xy.append((float(raw), float(kg)))
                except Exception:
                    pass

        if isinstance(pts1, list) and pts1:
            for p in pts1:
                try:
                    raw = p.get("ch1_raw")
                    kg = p.get("kg")
                    if raw is not None and kg is not None:
                        self._points1_xy.append((float(raw), float(kg)))
                except Exception:
                    pass

        self._refresh_points_lists()
        self._update_regression_plots()
        self.status_lbl.setText(f"Status: loaded {path}")

    def _refresh_points_lists(self) -> None:
        self.points_ch0_lbl.setText(f"Ch0 points: {len(self._points0_xy)}")
        self.points_ch1_lbl.setText(f"Ch1 points: {len(self._points1_xy)}")
        all_w = sorted({kg for _, kg in self._points0_xy} | {kg for _, kg in self._points1_xy})
        self.weights_lbl.setText(f"Weights: {all_w}" if all_w else "Weights: —")
        self._update_sequence_status_label()
        self._update_regression_plots()

    def _update_sequence_status_label(self) -> None:
        ch0 = "started" if self._sequence_started.get(0, False) else "not started"
        ch1 = "started" if self._sequence_started.get(1, False) else "not started"
        self.seq_state_lbl.setText(f"Sequence: Ch0 {ch0}, Ch1 {ch1}")

    def _open_points_modal(self) -> None:
        dlg = _PointsSequenceDialog(self)
        dlg.exec()

    def _upsert_point(self, channel: int, kg: float, raw: float) -> str:
        pts = self._points0_xy if channel == 0 else self._points1_xy
        for i, (_raw_i, kg_i) in enumerate(pts):
            if abs(kg_i - kg) < 1e-9:
                pts[i] = (raw, kg)
                return "updated"
        pts.append((raw, kg))
        return "added"

    def _begin_capture(self, channel: int, weight: float, mode: str) -> None:
        if self._raw_sock is None:
            self.status_lbl.setText("Status: no live stream connected")
            return
        if self._capture_active:
            self.status_lbl.setText("Status: capture already in progress")
            return
        self._capture_channel = int(channel)
        self._capture_weight = float(weight)
        self._capture_mode = mode
        self._capture_vals.clear()
        self._capture_active = True
        self.status_lbl.setText(
            f"Status: capturing Ch{self._capture_channel} 0/{self._capture_target} at {self._capture_weight:g} kg..."
        )

    def add_manual_point(self, channel: int, kg: float, raw: float) -> None:
        pts = self._points0_xy if int(channel) == 0 else self._points1_xy
        pts.append((float(raw), float(kg)))
        self.status_lbl.setText(f"Status: added Ch{int(channel)} point ({kg:g} kg, {raw:.6g})")
        self._refresh_points_lists()

    def update_manual_point(self, channel: int, index: int, kg: float, raw: float) -> bool:
        pts = self._points0_xy if int(channel) == 0 else self._points1_xy
        if index < 0 or index >= len(pts):
            return False
        pts[index] = (float(raw), float(kg))
        self.status_lbl.setText(f"Status: updated Ch{int(channel)} point")
        self._refresh_points_lists()
        return True

    def remove_point(self, channel: int, index: int) -> bool:
        pts = self._points0_xy if int(channel) == 0 else self._points1_xy
        if index < 0 or index >= len(pts):
            return False
        del pts[index]
        self.status_lbl.setText(f"Status: removed Ch{int(channel)} point")
        self._refresh_points_lists()
        return True

    def reset_channel_points(self, channel: int) -> None:
        ch = int(channel)
        if ch == 0:
            self._points0_xy.clear()
            self.ch0_zero.setText("")
        else:
            self._points1_xy.clear()
            self.ch1_zero.setText("")
        self._sequence_started[ch] = False
        self.status_lbl.setText(f"Status: reset Ch{ch} points and sequence")
        self._refresh_points_lists()

    def start_sequence_capture(self, channel: int, kg: float, *, start_new: bool) -> bool:
        ch = int(channel)
        if start_new:
            if abs(float(kg)) > 1e-9:
                self.status_lbl.setText("Status: new sequence must start at 0 kg")
                return False
            self.reset_channel_points(ch)
            self._begin_capture(ch, 0.0, "sequence_zero")
            return True
        if not self._sequence_started.get(ch, False):
            self.status_lbl.setText(f"Status: start Ch{ch} sequence first (0 kg)")
            return False
        if float(kg) <= 0.0:
            self.status_lbl.setText("Status: expected kg must be > 0 for sequence points")
            return False
        self._begin_capture(ch, float(kg), "sequence_point")
        return True

    def _poll_raw_stream(self) -> None:
        if self._raw_sock is None:
            return
        received = False
        while True:
            try:
                payload, _addr = self._raw_sock.recvfrom(256)
            except BlockingIOError:
                break
            except Exception:
                break
            try:
                t_s, c0_s, c1_s = payload.decode("ascii", errors="ignore").strip().split(",")
                t_mono = float(t_s)
                ch0 = float(c0_s)
                ch1 = float(c1_s)
            except Exception:
                continue
            received = True
            self._raw_recent.append((t_mono, ch0, ch1))
            self._live_raw_ch0 = ch0
            self._live_raw_ch1 = ch1
            if self._capture_active:
                v = ch0 if self._capture_channel == 0 else ch1
                self._capture_vals.append(v)
                n = len(self._capture_vals)
                self.status_lbl.setText(
                    f"Status: capturing Ch{self._capture_channel} {n}/{self._capture_target} at {self._capture_weight:g} kg..."
                )
                if n >= self._capture_target:
                    avg = sum(self._capture_vals) / float(n)
                    action = self._upsert_point(self._capture_channel, self._capture_weight, avg)
                    if self._capture_mode == "sequence_zero":
                        if self._capture_channel == 0:
                            self.ch0_zero.setText(str(avg))
                        else:
                            self.ch1_zero.setText(str(avg))
                        self._sequence_started[self._capture_channel] = True
                    self._capture_active = False
                    self._capture_vals.clear()
                    self._refresh_points_lists()
                    self.status_lbl.setText(
                        f"Status: captured avg raw={avg:.6g} at {self._capture_weight:g} kg on Ch{self._capture_channel} ({action})"
                    )
        if received and not self._capture_active:
            self.live_lbl.setText(f"Live raw: Ch0={self._live_raw_ch0:.6g}  Ch1={self._live_raw_ch1:.6g}")

    def _refit_from_points(self) -> None:
        def fit_line(xs: list[float], ys: list[float]) -> tuple[float, float]:
            n = float(len(xs))
            sx = sum(xs)
            sy = sum(ys)
            sxx = sum(x * x for x in xs)
            sxy = sum(x * y for x, y in zip(xs, ys))
            denom = (n * sxx) - (sx * sx)
            if abs(denom) < 1e-18:
                raise ValueError("degenerate points")
            m = (n * sxy - sx * sy) / denom
            b = (sy - m * sx) / n
            return m, b

        def fit_line_through_zero(xs: list[float], ys: list[float]) -> float:
            denom = sum(x * x for x in xs)
            if abs(denom) < 1e-18:
                raise ValueError("degenerate points")
            return sum(x * y for x, y in zip(xs, ys)) / denom

        def fit_poly2(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
            n = float(len(xs))
            sx = sum(xs)
            sx2 = sum(x * x for x in xs)
            sx3 = sum(x * x * x for x in xs)
            sx4 = sum(x * x * x * x for x in xs)
            sy = sum(ys)
            sxy = sum(x * y for x, y in zip(xs, ys))
            sx2y = sum((x * x) * y for x, y in zip(xs, ys))
            a11, a12, a13 = sx4, sx3, sx2
            a21, a22, a23 = sx3, sx2, sx
            a31, a32, a33 = sx2, sx, n
            b1, b2, b3 = sx2y, sxy, sy
            if abs(a11) < 1e-18:
                raise ValueError("degenerate points")
            f21 = a21 / a11
            f31 = a31 / a11
            a22 -= f21 * a12
            a23 -= f21 * a13
            b2 -= f21 * b1
            a32 -= f31 * a12
            a33 -= f31 * a13
            b3 -= f31 * b1
            if abs(a22) < 1e-18:
                raise ValueError("degenerate points")
            f32 = a32 / a22
            a33 -= f32 * a23
            b3 -= f32 * b2
            if abs(a33) < 1e-18:
                raise ValueError("degenerate points")
            c = b3 / a33
            b = (b2 - a23 * c) / a22
            a = (b1 - a12 * b - a13 * c) / a11
            return a, b, c

        def fit_poly2_through_zero(xs: list[float], ys: list[float]) -> tuple[float, float]:
            sx2 = sum(x * x for x in xs)
            sx3 = sum(x * x * x for x in xs)
            sx4 = sum(x * x * x * x for x in xs)
            sxy = sum(x * y for x, y in zip(xs, ys))
            sx2y = sum((x * x) * y for x, y in zip(xs, ys))
            det = sx4 * sx2 - sx3 * sx3
            if abs(det) < 1e-18:
                raise ValueError("degenerate points")
            a = (sx2y * sx2 - sxy * sx3) / det
            b = (sx4 * sxy - sx3 * sx2y) / det
            return a, b

        def sse_line(xs: list[float], ys: list[float], m: float, b: float) -> float:
            return sum((y - (m * x + b)) ** 2 for x, y in zip(xs, ys))

        def sse_poly2(xs: list[float], ys: list[float], a: float, b: float, c: float) -> float:
            return sum((y - (a * x * x + b * x + c)) ** 2 for x, y in zip(xs, ys))

        def aic(sse: float, n: int, k: int) -> float:
            if n <= 0:
                return float("inf")
            s = max(sse, 1e-18)
            return n * math.log(s / n) + (2 * k)

        def fit_one(points_xy: list[tuple[float, float]]) -> tuple[float, float, float | None, dict[str, Any]]:
            if len(points_xy) < 3:
                raise ValueError("need at least 3 points")
            xs = [x for x, _ in points_xy]
            ys = [y for _, y in points_xy]
            zero_candidates = [x for x, y in points_xy if abs(y) < 1e-9]
            if zero_candidates:
                x0 = float(zero_candidates[0])
                xs_shift = [x - x0 for x in xs]
                m0 = fit_line_through_zero(xs_shift, ys)
                sse_l = sse_line(xs_shift, ys, m0, 0.0)
                aic_l = aic(sse_l, len(xs_shift), 1)
                a2, b2 = fit_poly2_through_zero(xs_shift, ys)
                sse_q = sse_poly2(xs_shift, ys, a2, b2, 0.0)
                aic_q = aic(sse_q, len(xs_shift), 2)
                m = m0
                b = -m0 * x0
                if aic_q < aic_l:
                    return m, b, x0, {"type": "poly2", "a": a2, "b": b2, "c": 0.0, "x0": x0}
                return m, b, x0, {"type": "linear", "x0": x0}
            m, b = fit_line(xs, ys)
            sse_l = sse_line(xs, ys, m, b)
            aic_l = aic(sse_l, len(xs), 2)
            a2, b2, c2 = fit_poly2(xs, ys)
            sse_q = sse_poly2(xs, ys, a2, b2, c2)
            aic_q = aic(sse_q, len(xs), 3)
            if aic_q < aic_l:
                return m, b, None, {"type": "poly2", "a": a2, "b": b2, "c": c2, "x0": None}
            return m, b, None, {"type": "linear", "x0": None}

        try:
            if len(self._points0_xy) >= 3:
                m0, b0, x0, fit0 = fit_one(self._points0_xy)
                self.ch0_m.setText(str(m0))
                self.ch0_b.setText(str(b0))
                self.ch0_zero.setText("" if x0 is None else str(x0))
                self._ch0_fit_meta = fit0
                self.ch0_fit_lbl.setText(f"Ch0 fit: {fit0.get('type', 'linear')}")
            if len(self._points1_xy) >= 3:
                m1, b1, x1, fit1 = fit_one(self._points1_xy)
                self.ch1_m.setText(str(m1))
                self.ch1_b.setText(str(b1))
                self.ch1_zero.setText("" if x1 is None else str(x1))
                self._ch1_fit_meta = fit1
                self.ch1_fit_lbl.setText(f"Ch1 fit: {fit1.get('type', 'linear')}")
        except Exception as e:
            self.status_lbl.setText(f"Status: refit failed ({e})")
            return

        self._update_regression_plots()
        self.status_lbl.setText("Status: regression updated from points")

    def _update_regression_plots(self) -> None:
        def set_channel(points_xy: list[tuple[float, float]], m_edit: QtWidgets.QLineEdit, b_edit: QtWidgets.QLineEdit, pts_curve, fit_curve) -> None:
            if points_xy:
                xs = [x for x, _ in points_xy]
                ys = [y for _, y in points_xy]
                pts_curve.setData(xs, ys)
            else:
                xs = []
                pts_curve.setData([], [])

            try:
                m = _get_float(m_edit.text())
                b = _get_float(b_edit.text())
            except Exception:
                fit_curve.setData([], [])
                return

            if m is None or b is None:
                fit_curve.setData([], [])
                return

            if not xs:
                fit_curve.setData([], [])
                return

            x_min = min(xs)
            x_max = max(xs)
            if x_max <= x_min:
                x_fit = [x_min - 1.0, x_min + 1.0]
            else:
                span = x_max - x_min
                x_fit = [x_min - 0.05 * span, x_max + 0.05 * span]
            y_fit = [(m * x) + b for x in x_fit]
            fit_curve.setData(x_fit, y_fit)

        set_channel(self._points0_xy, self.ch0_m, self.ch0_b, self.p0_points, self.p0_fit)
        set_channel(self._points1_xy, self.ch1_m, self.ch1_b, self.p1_points, self.p1_fit)

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
        self._data["points"] = [{"kg": kg, "ch0_raw": raw} for raw, kg in self._points0_xy]
        self._data["points_ch1"] = [{"kg": kg, "ch1_raw": raw} for raw, kg in self._points1_xy]
        self._data["weights_kg"] = sorted({kg for _, kg in self._points0_xy} | {kg for _, kg in self._points1_xy})
        if isinstance(self._ch0_fit_meta, dict):
            self._data["ch0_fit"] = self._ch0_fit_meta
        if isinstance(self._ch1_fit_meta, dict):
            self._data["ch1_fit"] = self._ch1_fit_meta

        try:
            if self.backup_chk.isChecked():
                backup_path = self._path.with_suffix(self._path.suffix + ".bak")
                _write_json(backup_path, self._data)
            _write_json(self._path, self._data)
        except Exception as e:
            self.status_lbl.setText(f"Status: save failed ({e})")
            return

        self.status_lbl.setText(f"Status: saved {self._path}")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            if self._raw_timer is not None:
                self._raw_timer.stop()
        except Exception:
            pass
        try:
            if self._raw_sock is not None:
                self._raw_sock.close()
        except Exception:
            pass
        super().closeEvent(event)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("calibration_file", nargs="?", default=None)
    parser.add_argument("--raw-port", type=int, default=None)
    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    win = CalibrationEditor(initial_file=args.calibration_file, raw_port=args.raw_port)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
