from __future__ import annotations

from collections import deque
from typing import Optional, Callable, Tuple

import pyqtgraph as pg
from PyQt6 import QtCore, QtWidgets


class MainWindow(QtWidgets.QMainWindow):
    def __init__(
            self,
            *,
            history: int = 5000,  # max samples stored (raw + filtered)
            initial_window_seconds: float = 10.0,
            send_command: Optional[Callable[[str, bool], object]] = None,
            parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Serial Telemetry Viewer")

        self._paused = False
        self._history = int(history)
        self._window_seconds = float(initial_window_seconds)
        self._send_command = send_command

        # Filter settings
        self._filter_enabled = True
        self._ema_alpha = 0.20  # 0..1

        # EMA state (continues across samples; reset on clear)
        self._ema_ch0: Optional[float] = None
        self._ema_ch1: Optional[float] = None
        self._ema_iadc: Optional[float] = None

        # Full history buffers (DO NOT time-trim; only capped by history samples)
        self._xs = deque(maxlen=self._history)

        # Raw series
        self._raw_ch0 = deque(maxlen=self._history)
        self._raw_ch1 = deque(maxlen=self._history)
        self._raw_iadc = deque(maxlen=self._history)

        # Filtered series (EMA)
        self._flt_ch0 = deque(maxlen=self._history)
        self._flt_ch1 = deque(maxlen=self._history)
        self._flt_iadc = deque(maxlen=self._history)

        # Cached window maxes (computed over currently displayed series)
        self._max_ch0: Optional[float] = None
        self._max_ch1: Optional[float] = None
        self._max_iadc: Optional[float] = None

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # -------------------------------------------------
        # Top row: max + current values
        # -------------------------------------------------
        top = QtWidgets.QHBoxLayout()
        layout.addLayout(top)

        self.max_ch0_lbl = QtWidgets.QLabel()
        self.max_ch1_lbl = QtWidgets.QLabel()
        self.max_iadc_lbl = QtWidgets.QLabel()

        self.cur_ch0_lbl = QtWidgets.QLabel("Cur Ch0: —")
        self.cur_ch1_lbl = QtWidgets.QLabel("Cur Ch1: —")
        self.cur_iadc_lbl = QtWidgets.QLabel("Cur IADC: —")

        for w in (
                self.max_ch0_lbl,
                self.max_ch1_lbl,
                self.max_iadc_lbl,
                self.cur_ch0_lbl,
                self.cur_ch1_lbl,
                self.cur_iadc_lbl,
        ):
            w.setMinimumWidth(260)
            top.addWidget(w)

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
        self.window_slider.setRange(5, 120)
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
        # Plots
        # -------------------------------------------------
        pg.setConfigOptions(antialias=True)
        self.plot_widget = pg.GraphicsLayoutWidget()
        layout.addWidget(self.plot_widget, stretch=1)

        self.p0 = self.plot_widget.addPlot(row=0, col=0, title="Channel 0")
        self.p1 = self.plot_widget.addPlot(row=1, col=0, title="Channel 1")
        self.p2 = self.plot_widget.addPlot(row=2, col=0, title="Internal ADC")

        for p in (self.p0, self.p1, self.p2):
            p.showGrid(x=True, y=True)
            p.setLabel("bottom", "Time (s)")

        self.c0 = self.p0.plot([], [])
        self.c1 = self.p1.plot([], [])
        self.c2 = self.p2.plot([], [])

        self._recompute_window_maxes()
        self._update_labels()
        self._redraw()

    # -------------------------------------------------
    # Command handling
    # -------------------------------------------------
    def _do_send_command(self, cmd: str, on: bool) -> None:
        if self._send_command is None:
            self.cmd_status_lbl.setText("ACK: no radio hooked up")
            return
        self._send_command(cmd, on)
        self.cmd_status_lbl.setText(f"Sent: {cmd} {'ON' if on else 'OFF'} (waiting...)")

    @QtCore.pyqtSlot(str)
    def on_status(self, msg: str) -> None:
        if msg.startswith("ACK:"):
            self.cmd_status_lbl.setText(msg)
        else:
            self.setWindowTitle(f"Serial Telemetry Viewer — {msg}")

    # -------------------------------------------------
    # UI callbacks
    # -------------------------------------------------
    def _on_pause_toggled(self, checked: bool) -> None:
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
        # (optional) reset EMA so new alpha doesn't “inherit” old smoothing
        self._reset_filter_state()

    def _reset_filter_state(self) -> None:
        self._ema_ch0 = None
        self._ema_ch1 = None
        self._ema_iadc = None

    def _clear(self) -> None:
        self._xs.clear()
        self._raw_ch0.clear()
        self._raw_ch1.clear()
        self._raw_iadc.clear()
        self._flt_ch0.clear()
        self._flt_ch1.clear()
        self._flt_iadc.clear()

        self._max_ch0 = self._max_ch1 = self._max_iadc = None
        self._reset_filter_state()
        self._update_labels()
        self._redraw()

    # -------------------------------------------------
    # Window view helpers (slice only; do NOT delete history)
    # -------------------------------------------------
    def _get_active_series(self) -> Tuple[list, list, list, list]:
        xs = list(self._xs)
        if self._filter_enabled:
            y0 = list(self._flt_ch0)
            y1 = list(self._flt_ch1)
            y2 = list(self._flt_iadc)
        else:
            y0 = list(self._raw_ch0)
            y1 = list(self._raw_ch1)
            y2 = list(self._raw_iadc)

        # Slice to last window_seconds (but keep full buffers intact)
        if not xs:
            return [], [], [], []

        cutoff = xs[-1] - self._window_seconds
        # find first index >= cutoff
        i0 = 0
        for i, x in enumerate(xs):
            if x >= cutoff:
                i0 = i
                break

        return xs[i0:], y0[i0:], y1[i0:], y2[i0:]

    def _recompute_window_maxes(self) -> None:
        xs, y0, y1, y2 = self._get_active_series()
        if not xs:
            self._max_ch0 = self._max_ch1 = self._max_iadc = None
            return
        self._max_ch0 = max(y0) if y0 else None
        self._max_ch1 = max(y1) if y1 else None
        self._max_iadc = max(y2) if y2 else None

    def _update_labels(self) -> None:
        def ff(v):
            return "—" if v is None else f"{v:.3f}"

        def fi(v):
            return "—" if v is None else f"{int(v)}"

        ws = int(self._window_seconds)
        mode = "F" if self._filter_enabled else "R"  # filtered / raw

        self.max_ch0_lbl.setText(f"Max Ch0 ({ws}s) [{mode}]: {ff(self._max_ch0)}")
        self.max_ch1_lbl.setText(f"Max Ch1 ({ws}s) [{mode}]: {ff(self._max_ch1)}")
        self.max_iadc_lbl.setText(f"Max IADC ({ws}s) [{mode}]: {fi(self._max_iadc)}")

        # current (last sample) in active mode
        if not self._xs:
            self.cur_ch0_lbl.setText("Cur Ch0: —")
            self.cur_ch1_lbl.setText("Cur Ch1: —")
            self.cur_iadc_lbl.setText("Cur IADC: —")
            return

        if self._filter_enabled:
            c0 = self._flt_ch0[-1] if self._flt_ch0 else None
            c1 = self._flt_ch1[-1] if self._flt_ch1 else None
            ci = self._flt_iadc[-1] if self._flt_iadc else None
        else:
            c0 = self._raw_ch0[-1] if self._raw_ch0 else None
            c1 = self._raw_ch1[-1] if self._raw_ch1 else None
            ci = self._raw_iadc[-1] if self._raw_iadc else None

        self.cur_ch0_lbl.setText(f"Cur Ch0 [{mode}]: {ff(c0)}")
        self.cur_ch1_lbl.setText(f"Cur Ch1 [{mode}]: {ff(c1)}")
        self.cur_iadc_lbl.setText(f"Cur IADC [{mode}]: {fi(ci)}")

    def _redraw(self) -> None:
        xs, y0, y1, y2 = self._get_active_series()

        self.c0.setData(xs, y0)
        self.c1.setData(xs, y1)
        self.c2.setData(xs, y2)

        if xs:
            xmin = xs[-1] - self._window_seconds
            xmax = xs[-1]
            for p in (self.p0, self.p1, self.p2):
                p.setXRange(xmin, xmax, padding=0)

    # -------------------------------------------------
    # Data entry
    # -------------------------------------------------
    @QtCore.pyqtSlot(float, float, float, int)
    def on_sample(self, t_seconds: float, ch0: float, ch1: float, internal_adc: int) -> None:
        if self._paused:
            return

        # Always store raw
        self._xs.append(float(t_seconds))
        self._raw_ch0.append(float(ch0))
        self._raw_ch1.append(float(ch1))
        self._raw_iadc.append(int(internal_adc))

        # Always compute & store filtered (so toggling shows history too)
        a = self._ema_alpha
        if self._ema_ch0 is None:
            self._ema_ch0 = float(ch0)
            self._ema_ch1 = float(ch1)
            self._ema_iadc = float(internal_adc)
        else:
            self._ema_ch0 = (1.0 - a) * self._ema_ch0 + a * float(ch0)
            self._ema_ch1 = (1.0 - a) * self._ema_ch1 + a * float(ch1)
            self._ema_iadc = (1.0 - a) * self._ema_iadc + a * float(internal_adc)

        self._flt_ch0.append(float(self._ema_ch0))
        self._flt_ch1.append(float(self._ema_ch1))
        self._flt_iadc.append(int(round(self._ema_iadc)))

        self._recompute_window_maxes()
        self._update_labels()
        self._redraw()
