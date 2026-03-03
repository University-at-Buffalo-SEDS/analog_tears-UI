# gui.py
from __future__ import annotations

import json
import hashlib
import hmac
import math
import socket
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable, Tuple

import pyqtgraph as pg
from PyQt6 import QtCore, QtWidgets


IGNITER_AUTH_FILE = Path(__file__).with_name("igniter_auth.json")


@dataclass(frozen=True)
class _CalPoint:
    weight: float
    ch0_raw: float
    ch1_raw: float


class _CalibrationDialog(QtWidgets.QDialog):
    def __init__(
        self,
        *,
        channel: int,
        samples_per_point: int,
        start_capture,
        existing_points: list[_CalPoint],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Calibrate Ch{channel}")
        self.setModal(False)
        self.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        self.resize(420, 320)

        self._channel = channel
        self._samples_per_point = samples_per_point
        self._start_capture = start_capture
        self._points: list[_CalPoint] = list(existing_points)
        self._capturing = False
        self._current_weight: Optional[float] = None

        layout = QtWidgets.QVBoxLayout(self)

        self.info_lbl = QtWidgets.QLabel(
            "Step 1 sets the 0 kg point. After that you can enter any whole kg value."
        )
        layout.addWidget(self.info_lbl)

        self.list = QtWidgets.QListWidget()
        layout.addWidget(self.list, stretch=1)

        edit_row = QtWidgets.QHBoxLayout()
        layout.addLayout(edit_row)
        self.edit_btn = QtWidgets.QPushButton("Edit selected")
        self.edit_btn.clicked.connect(self._edit_selected)
        edit_row.addWidget(self.edit_btn)
        self.delete_btn = QtWidgets.QPushButton("Delete selected")
        self.delete_btn.clicked.connect(self._delete_selected)
        edit_row.addWidget(self.delete_btn)
        self.reset_btn = QtWidgets.QPushButton("Reset sequence")
        self.reset_btn.clicked.connect(self._reset_sequence)
        edit_row.addWidget(self.reset_btn)
        edit_row.addStretch(1)

        btn_row = QtWidgets.QHBoxLayout()
        layout.addLayout(btn_row)

        self.next_btn = QtWidgets.QPushButton("Capture next point")
        self.next_btn.clicked.connect(self._on_next)
        btn_row.addWidget(self.next_btn)

        self.finish_btn = QtWidgets.QPushButton("Finish")
        self.finish_btn.setEnabled(len(self._points) >= 3)
        self.finish_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.finish_btn)

        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        self.status_lbl = QtWidgets.QLabel("Status: —")
        layout.addWidget(self.status_lbl)

        self._refresh_list()

    def points(self) -> list[_CalPoint]:
        return list(self._points)

    def _refresh_list(self) -> None:
        self.list.clear()
        if not self._points:
            self.list.addItem("(no points yet)")
            return
        for p in self._points:
            raw = p.ch0_raw if self._channel == 0 else p.ch1_raw
            self.list.addItem(f"{p.weight:g} kg → raw {raw:.6g}")
        self.finish_btn.setEnabled(len(self._points) >= 3 and not self._capturing)

    def _on_next(self) -> None:
        if self._capturing:
            return

        if not self._points:
            weight = 0.0
        else:
            dlg = QtWidgets.QInputDialog(self)
            dlg.setWindowTitle("Next calibration point")
            dlg.setLabelText("Enter next weight (kg):")
            dlg.setDoubleDecimals(2)
            dlg.setDoubleRange(0.0, 10000.0)
            dlg.setDoubleValue(float(self._points[-1].weight))
            dlg.setWindowModality(QtCore.Qt.WindowModality.NonModal)

            def on_accept() -> None:
                w = dlg.doubleValue()
                self._begin_capture(float(w))

            def on_reject() -> None:
                self.status_lbl.setText("Status: canceled")

            dlg.accepted.connect(on_accept)
            dlg.rejected.connect(on_reject)
            dlg.open()
            return

        self._begin_capture(weight)

    def _begin_capture(self, weight: float) -> None:
        if self._capturing:
            return

        prompt = (
            f"Place {weight:g} kg on Ch{self._channel}.\n\n"
            f"Keep the load steady, then click OK to capture {self._samples_per_point} samples."
        )
        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle(f"Calibrate Ch{self._channel}")
        msg.setText(prompt)
        msg.setStandardButtons(
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )
        msg.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Yes)
        msg.setWindowModality(QtCore.Qt.WindowModality.NonModal)

        def on_result(result: int) -> None:
            if result != int(QtWidgets.QMessageBox.StandardButton.Yes):
                self.status_lbl.setText("Status: canceled")
                return

            self._capturing = True
            self._current_weight = weight
            self.next_btn.setEnabled(False)
            self.finish_btn.setEnabled(False)
            self.status_lbl.setText(
                f"Status: capturing {self._samples_per_point} samples at {weight:g} kg..."
            )

            def on_done(avg_raw: float) -> None:
                if self._channel == 0:
                    self._points.append(_CalPoint(weight=weight, ch0_raw=avg_raw, ch1_raw=0.0))
                else:
                    self._points.append(_CalPoint(weight=weight, ch0_raw=0.0, ch1_raw=avg_raw))

                self._capturing = False
                self._current_weight = None
                self.next_btn.setEnabled(True)
                self.finish_btn.setEnabled(len(self._points) >= 3)
                self.status_lbl.setText(f"Status: captured {weight:g} kg (avg raw={avg_raw:.6g})")
                self._refresh_list()

            def on_progress(count: int, total: int) -> None:
                if self._capturing and self._current_weight is not None:
                    self.status_lbl.setText(
                        f"Status: capturing {count}/{total} samples at {self._current_weight:g} kg..."
                    )

            self._start_capture(self._channel, on_done, on_progress)

        msg.finished.connect(on_result)
        msg.open()

    def _edit_selected(self) -> None:
        if not self._points:
            self.status_lbl.setText("Status: no points to edit")
            return
        row = self.list.currentRow()
        if row < 0 or row >= len(self._points):
            self.status_lbl.setText("Status: select a point to edit")
            return
        p = self._points[row]

        dlg = QtWidgets.QInputDialog(self)
        dlg.setWindowTitle("Edit point weight")
        dlg.setLabelText("Enter weight (kg):")
        dlg.setDoubleDecimals(2)
        dlg.setDoubleRange(0.0, 10000.0)
        dlg.setDoubleValue(float(p.weight))
        dlg.setWindowModality(QtCore.Qt.WindowModality.NonModal)

        def on_accept() -> None:
            w = float(dlg.doubleValue())
            if self._channel == 0:
                self._points[row] = _CalPoint(weight=w, ch0_raw=p.ch0_raw, ch1_raw=0.0)
            else:
                self._points[row] = _CalPoint(weight=w, ch0_raw=0.0, ch1_raw=p.ch1_raw)
            self.status_lbl.setText(f"Status: updated point to {w:g} kg")
            self._refresh_list()

        def on_reject() -> None:
            self.status_lbl.setText("Status: edit canceled")

        dlg.accepted.connect(on_accept)
        dlg.rejected.connect(on_reject)
        dlg.open()

    def _delete_selected(self) -> None:
        if not self._points:
            self.status_lbl.setText("Status: no points to delete")
            return
        row = self.list.currentRow()
        if row < 0 or row >= len(self._points):
            self.status_lbl.setText("Status: select a point to delete")
            return
        p = self._points[row]
        ok = QtWidgets.QMessageBox.question(
            self,
            "Delete point",
            f"Delete {p.weight:g} kg point?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if ok != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        del self._points[row]
        self.status_lbl.setText("Status: point deleted")
        self._refresh_list()

    def _reset_sequence(self) -> None:
        if not self._points:
            self.status_lbl.setText("Status: already empty")
            return
        ok = QtWidgets.QMessageBox.question(
            self,
            "Reset sequence",
            "Clear all calibration points and restart?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if ok != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._points.clear()
        self.status_lbl.setText("Status: sequence reset")
        self._refresh_list()


class MainWindow(QtWidgets.QMainWindow):
    # logging control signals (GUI -> worker)
    start_saving = QtCore.pyqtSignal(str)     # filename
    stop_saving = QtCore.pyqtSignal()
    pause_saving = QtCore.pyqtSignal(bool)    # True=paused, False=running
    save_last_10s = QtCore.pyqtSignal(str)    # filename
    calibration_saved = QtCore.pyqtSignal()
    raw_stream_enabled = QtCore.pyqtSignal(bool)
    sample_consumed = QtCore.pyqtSignal()
    display_raw_values = QtCore.pyqtSignal(bool)

    def __init__(
        self,
        *,
        history: int = 120000,  # max samples stored (raw + filtered)
        initial_window_seconds: float = 10.0,
        send_command: Optional[Callable[[str, bool], object]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Serial Telemetry Viewer")

        # UI state
        self._paused = False                 # display pause
        self._saving_active = False          # saving on/off
        self._saving_paused = False          # saving paused/unpaused

        self._history = int(history)
        self._window_seconds = float(initial_window_seconds)
        self._default_window_seconds = float(initial_window_seconds)
        self._send_command = send_command
        self._plot_refresh_interval_s = 1.0 / 120.0
        self._last_plot_refresh_mono = 0.0
        self._stats_refresh_interval_s = 1.0 / 8.0
        self._last_stats_refresh_mono = 0.0
        self._max_sample_lag_s = 0.200
        self._max_raw_lag_s = 0.300
        self._no_data_synth_delay_s = 0.100
        self._stream_t0_mono: Optional[float] = None
        self._latest_sample: Optional[tuple[float, float, float, float, float, float, float]] = None
        self._last_real_sample: Optional[tuple[float, float, float, float, float, float, float]] = None
        self._prev_real_sample: Optional[tuple[float, float, float, float, float, float, float]] = None
        self._show_raw_values = False
        self._active_calib_dialog: Optional[_CalibrationDialog] = None

        # Filter settings
        self._default_filter_enabled = True
        self._default_ema_alpha = 0.20
        self._filter_enabled = self._default_filter_enabled
        self._ema_alpha = self._default_ema_alpha  # 0..1

        # EMA state (continues across samples; reset on clear)
        self._ema_ch0: Optional[float] = None
        self._ema_ch1: Optional[float] = None
        self._ema_iadc: Optional[float] = None
        self._ema_batt: Optional[float] = None

        # Full history buffers (DO NOT time-trim; only capped by history samples)
        self._xs = deque(maxlen=self._history)

        # Raw series
        self._raw_ch0 = deque(maxlen=self._history)
        self._raw_ch1 = deque(maxlen=self._history)
        self._raw_iadc = deque(maxlen=self._history)
        self._raw_batt = deque(maxlen=self._history)
        self._hist_ch0_raw = deque(maxlen=self._history)
        self._hist_ch1_raw = deque(maxlen=self._history)
        self._hist_ch0_cal = deque(maxlen=self._history)
        self._hist_ch1_cal = deque(maxlen=self._history)

        # Filtered series (EMA)
        self._flt_ch0 = deque(maxlen=self._history)
        self._flt_ch1 = deque(maxlen=self._history)
        self._flt_iadc = deque(maxlen=self._history)
        self._flt_batt = deque(maxlen=self._history)

        # Cached window maxes (computed over currently displayed series)
        self._max_ch0: Optional[float] = None
        self._max_ch1: Optional[float] = None
        self._max_iadc: Optional[float] = None
        self._max_batt: Optional[float] = None
        self._min_ch0: Optional[float] = None
        self._min_ch1: Optional[float] = None
        self._min_iadc: Optional[float] = None
        self._min_batt: Optional[float] = None

        # Calibration state
        self._calib_points_ch0: list[_CalPoint] = []
        self._calib_points_ch1: list[_CalPoint] = []
        self._calib_samples_per_point = 200
        self._calib_pending_channel: Optional[int] = None
        self._calib_pending_samples: list[float] = []
        self._calib_pending_callback = None
        self._calib_pending_progress_cb = None
        self._raw_ch0_recent = deque(maxlen=40)
        self._raw_ch1_recent = deque(maxlen=40)
        self._cal_editor_proc: Optional[subprocess.Popen] = None
        self._cal_stream_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cal_stream_addr: Optional[tuple[str, int]] = None
        self._cal_stream_watchdog = QtCore.QTimer(self)
        self._cal_stream_watchdog.setInterval(500)
        self._cal_stream_watchdog.timeout.connect(self._on_cal_editor_watchdog)
        self._calib_file_watcher = QtCore.QFileSystemWatcher(self)
        self._calib_file_watcher.fileChanged.connect(self._on_calibration_file_changed)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # -------------------------------------------------
        # Top row: max/current/min values
        # -------------------------------------------------
        top = QtWidgets.QHBoxLayout()
        layout.addLayout(top)

        self.max_ch0_lbl = QtWidgets.QLabel()
        self.max_ch1_lbl = QtWidgets.QLabel()
        self.max_iadc_lbl = QtWidgets.QLabel()
        self.max_batt_lbl = QtWidgets.QLabel()
        self.min_ch0_lbl = QtWidgets.QLabel()
        self.min_ch1_lbl = QtWidgets.QLabel()
        self.min_iadc_lbl = QtWidgets.QLabel()
        self.min_batt_lbl = QtWidgets.QLabel()

        self.cur_ch0_lbl = QtWidgets.QLabel("Cur 50kg: —")
        self.cur_ch1_lbl = QtWidgets.QLabel("Cur 1000kg: —")
        self.cur_iadc_lbl = QtWidgets.QLabel("Cur Tank Pressure: —")
        self.cur_batt_lbl = QtWidgets.QLabel("Cur BattV: —")
        self.data_rate_lbl = QtWidgets.QLabel("Rate: —")

        cards = [
            (self.max_ch0_lbl, self.cur_ch0_lbl, self.min_ch0_lbl),
            (self.max_ch1_lbl, self.cur_ch1_lbl, self.min_ch1_lbl),
            (self.max_iadc_lbl, self.cur_iadc_lbl, self.min_iadc_lbl),
            (self.max_batt_lbl, self.cur_batt_lbl, self.min_batt_lbl),
        ]
        for max_lbl, cur_lbl, min_lbl in cards:
            col = QtWidgets.QVBoxLayout()
            max_lbl.setMinimumWidth(240)
            cur_lbl.setMinimumWidth(240)
            min_lbl.setMinimumWidth(240)
            col.addWidget(max_lbl)
            col.addWidget(cur_lbl)
            col.addWidget(min_lbl)
            top.addLayout(col)

        self.data_rate_lbl.setMinimumWidth(220)
        top.addWidget(self.data_rate_lbl)
        top.addStretch(1)

        # -------------------------------------------------
        # Controls
        # -------------------------------------------------
        controls = QtWidgets.QHBoxLayout()
        layout.addLayout(controls)

        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._on_pause_toggled)
        controls.addWidget(self.pause_btn)

        self.clear_btn = QtWidgets.QPushButton("Clear")
        self.clear_btn.clicked.connect(self._clear)
        controls.addWidget(self.clear_btn)

        controls.addSpacing(20)

        # Window size slider (affects view only)
        controls.addWidget(QtWidgets.QLabel("Window (s):"))
        self.window_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.window_slider.setRange(5, 600)
        self.window_slider.setValue(int(self._window_seconds))
        self.window_slider.setFixedWidth(200)
        self.window_slider.valueChanged.connect(self._on_window_changed)
        controls.addWidget(self.window_slider)

        self.window_value_lbl = QtWidgets.QLabel(f"{int(self._window_seconds)} s")
        self.window_value_lbl.setMinimumWidth(50)
        controls.addWidget(self.window_value_lbl)

        controls.addSpacing(20)

        # Filter enable checkbox
        self.filter_chk = QtWidgets.QCheckBox("Filter enabled")
        self.filter_chk.setChecked(self._filter_enabled)
        self.filter_chk.toggled.connect(self._on_filter_toggled)
        controls.addWidget(self.filter_chk)

        # Filter slider
        controls.addWidget(QtWidgets.QLabel("EMA α:"))
        self.filter_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.filter_slider.setRange(0, 100)
        self.filter_slider.setValue(int(self._ema_alpha * 100))
        self.filter_slider.setFixedWidth(160)
        self.filter_slider.valueChanged.connect(self._on_filter_changed)
        controls.addWidget(self.filter_slider)

        self.filter_value_lbl = QtWidgets.QLabel(f"{self._ema_alpha:.2f}")
        self.filter_value_lbl.setMinimumWidth(40)
        controls.addWidget(self.filter_value_lbl)

        self.reset_window_btn = QtWidgets.QPushButton("Reset window")
        self.reset_window_btn.clicked.connect(self._on_reset_window_control)
        controls.addWidget(self.reset_window_btn)

        self.reset_filter_btn = QtWidgets.QPushButton("Reset filter")
        self.reset_filter_btn.clicked.connect(self._on_reset_filter_control)
        controls.addWidget(self.reset_filter_btn)

        self.raw_values_btn = QtWidgets.QPushButton("Show raw")
        self.raw_values_btn.setCheckable(True)
        self.raw_values_btn.toggled.connect(self._on_raw_values_toggled)
        controls.addWidget(self.raw_values_btn)

        controls.addStretch(1)

        # -------------------------------------------------
        # Command row
        # -------------------------------------------------
        cmd_row = QtWidgets.QHBoxLayout()
        layout.addLayout(cmd_row)

        cmd_row.addWidget(QtWidgets.QLabel("Commands:"))

        def add_cmd_buttons(label: str, cmd_char: str):
            box = QtWidgets.QGroupBox(label)
            box_l = QtWidgets.QHBoxLayout(box)
            on_btn = QtWidgets.QPushButton("ON")
            off_btn = QtWidgets.QPushButton("OFF")
            on_btn.clicked.connect(lambda: self._do_send_command(cmd_char, True))
            off_btn.clicked.connect(lambda: self._do_send_command(cmd_char, False))
            box_l.addWidget(on_btn)
            box_l.addWidget(off_btn)
            cmd_row.addWidget(box)

        add_cmd_buttons("Igniter (I)", "I")
        add_cmd_buttons("Pilot (P)", "P")
        add_cmd_buttons("Tanks (T)", "T")
        add_cmd_buttons("Spare (S)", "S")

        cmd_row.addStretch(1)

        self.cmd_status_lbl = QtWidgets.QLabel("ACK: —")
        self.cmd_status_lbl.setMinimumWidth(260)
        cmd_row.addWidget(self.cmd_status_lbl)

        # -------------------------------------------------
        # Saving / Logging row
        # -------------------------------------------------
        save_row = QtWidgets.QHBoxLayout()
        layout.addLayout(save_row)

        save_row.addWidget(QtWidgets.QLabel("CSV file:"))
        self.filename_edit = QtWidgets.QLineEdit()
        self.filename_edit.setPlaceholderText("Data/HTF_Data.csv")
        self.filename_edit.setFixedWidth(320)
        save_row.addWidget(self.filename_edit)

        # Start saving (visible only when not saving)
        self.start_save_btn = QtWidgets.QPushButton("Start saving")
        self.start_save_btn.clicked.connect(self._on_start_saving)
        save_row.addWidget(self.start_save_btn)

        # Pause saving (replaces start button when saving is active)
        self.pause_save_btn = QtWidgets.QPushButton("Pause saving")
        self.pause_save_btn.setCheckable(True)
        self.pause_save_btn.toggled.connect(self._on_pause_saving_toggled)
        self.pause_save_btn.setVisible(False)  # only when saving active
        save_row.addWidget(self.pause_save_btn)

        # Stop saving (visible only when saving active)
        self.stop_save_btn = QtWidgets.QPushButton("Stop saving")
        self.stop_save_btn.clicked.connect(self._on_stop_saving)
        self.stop_save_btn.setVisible(False)  # only when saving active
        save_row.addWidget(self.stop_save_btn)

        save_row.addSpacing(20)

        # This button will change text depending on paused/running
        self.save_last10_btn = QtWidgets.QPushButton("Save last 10s")
        self.save_last10_btn.clicked.connect(self._on_save_last_10s)
        self.save_last10_btn.setEnabled(False)  # enabled when saving is paused
        save_row.addWidget(self.save_last10_btn)

        save_row.addStretch(1)

        self.save_status_lbl = QtWidgets.QLabel("Saving: OFF")
        self.save_status_lbl.setMinimumWidth(260)
        save_row.addWidget(self.save_status_lbl)

        # -------------------------------------------------
        # Calibration row
        # -------------------------------------------------
        calib_row = QtWidgets.QHBoxLayout()
        layout.addLayout(calib_row)

        calib_row.addWidget(QtWidgets.QLabel("Calibration:"))

        self.calib_filename_edit = QtWidgets.QLineEdit()
        self.calib_filename_edit.setFixedWidth(320)
        self.calib_filename_edit.setPlaceholderText("loadcell_calibration.json")
        self.calib_filename_edit.editingFinished.connect(self._on_calibration_filename_edited)
        calib_row.addWidget(self.calib_filename_edit)

        self.calib_ch0_btn = QtWidgets.QPushButton("Open Cal GUI")
        self.calib_ch0_btn.clicked.connect(self._open_external_calibration_gui)
        calib_row.addWidget(self.calib_ch0_btn)

        self.calib_ch1_btn = QtWidgets.QPushButton("Reload config")
        self.calib_ch1_btn.clicked.connect(self._reload_calibration_from_file)
        calib_row.addWidget(self.calib_ch1_btn)

        self.calib_reset_btn = QtWidgets.QPushButton("Reset points")
        self.calib_reset_btn.clicked.connect(self._reset_calibration_points)
        calib_row.addWidget(self.calib_reset_btn)

        calib_row.addStretch(1)

        self.calib_status_lbl = QtWidgets.QLabel("Calib: —")
        self.calib_status_lbl.setMinimumWidth(260)
        calib_row.addWidget(self.calib_status_lbl)

        # -------------------------------------------------
        # Plots
        # -------------------------------------------------
        pg.setConfigOptions(antialias=True)
        self.plot_widget = pg.GraphicsLayoutWidget()
        layout.addWidget(self.plot_widget, stretch=1)

        self.p0 = self.plot_widget.addPlot(row=0, col=0, title="50kg Channel")
        self.p1 = self.plot_widget.addPlot(row=1, col=0, title="1000kg Channel")
        self.p2 = self.plot_widget.addPlot(row=2, col=0, title="Tank Pressure")
        self.p3 = self.plot_widget.addPlot(row=3, col=0, title="Battery Voltage")

        for p in (self.p0, self.p1, self.p2, self.p3):
            p.showGrid(x=True, y=True)
            p.setLabel("bottom", "Time (s)")

        self.c0 = self.p0.plot([], [])
        self.c1 = self.p1.plot([], [])
        self.c2 = self.p2.plot([], [])
        self.c3 = self.p3.plot([], [])
        for c in (self.c0, self.c1, self.c2, self.c3):
            c.setDownsampling(auto=True, method="peak")
            c.setClipToView(True)

        self._recompute_window_maxes()
        self._update_labels()
        self._redraw()

        self._frame_timer = QtCore.QTimer(self)
        self._frame_timer.setInterval(8)  # ~120 Hz
        self._frame_timer.timeout.connect(self._consume_latest_sample)
        self._frame_timer.start()

    # -------------------------------------------------
    # Public helper (main.py convenience)
    # -------------------------------------------------
    def set_filename(self, filename: str) -> None:
        self.filename_edit.setText(filename)

    def set_calibration_filename(self, filename: str) -> None:
        self.calib_filename_edit.setText(filename)
        self._refresh_calibration_watch()

    def _get_filename(self) -> str:
        name = self.filename_edit.text().strip()
        return name if name else "Data/HTF_Data.csv"

    def _get_calibration_filename(self) -> str:
        name = self.calib_filename_edit.text().strip()
        return name if name else "loadcell_calibration.json"

    def _refresh_calibration_watch(self) -> None:
        try:
            watched = list(self._calib_file_watcher.files())
            if watched:
                self._calib_file_watcher.removePaths(watched)
        except Exception:
            pass
        try:
            path = Path(self._get_calibration_filename()).expanduser().resolve()
            if path.exists():
                self._calib_file_watcher.addPath(str(path))
        except Exception:
            pass

    def _on_calibration_filename_edited(self) -> None:
        self._refresh_calibration_watch()
        self.calibration_saved.emit()
        self.calib_status_lbl.setText("Calib: config path updated and reloaded")

    def _on_calibration_file_changed(self, _path: str) -> None:
        self.calibration_saved.emit()
        self.calib_status_lbl.setText("Calib: auto-reloaded from calibration UI save")
        QtCore.QTimer.singleShot(150, self._refresh_calibration_watch)

    def _sync_saving_ui(self) -> None:
        # Start visible only when not saving
        self.start_save_btn.setVisible(not self._saving_active)

        # Pause/Stop visible only when saving
        self.pause_save_btn.setVisible(self._saving_active)
        self.stop_save_btn.setVisible(self._saving_active)

        if not self._saving_active:
            self.pause_save_btn.setChecked(False)
            self.pause_save_btn.setText("Pause saving")
            self._saving_paused = False
            self.save_last10_btn.setEnabled(False)
            self.save_last10_btn.setText("Save last 10s")
            self.save_status_lbl.setText("Saving: OFF")
        else:
            fn = self._get_filename()
            if self._saving_paused:
                self.save_status_lbl.setText(f"Saving: PAUSED → {fn}")
                self.save_last10_btn.setEnabled(True)
                # NEW: button text indicates it resumes saving too
                self.save_last10_btn.setText("Save + Resume (10s)")
                self.pause_save_btn.setText("Resume saving")
            else:
                self.save_status_lbl.setText(f"Saving: ON → {fn}")
                self.save_last10_btn.setEnabled(False)
                self.save_last10_btn.setText("Save last 10s")
                self.pause_save_btn.setText("Pause saving")

    # -------------------------------------------------
    # Guards
    # -------------------------------------------------
    def _confirm(self, title: str, text: str) -> bool:
        resp = QtWidgets.QMessageBox.question(
            self,
            title,
            text,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return resp == QtWidgets.QMessageBox.StandardButton.Yes

    def _prompt_igniter_password(self) -> bool:
        pw, ok = QtWidgets.QInputDialog.getText(
            self,
            "Igniter arming",
            "Enter igniter password:",
            QtWidgets.QLineEdit.EchoMode.Password,
        )
        if not ok:
            self.cmd_status_lbl.setText("Igniter: cancelled")
            return False
        if not self._verify_igniter_password(pw.strip()):
            self.cmd_status_lbl.setText("Igniter: wrong password")
            return False
        return True

    # -------------------------------------------------
    # Saving UI callbacks
    # -------------------------------------------------
    def _on_start_saving(self) -> None:
        fn = self._get_filename()
        self._saving_active = True
        self._saving_paused = False
        self.start_saving.emit(fn)
        self.pause_saving.emit(False)  # ensure running
        self._sync_saving_ui()

    def _on_stop_saving(self) -> None:
        if not self._saving_active:
            return

        if not self._confirm(
            "Stop saving?",
            "This will stop writing data to the CSV file.\n\nStop saving now?",
        ):
            return

        self._saving_active = False
        self._saving_paused = False
        self.stop_saving.emit()
        self.pause_saving.emit(False)
        self._sync_saving_ui()

    def _on_pause_saving_toggled(self, checked: bool) -> None:
        if not self._saving_active:
            self.pause_save_btn.setChecked(False)
            return
        self._saving_paused = bool(checked)
        self.pause_saving.emit(self._saving_paused)
        self._sync_saving_ui()

    def _on_save_last_10s(self) -> None:
        """
        NEW behavior:
        - When saving is paused, this does:
            1) save last 10s
            2) resume saving immediately
        - UI updates accordingly (button text + pause toggle)
        """
        if not self._saving_active or not self._saving_paused:
            return

        fn = self._get_filename()

        # 1) Save last 10 seconds
        self.save_last_10s.emit(fn)

        # 2) Resume saving immediately
        self._saving_paused = False
        # blockSignals avoids recursion via toggled signal
        self.pause_save_btn.blockSignals(True)
        self.pause_save_btn.setChecked(False)
        self.pause_save_btn.blockSignals(False)

        self.pause_saving.emit(False)

        # 3) Update UI
        self.save_status_lbl.setText(f"Saved 10s, resumed → {fn}")
        self._sync_saving_ui()

    # -------------------------------------------------
    # Command handling
    # -------------------------------------------------
    def _do_send_command(self, cmd: str, on: bool) -> None:
        if self._send_command is None:
            self.cmd_status_lbl.setText("ACK: no radio hooked up")
            return

        # Guard igniter
        if cmd.upper() == "I" and on:
            if not self._prompt_igniter_password():
                return

            if not self._confirm(
                "IGNITER ON",
                "You are about to turn the IGNITER ON.\n\nAre you absolutely sure?",
            ):
                self.cmd_status_lbl.setText("Igniter: aborted")
                return

        self._send_command(cmd, on)
        self.cmd_status_lbl.setText(f"Sent: {cmd} {'ON' if on else 'OFF'} (waiting...)")

    # -------------------------------------------------
    # UI callbacks
    # -------------------------------------------------
    def _on_pause_toggled(self, checked: bool) -> None:
        # display pause only (does NOT affect saving)
        self._paused = checked
        self.pause_btn.setText("Resume" if checked else "Pause")

    def _on_window_changed(self, value: int) -> None:
        self._window_seconds = float(value)
        self.window_value_lbl.setText(f"{value} s")
        self._recompute_window_maxes()
        self._update_labels()
        self._redraw()

    def _on_filter_toggled(self, checked: bool) -> None:
        self._filter_enabled = bool(checked)
        self._recompute_window_maxes()
        self._update_labels()
        self._redraw()

    def _on_filter_changed(self, value: int) -> None:
        self._ema_alpha = float(value) / 100.0
        self.filter_value_lbl.setText(f"{self._ema_alpha:.2f}")
        self._reset_filter_state()

    def _on_reset_window_control(self) -> None:
        self.window_slider.setValue(int(self._default_window_seconds))
        self._recompute_window_maxes()
        self._update_labels()
        self._redraw()

    def _on_reset_filter_control(self) -> None:
        self.filter_chk.setChecked(self._default_filter_enabled)
        self.filter_slider.setValue(int(round(self._default_ema_alpha * 100.0)))
        self.filter_value_lbl.setText(f"{self._default_ema_alpha:.2f}")
        self._reset_filter_state()
        self._recompute_window_maxes()
        self._update_labels()
        self._redraw()

    def _on_raw_values_toggled(self, checked: bool) -> None:
        self.raw_values_btn.setText("Show calibrated" if checked else "Show raw")
        self._show_raw_values = bool(checked)
        self._rebuild_display_series_from_history()
        self._recompute_window_maxes()
        self._update_labels()
        self._redraw()

    def _rebuild_display_series_from_history(self) -> None:
        if not self._xs:
            self._raw_ch0.clear()
            self._raw_ch1.clear()
            self._flt_ch0.clear()
            self._flt_ch1.clear()
            return

        self._raw_ch0.clear()
        self._raw_ch1.clear()
        self._flt_ch0.clear()
        self._flt_ch1.clear()
        self._flt_iadc.clear()
        self._flt_batt.clear()
        self._reset_filter_state()

        if self._show_raw_values:
            src0 = list(self._hist_ch0_raw)
            src1 = list(self._hist_ch1_raw)
        else:
            src0 = list(self._hist_ch0_cal)
            src1 = list(self._hist_ch1_cal)
        yi = list(self._raw_iadc)
        yb = list(self._raw_batt)

        n = min(len(src0), len(src1), len(yi), len(yb))
        if n <= 0:
            return

        a = self._ema_alpha
        for i in range(n):
            c0 = float(src0[i])
            c1 = float(src1[i])
            ci = float(yi[i])
            cb = float(yb[i])
            self._raw_ch0.append(c0)
            self._raw_ch1.append(c1)
            if self._ema_ch0 is None:
                self._ema_ch0 = c0
                self._ema_ch1 = c1
                self._ema_iadc = ci
                self._ema_batt = cb
            else:
                self._ema_ch0 = (1.0 - a) * self._ema_ch0 + a * c0
                self._ema_ch1 = (1.0 - a) * self._ema_ch1 + a * c1
                self._ema_iadc = (1.0 - a) * self._ema_iadc + a * ci
                self._ema_batt = (1.0 - a) * self._ema_batt + a * cb
            self._flt_ch0.append(float(self._ema_ch0))
            self._flt_ch1.append(float(self._ema_ch1))
            self._flt_iadc.append(float(self._ema_iadc))
            self._flt_batt.append(float(self._ema_batt))

    def _reset_filter_state(self) -> None:
        self._ema_ch0 = None
        self._ema_ch1 = None
        self._ema_iadc = None
        self._ema_batt = None

    def _clear(self) -> None:
        self._xs.clear()
        self._raw_ch0.clear()
        self._raw_ch1.clear()
        self._raw_iadc.clear()
        self._raw_batt.clear()
        self._hist_ch0_raw.clear()
        self._hist_ch1_raw.clear()
        self._hist_ch0_cal.clear()
        self._hist_ch1_cal.clear()
        self._flt_ch0.clear()
        self._flt_ch1.clear()
        self._flt_iadc.clear()
        self._flt_batt.clear()

        self._max_ch0 = self._max_ch1 = self._max_iadc = self._max_batt = None
        self._min_ch0 = self._min_ch1 = self._min_iadc = self._min_batt = None
        self._reset_filter_state()
        self._stream_t0_mono = None
        self._latest_sample = None
        self._last_real_sample = None
        self._prev_real_sample = None
        self._last_plot_refresh_mono = 0.0
        self._last_stats_refresh_mono = 0.0
        self._update_labels()
        self._redraw()

    def _reset_calibration_points(self) -> None:
        self._calib_points_ch0.clear()
        self._calib_points_ch1.clear()
        self._calib_pending_channel = None
        self._calib_pending_samples.clear()
        self.raw_stream_enabled.emit(False)
        self.calib_status_lbl.setText("Calib: reset")

    # -------------------------------------------------
    # Window view helpers (slice only; do NOT delete history)
    # -------------------------------------------------
    def _get_active_series(self) -> Tuple[list, list, list, list, list]:
        xs = list(self._xs)
        if self._filter_enabled:
            y0 = list(self._flt_ch0)
            y1 = list(self._flt_ch1)
            y2 = list(self._flt_iadc)
            y3 = list(self._flt_batt)
        else:
            y0 = list(self._raw_ch0)
            y1 = list(self._raw_ch1)
            y2 = list(self._raw_iadc)
            y3 = list(self._raw_batt)

        if not xs:
            return [], [], [], [], []

        cutoff = xs[-1] - self._window_seconds
        i0 = 0
        for i, x in enumerate(xs):
            if x >= cutoff:
                i0 = i
                break

        return xs[i0:], y0[i0:], y1[i0:], y2[i0:], y3[i0:]

    def _recompute_window_maxes(self) -> None:
        xs, y0, y1, y2, y3 = self._get_active_series()
        if not xs:
            self._max_ch0 = self._max_ch1 = self._max_iadc = self._max_batt = None
            self._min_ch0 = self._min_ch1 = self._min_iadc = self._min_batt = None
            return
        self._max_ch0 = max(y0) if y0 else None
        self._max_ch1 = max(y1) if y1 else None
        self._max_iadc = max(y2) if y2 else None
        self._max_batt = max(y3) if y3 else None
        self._min_ch0 = min(y0) if y0 else None
        self._min_ch1 = min(y1) if y1 else None
        self._min_iadc = min(y2) if y2 else None
        self._min_batt = min(y3) if y3 else None

    def _update_labels(self) -> None:
        def ff(v):
            return "—" if v is None else f"{v:.3f}"

        ws = int(self._window_seconds)
        mode = "F" if self._filter_enabled else "R"

        self.max_ch0_lbl.setText(f"Max 50kg ({ws}s) [{mode}]: {ff(self._max_ch0)}")
        self.max_ch1_lbl.setText(f"Max 1000kg ({ws}s) [{mode}]: {ff(self._max_ch1)}")
        self.max_iadc_lbl.setText(f"Max Tank Pressure ({ws}s) [{mode}]: {ff(self._max_iadc)}")
        self.max_batt_lbl.setText(f"Max BattV ({ws}s) [{mode}]: {ff(self._max_batt)}")
        self.min_ch0_lbl.setText(f"Min 50kg ({ws}s) [{mode}]: {ff(self._min_ch0)}")
        self.min_ch1_lbl.setText(f"Min 1000kg ({ws}s) [{mode}]: {ff(self._min_ch1)}")
        self.min_iadc_lbl.setText(f"Min Tank Pressure ({ws}s) [{mode}]: {ff(self._min_iadc)}")
        self.min_batt_lbl.setText(f"Min BattV ({ws}s) [{mode}]: {ff(self._min_batt)}")

        if not self._xs:
            self.cur_ch0_lbl.setText("Cur 50kg: —")
            self.cur_ch1_lbl.setText("Cur 1000kg: —")
            self.cur_iadc_lbl.setText("Cur Tank Pressure: —")
            self.cur_batt_lbl.setText("Cur BattV: —")
            return

        if self._filter_enabled:
            c0 = self._flt_ch0[-1] if self._flt_ch0 else None
            c1 = self._flt_ch1[-1] if self._flt_ch1 else None
            ci = self._flt_iadc[-1] if self._flt_iadc else None
            cb = self._flt_batt[-1] if self._flt_batt else None
        else:
            c0 = self._raw_ch0[-1] if self._raw_ch0 else None
            c1 = self._raw_ch1[-1] if self._raw_ch1 else None
            ci = self._raw_iadc[-1] if self._raw_iadc else None
            cb = self._raw_batt[-1] if self._raw_batt else None

        self.cur_ch0_lbl.setText(f"Cur 50kg [{mode}]: {ff(c0)}")
        self.cur_ch1_lbl.setText(f"Cur 1000kg [{mode}]: {ff(c1)}")
        self.cur_iadc_lbl.setText(f"Cur Tank Pressure [{mode}]: {ff(ci)}")
        self.cur_batt_lbl.setText(f"Cur BattV [{mode}]: {ff(cb)}")

    def _redraw(self) -> None:
        xs, y0, y1, y2, y3 = self._get_active_series()

        self.c0.setData(xs, y0)
        self.c1.setData(xs, y1)
        self.c2.setData(xs, y2)
        self.c3.setData(xs, y3)

        if xs:
            xmin = xs[-1] - self._window_seconds
            xmax = xs[-1]
            for p in (self.p0, self.p1, self.p2, self.p3):
                p.setXRange(xmin, xmax, padding=0)

    @QtCore.pyqtSlot(float, float)
    def on_data_rate(self, bytes_per_s: float, packets_per_s: float) -> None:
        kbps = (float(bytes_per_s) * 8.0) / 1000.0
        self.data_rate_lbl.setText(f"Rate: {kbps:.1f} kbps ({packets_per_s:.0f} pkt/s)")

    # -------------------------------------------------
    # Data entry
    # -------------------------------------------------
    @QtCore.pyqtSlot(float, float, float, float, float, float, float)
    def on_sample(
        self,
        t_mono: float,
        ch0_raw: float,
        ch1_raw: float,
        ch0_cal: float,
        ch1_cal: float,
        tank_pressure: float,
        battery_voltage: float,
    ) -> None:
        self._latest_sample = (
            float(t_mono),
            float(ch0_raw),
            float(ch1_raw),
            float(ch0_cal),
            float(ch1_cal),
            float(tank_pressure),
            float(battery_voltage),
        )
        self.sample_consumed.emit()

    def _append_processed_sample(
        self,
        t_mono: float,
        ch0_raw: float,
        ch1_raw: float,
        ch0_cal: float,
        ch1_cal: float,
        internal_adc: float,
        battery_voltage: float,
    ) -> None:
        if self._stream_t0_mono is None:
            self._stream_t0_mono = t_mono
        t_seconds = t_mono - self._stream_t0_mono

        self._xs.append(float(t_seconds))
        self._hist_ch0_raw.append(float(ch0_raw))
        self._hist_ch1_raw.append(float(ch1_raw))
        self._hist_ch0_cal.append(float(ch0_cal))
        self._hist_ch1_cal.append(float(ch1_cal))
        ch0 = float(ch0_raw) if self._show_raw_values else float(ch0_cal)
        ch1 = float(ch1_raw) if self._show_raw_values else float(ch1_cal)
        self._raw_ch0.append(ch0)
        self._raw_ch1.append(ch1)
        self._raw_iadc.append(float(internal_adc))
        self._raw_batt.append(float(battery_voltage))

        a = self._ema_alpha
        if self._ema_ch0 is None:
            self._ema_ch0 = float(ch0)
            self._ema_ch1 = float(ch1)
            self._ema_iadc = float(internal_adc)
            self._ema_batt = float(battery_voltage)
        else:
            self._ema_ch0 = (1.0 - a) * self._ema_ch0 + a * float(ch0)
            self._ema_ch1 = (1.0 - a) * self._ema_ch1 + a * float(ch1)
            self._ema_iadc = (1.0 - a) * self._ema_iadc + a * float(internal_adc)
            self._ema_batt = (1.0 - a) * self._ema_batt + a * float(battery_voltage)

        self._flt_ch0.append(float(self._ema_ch0))
        self._flt_ch1.append(float(self._ema_ch1))
        self._flt_iadc.append(float(self._ema_iadc))
        self._flt_batt.append(float(self._ema_batt))

    def _consume_latest_sample(self) -> None:
        if self._paused:
            self._latest_sample = None
            return
        now = time.monotonic()
        used_sample = False

        if self._latest_sample is not None:
            t_mono, ch0_raw, ch1_raw, ch0_cal, ch1_cal, internal_adc, battery_voltage = self._latest_sample
            self._latest_sample = None
            if (now - t_mono) <= self._max_sample_lag_s:
                self._append_processed_sample(
                    t_mono, ch0_raw, ch1_raw, ch0_cal, ch1_cal, internal_adc, battery_voltage
                )
                self._prev_real_sample = self._last_real_sample
                self._last_real_sample = (
                    t_mono, ch0_raw, ch1_raw, ch0_cal, ch1_cal, internal_adc, battery_voltage
                )
                used_sample = True

        if (not used_sample) and (self._last_real_sample is not None):
            last_t, last_c0_raw, last_c1_raw, last_c0_cal, last_c1_cal, last_iadc, last_batt = self._last_real_sample
            if (now - last_t) >= self._no_data_synth_delay_s:
                slope_c0_raw = slope_c1_raw = slope_c0_cal = slope_c1_cal = slope_iadc = slope_batt = 0.0
                if self._prev_real_sample is not None:
                    prev_t, prev_c0_raw, prev_c1_raw, prev_c0_cal, prev_c1_cal, prev_iadc, prev_batt = self._prev_real_sample
                    dt = last_t - prev_t
                    if dt > 1e-6:
                        slope_c0_raw = (last_c0_raw - prev_c0_raw) / dt
                        slope_c1_raw = (last_c1_raw - prev_c1_raw) / dt
                        slope_c0_cal = (last_c0_cal - prev_c0_cal) / dt
                        slope_c1_cal = (last_c1_cal - prev_c1_cal) / dt
                        slope_iadc = (float(last_iadc) - float(prev_iadc)) / dt
                        slope_batt = (last_batt - prev_batt) / dt
                dt_now = now - last_t
                synth_c0_raw = last_c0_raw + slope_c0_raw * dt_now
                synth_c1_raw = last_c1_raw + slope_c1_raw * dt_now
                synth_c0_cal = last_c0_cal + slope_c0_cal * dt_now
                synth_c1_cal = last_c1_cal + slope_c1_cal * dt_now
                synth_iadc = float(last_iadc) + slope_iadc * dt_now
                synth_batt = last_batt + slope_batt * dt_now
                self._append_processed_sample(
                    now, synth_c0_raw, synth_c1_raw, synth_c0_cal, synth_c1_cal, synth_iadc, synth_batt
                )
                used_sample = True

        if not used_sample:
            return

        self._redraw()
        self._last_plot_refresh_mono = now
        if (now - self._last_stats_refresh_mono) >= self._stats_refresh_interval_s:
            self._recompute_window_maxes()
            self._update_labels()
            self._last_stats_refresh_mono = now

    @QtCore.pyqtSlot(float, float, float, float)
    def on_raw_sample(self, t_mono: float, ch0_raw: float, ch1_raw: float, iadc_raw: float) -> None:
        _ = t_mono  # timestamp retained in signal for future tuning
        self._raw_ch0_recent.append(float(ch0_raw))
        self._raw_ch1_recent.append(float(ch1_raw))
        if self._cal_stream_addr is not None:
            try:
                payload = (
                    f"{float(t_mono):.6f},{float(ch0_raw):.9g},{float(ch1_raw):.9g},{float(iadc_raw):.9g}"
                ).encode("ascii")
                self._cal_stream_sock.sendto(payload, self._cal_stream_addr)
            except Exception:
                pass

        # Calibration capture (averaging)
        if self._calib_pending_channel is None:
            return

        if self._calib_pending_channel == 0:
            self._calib_pending_samples.append(float(ch0_raw))
        else:
            self._calib_pending_samples.append(float(ch1_raw))

        if self._calib_pending_progress_cb is not None:
            self._calib_pending_progress_cb(
                len(self._calib_pending_samples), self._calib_samples_per_point
            )

        if len(self._calib_pending_samples) >= self._calib_samples_per_point:
            # channel = self._calib_pending_channel
            avg = sum(self._calib_pending_samples) / len(self._calib_pending_samples)

            cb = self._calib_pending_callback
            pcb = self._calib_pending_progress_cb
            self._calib_pending_channel = None
            self._calib_pending_samples.clear()
            self._calib_pending_callback = None
            self._calib_pending_progress_cb = None
            if cb is not None:
                cb(avg)
            if pcb is not None:
                pcb(self._calib_samples_per_point, self._calib_samples_per_point)

    # -------------------------------------------------
    # Calibration helpers
    # -------------------------------------------------
    def _open_external_calibration_gui(self) -> None:
        try:
            if self._cal_editor_proc is not None and self._cal_editor_proc.poll() is None:
                self.calib_status_lbl.setText("Calib: external editor already open")
                return
        except Exception:
            self._cal_editor_proc = None

        script = Path(__file__).with_name("edit_calibration_gui.py")
        calib_path = self._get_calibration_filename()
        try:
            tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tmp.bind(("127.0.0.1", 0))
            raw_port = int(tmp.getsockname()[1])
            tmp.close()
            self._cal_stream_addr = ("127.0.0.1", raw_port)
            self._cal_editor_proc = subprocess.Popen([
                sys.executable,
                str(script),
                calib_path,
                "--raw-port",
                str(raw_port),
            ], start_new_session=True)
            self.raw_stream_enabled.emit(True)
            self._cal_stream_watchdog.start()
            self.calib_status_lbl.setText("Calib: opened external editor")
        except Exception as e:
            self._cal_stream_addr = None
            self.calib_status_lbl.setText(f"Calib: failed to open editor ({e})")

    def _on_cal_editor_watchdog(self) -> None:
        if self._cal_editor_proc is None:
            self._cal_stream_watchdog.stop()
            self._cal_stream_addr = None
            self.raw_stream_enabled.emit(False)
            return
        if self._cal_editor_proc.poll() is None:
            return
        self._cal_stream_watchdog.stop()
        self._cal_editor_proc = None
        self._cal_stream_addr = None
        self.raw_stream_enabled.emit(False)

    def _reload_calibration_from_file(self) -> None:
        self.calibration_saved.emit()
        self._refresh_calibration_watch()
        self.calib_status_lbl.setText("Calib: reload requested")

    def _open_calibration_dialog(self, channel: int) -> None:
        if self._active_calib_dialog is not None:
            self._active_calib_dialog.raise_()
            self._active_calib_dialog.activateWindow()
            return

        points = self._calib_points_ch0 if channel == 0 else self._calib_points_ch1
        self.raw_stream_enabled.emit(True)

        def start_capture(ch: int, done_cb, progress_cb) -> None:
            if self._calib_pending_channel is not None:
                self.calib_status_lbl.setText("Calib: capture in progress, please wait")
                return
            if len(self._raw_ch0_recent) < 5 or len(self._raw_ch1_recent) < 5:
                self.calib_status_lbl.setText("Calib: not enough samples yet")
                return
            self.raw_stream_enabled.emit(True)
            self._calib_pending_channel = ch
            self._calib_pending_samples.clear()
            self._calib_pending_callback = done_cb
            self._calib_pending_progress_cb = progress_cb

        dlg = _CalibrationDialog(
            channel=channel,
            samples_per_point=self._calib_samples_per_point,
            start_capture=start_capture,
            existing_points=points,
            parent=self,
        )
        self._active_calib_dialog = dlg

        def on_finished(result: int) -> None:
            if result == int(QtWidgets.QDialog.DialogCode.Accepted):
                new_points = dlg.points()
                if channel == 0:
                    self._calib_points_ch0 = new_points
                else:
                    self._calib_points_ch1 = new_points
                self.calib_status_lbl.setText(
                    f"Calib: Ch{channel} points set ({len(new_points)} point(s))"
                )
            # Ensure calibration capture state is always reset on dialog exit.
            self._calib_pending_channel = None
            self._calib_pending_samples.clear()
            self._calib_pending_callback = None
            self._calib_pending_progress_cb = None
            self.raw_stream_enabled.emit(False)
            self._active_calib_dialog = None
            dlg.deleteLater()

        dlg.finished.connect(on_finished)
        dlg.open()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._cal_stream_addr = None
        self.raw_stream_enabled.emit(False)
        try:
            self._cal_stream_watchdog.stop()
        except Exception:
            pass
        try:
            self._cal_stream_sock.close()
        except Exception:
            pass
        try:
            if self._cal_editor_proc is not None and self._cal_editor_proc.poll() is None:
                self._cal_editor_proc.terminate()
                try:
                    self._cal_editor_proc.wait(timeout=1.0)
                except Exception:
                    self._cal_editor_proc.kill()
                    self._cal_editor_proc.wait(timeout=1.0)
        except Exception:
            pass
        self._cal_editor_proc = None
        super().closeEvent(event)

    def _verify_igniter_password(self, password: str) -> bool:
        if not IGNITER_AUTH_FILE.exists():
            self.cmd_status_lbl.setText("Igniter: auth file missing")
            return False
        try:
            data = json.loads(IGNITER_AUTH_FILE.read_text())
            salt_hex = data.get("salt_hex", "")
            hash_hex = data.get("hash_hex", "")
            iterations = int(data.get("iterations", 120000))
            if not salt_hex or not hash_hex:
                return False
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(hash_hex)
            candidate = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt, iterations
            )
            return hmac.compare_digest(candidate, expected)
        except Exception:
            return False

    def _save_calibration(self) -> None:
        points0 = list(self._calib_points_ch0)
        points1 = list(self._calib_points_ch1)
        has_ch0 = len(points0) >= 3
        has_ch1 = len(points1) >= 3
        if not has_ch0 and not has_ch1:
            self.calib_status_lbl.setText("Calib: capture at least 3 points for Ch0 or Ch1")
            return

        def fit_line(xs: list[float], ys: list[float]) -> tuple[float, float]:
            n = float(len(xs))
            sx = sum(xs)
            sy = sum(ys)
            sxx = sum(x * x for x in xs)
            sxy = sum(x * y for x, y in zip(xs, ys))
            denom = (n * sxx) - (sx * sx)
            if denom == 0.0:
                raise ValueError("degenerate calibration points")
            m = (n * sxy - sx * sy) / denom
            b = (sy - m * sx) / n
            return m, b

        def fit_line_through_zero(xs: list[float], ys: list[float]) -> float:
            denom = sum(x * x for x in xs)
            if denom == 0.0:
                raise ValueError("degenerate calibration points")
            return sum(x * y for x, y in zip(xs, ys)) / denom

        def fit_poly2(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
            # Least-squares quadratic fit: y = a*x^2 + b*x + c
            n = float(len(xs))
            sx = sum(xs)
            sx2 = sum(x * x for x in xs)
            sx3 = sum(x * x * x for x in xs)
            sx4 = sum(x * x * x * x for x in xs)
            sy = sum(ys)
            sxy = sum(x * y for x, y in zip(xs, ys))
            sx2y = sum((x * x) * y for x, y in zip(xs, ys))

            # Solve normal equations using Gaussian elimination
            a11, a12, a13 = sx4, sx3, sx2
            a21, a22, a23 = sx3, sx2, sx
            a31, a32, a33 = sx2, sx, n
            b1, b2, b3 = sx2y, sxy, sy

            # Forward elimination
            if a11 == 0.0:
                raise ValueError("degenerate calibration points")
            f21 = a21 / a11
            f31 = a31 / a11
            a21 -= f21 * a11
            a22 -= f21 * a12
            a23 -= f21 * a13
            b2 -= f21 * b1
            a31 -= f31 * a11
            a32 -= f31 * a12
            a33 -= f31 * a13
            b3 -= f31 * b1

            if a22 == 0.0:
                raise ValueError("degenerate calibration points")
            f32 = a32 / a22
            a32 -= f32 * a22
            a33 -= f32 * a23
            b3 -= f32 * b2

            if a33 == 0.0:
                raise ValueError("degenerate calibration points")

            # Back substitution
            c = b3 / a33
            b = (b2 - a23 * c) / a22
            a = (b1 - a12 * b - a13 * c) / a11
            return a, b, c

        def fit_poly2_through_zero(xs: list[float], ys: list[float]) -> tuple[float, float]:
            # Fit y = a*x^2 + b*x with c=0
            sx2 = sum(x * x for x in xs)
            sx3 = sum(x * x * x for x in xs)
            sx4 = sum(x * x * x * x for x in xs)
            sxy = sum(x * y for x, y in zip(xs, ys))
            sx2y = sum((x * x) * y for x, y in zip(xs, ys))

            # Solve 2x2
            det = sx4 * sx2 - sx3 * sx3
            if det == 0.0:
                raise ValueError("degenerate calibration points")
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
            if sse <= 0.0:
                sse = 1e-18
            return n * (math.log(sse / n)) + (2 * k)

        ch0_m = ch0_b = None
        ch0_x0 = None
        ch0_poly2 = None
        ch0_fit_type = None
        ch1_m = ch1_b = None
        ch1_poly2 = None
        ch1_fit_type = None
        ch1_x0 = None
        try:
            if has_ch0:
                xs0 = [p.ch0_raw for p in points0]
                ys0 = [p.weight for p in points0]
                # If 0 kg point exists, force fit through it.
                zero_points0 = [p for p in points0 if abs(p.weight) < 1e-9]
                if zero_points0:
                    ch0_x0 = zero_points0[0].ch0_raw
                    xs0_shift = [x - ch0_x0 for x in xs0]
                    ys0_shift = [y - 0.0 for y in ys0]
                    m0 = fit_line_through_zero(xs0_shift, ys0_shift)
                    sse_l0 = sse_line(xs0_shift, ys0_shift, m0, 0.0)
                    aic_l0 = aic(sse_l0, len(xs0_shift), 1)

                    a2, b2 = fit_poly2_through_zero(xs0_shift, ys0_shift)
                    sse_q0 = sse_poly2(xs0_shift, ys0_shift, a2, b2, 0.0)
                    aic_q0 = aic(sse_q0, len(xs0_shift), 2)

                    if aic_q0 < aic_l0:
                        ch0_fit_type = "poly2"
                        ch0_poly2 = (a2, b2, 0.0)
                        ch0_m = m0
                        ch0_b = -m0 * ch0_x0
                    else:
                        ch0_fit_type = "linear"
                        ch0_m = m0
                        ch0_b = -m0 * ch0_x0
                else:
                    ch0_m, ch0_b = fit_line(xs0, ys0)
                    sse_l0 = sse_line(xs0, ys0, ch0_m, ch0_b)
                    aic_l0 = aic(sse_l0, len(xs0), 2)

                    ch0_poly2 = fit_poly2(xs0, ys0)
                    sse_q0 = sse_poly2(xs0, ys0, *ch0_poly2)
                    aic_q0 = aic(sse_q0, len(xs0), 3)

                    if aic_q0 < aic_l0:
                        ch0_fit_type = "poly2"
                    else:
                        ch0_fit_type = "linear"
            if has_ch1:
                xs1 = [p.ch1_raw for p in points1]
                ys1 = [p.weight for p in points1]
                zero_points1 = [p for p in points1 if abs(p.weight) < 1e-9]
                if zero_points1:
                    ch1_x0 = zero_points1[0].ch1_raw
                    xs1_shift = [x - ch1_x0 for x in xs1]
                    ys1_shift = [y - 0.0 for y in ys1]
                    # Evaluate linear vs quadratic on shifted data, forcing intercept 0
                    m0 = fit_line_through_zero(xs1_shift, ys1_shift)
                    sse_l = sse_line(xs1_shift, ys1_shift, m0, 0.0)
                    aic_l = aic(sse_l, len(xs1_shift), 1)

                    a2, b2 = fit_poly2_through_zero(xs1_shift, ys1_shift)
                    sse_q = sse_poly2(xs1_shift, ys1_shift, a2, b2, 0.0)
                    aic_q = aic(sse_q, len(xs1_shift), 2)

                    if aic_q < aic_l:
                        ch1_fit_type = "poly2"
                        ch1_poly2 = (a2, b2, 0.0)
                        # Also keep linear fallback mapped to original x
                        ch1_m = m0
                        ch1_b = -m0 * ch1_x0
                    else:
                        ch1_fit_type = "linear"
                        ch1_m = m0
                        ch1_b = -m0 * ch1_x0
                else:
                    # Evaluate linear vs quadratic and pick best by AIC (free intercept)
                    ch1_m, ch1_b = fit_line(xs1, ys1)
                    sse_l = sse_line(xs1, ys1, ch1_m, ch1_b)
                    aic_l = aic(sse_l, len(xs1), 2)

                    ch1_poly2 = fit_poly2(xs1, ys1)
                    sse_q = sse_poly2(xs1, ys1, *ch1_poly2)
                    aic_q = aic(sse_q, len(xs1), 3)

                    if aic_q < aic_l:
                        ch1_fit_type = "poly2"
                    else:
                        ch1_fit_type = "linear"
        except Exception as e:
            self.calib_status_lbl.setText(f"Calib: fit error ({e})")
            return

        path = Path(self._get_calibration_filename())
        # Merge with existing file if present to avoid overwriting channels without data.
        if path.exists():
            try:
                out = json.loads(path.read_text())
            except Exception:
                out = {}
        else:
            out = {}

        out["version"] = 1

        if has_ch0:
            out["ch0"] = {"m": ch0_m, "b": ch0_b}
            out["points"] = [{"kg": p.weight, "ch0_raw": p.ch0_raw} for p in points0]
            if ch0_x0 is not None:
                out["ch0_zero_raw"] = ch0_x0
            if ch0_poly2 is not None and ch0_fit_type == "poly2":
                a, b, c = ch0_poly2
                out["ch0_fit"] = {"type": "poly2", "a": a, "b": b, "c": c, "x0": ch0_x0}
            else:
                out["ch0_fit"] = {"type": "linear", "x0": ch0_x0}
        if has_ch1:
            out["ch1"] = {"m": ch1_m, "b": ch1_b}
            out["points_ch1"] = [{"kg": p.weight, "ch1_raw": p.ch1_raw} for p in points1]
            if ch1_x0 is not None:
                out["ch1_zero_raw"] = ch1_x0
            if ch1_poly2 is not None and ch1_fit_type == "poly2":
                a, b, c = ch1_poly2
                out["ch1_fit"] = {"type": "poly2", "a": a, "b": b, "c": c, "x0": ch1_x0}
            else:
                out["ch1_fit"] = {"type": "linear", "x0": ch1_x0}

        all_weights = []
        if has_ch0:
            all_weights.extend([p.weight for p in points0])
        if has_ch1:
            all_weights.extend([p.weight for p in points1])
        out["weights_kg"] = sorted({w for w in all_weights})

        try:
            path.write_text(json.dumps(out, indent=2))
        except Exception as e:
            self.calib_status_lbl.setText(f"Calib: save failed ({e})")
            return

        saved = []
        if ch0_m is not None and ch0_b is not None:
            saved.append("Ch0")
        if ch1_m is not None and ch1_b is not None:
            saved.append("Ch1")
        saved_text = "/".join(saved) if saved else "none"
        self.calib_status_lbl.setText(f"Calib: saved {saved_text} → {path.resolve()}")
        self.calibration_saved.emit()
