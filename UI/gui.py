# gui.py
from __future__ import annotations

from collections import deque
from typing import Optional, Callable

import pyqtgraph as pg
from PyQt6 import QtCore, QtWidgets


class MainWindow(QtWidgets.QMainWindow):
    def __init__(
            self,
            *,
            history: int = 5000,
            initial_window_seconds: float = 10.0,
            # Pass a function that sends commands, e.g. radio.send_command
            send_command: Optional[Callable[[str, bool], object]] = None,
            parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Serial Telemetry Viewer")

        self._paused = False
        self._history = history
        self._window_seconds = float(initial_window_seconds)

        # Hook for sending commands (Radio.send_command)
        self._send_command = send_command

        # Data buffers
        self._xs = deque(maxlen=history)
        self._ch0 = deque(maxlen=history)   # float
        self._ch1 = deque(maxlen=history)   # float
        self._iadc = deque(maxlen=history)  # int

        self._max_ch0: Optional[float] = None
        self._max_ch1: Optional[float] = None
        self._max_iadc: Optional[float] = None

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # --------------------
        # Top: max values
        # --------------------
        top = QtWidgets.QHBoxLayout()
        layout.addLayout(top)

        self.max_ch0_lbl = QtWidgets.QLabel()
        self.max_ch1_lbl = QtWidgets.QLabel()
        self.max_iadc_lbl = QtWidgets.QLabel()

        for w in (self.max_ch0_lbl, self.max_ch1_lbl, self.max_iadc_lbl):
            w.setMinimumWidth(260)
            top.addWidget(w)

        top.addStretch(1)

        # --------------------
        # Controls
        # --------------------
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

        # Window size slider
        controls.addWidget(QtWidgets.QLabel("Window (s):"))

        self.window_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.window_slider.setRange(5, 120)  # 5s → 2 minutes
        self.window_slider.setValue(int(self._window_seconds))
        self.window_slider.setFixedWidth(200)
        self.window_slider.valueChanged.connect(self._on_window_changed)
        controls.addWidget(self.window_slider)

        self.window_value_lbl = QtWidgets.QLabel(f"{int(self._window_seconds)} s")
        self.window_value_lbl.setMinimumWidth(50)
        controls.addWidget(self.window_value_lbl)

        controls.addStretch(1)

        # --------------------
        # Commands row
        # --------------------
        cmd_row = QtWidgets.QHBoxLayout()
        layout.addLayout(cmd_row)

        cmd_row.addWidget(QtWidgets.QLabel("Commands:"))

        # Helper to create ON/OFF button pairs
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

        # --------------------
        # Plots
        # --------------------
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

        self._update_max_labels()

    # --------------------
    # Command sending
    # --------------------
    def _do_send_command(self, cmd: str, on: bool) -> None:
        if self._send_command is None:
            self.cmd_status_lbl.setText("ACK: no radio hooked up")
            return

        try:
            # fire-and-forget; ACK will arrive later via on_status()
            self._send_command(cmd, on)
            self.cmd_status_lbl.setText(f"Sent: {cmd} {'ON' if on else 'OFF'} (waiting...)")
        except Exception as e:
            self.cmd_status_lbl.setText(f"ACK: error ({e})")
    @QtCore.pyqtSlot(str)
    def on_status(self, msg: str) -> None:
        # Route ACK messages to the ACK label, everything else to window status/title
        if msg.startswith("ACK:"):
            self.cmd_status_lbl.setText(msg)
        else:
            self.setWindowTitle(f"Serial Telemetry Viewer — {msg}")
    # --------------------
    # UI callbacks
    # --------------------
    def _on_pause_toggled(self, checked: bool) -> None:
        self._paused = checked
        self.pause_btn.setText("Resume" if checked else "Pause")

    def _on_window_changed(self, value: int) -> None:
        self._window_seconds = float(value)
        self.window_value_lbl.setText(f"{value} s")
        self._trim_time_window()
        self._recompute_window_maxes()
        self._update_max_labels()
        self._redraw()

    def _clear(self) -> None:
        self._xs.clear()
        self._ch0.clear()
        self._ch1.clear()
        self._iadc.clear()
        self._max_ch0 = self._max_ch1 = self._max_iadc = None
        self._update_max_labels()
        self._redraw()

    # --------------------
    # Window logic
    # --------------------
    def _trim_time_window(self) -> None:
        if not self._xs:
            return
        cutoff = self._xs[-1] - self._window_seconds
        while self._xs and self._xs[0] < cutoff:
            self._xs.popleft()
            self._ch0.popleft()
            self._ch1.popleft()
            self._iadc.popleft()

    def _recompute_window_maxes(self) -> None:
        if not self._xs:
            self._max_ch0 = self._max_ch1 = self._max_iadc = None
            return
        self._max_ch0 = max(self._ch0) if self._ch0 else None
        self._max_ch1 = max(self._ch1) if self._ch1 else None
        self._max_iadc = max(self._iadc) if self._iadc else None

    def _update_max_labels(self) -> None:
        def fmt_f(v: Optional[float]) -> str:
            return "—" if v is None else f"{v:.3f}"

        def fmt_i(v: Optional[float]) -> str:
            return "—" if v is None else f"{int(v)}"

        ws = int(self._window_seconds)
        self.max_ch0_lbl.setText(f"Max Ch0 ({ws}s): {fmt_f(self._max_ch0)}")
        self.max_ch1_lbl.setText(f"Max Ch1 ({ws}s): {fmt_f(self._max_ch1)}")
        self.max_iadc_lbl.setText(f"Max Internal ADC ({ws}s): {fmt_i(self._max_iadc)}")

    def _redraw(self) -> None:
        xs = list(self._xs)
        self.c0.setData(xs, list(self._ch0))
        self.c1.setData(xs, list(self._ch1))
        self.c2.setData(xs, list(self._iadc))

        if xs:
            xmin = xs[-1] - self._window_seconds
            xmax = xs[-1]
            for p in (self.p0, self.p1, self.p2):
                p.setXRange(xmin, xmax, padding=0)

    # --------------------
    # Data entry
    # --------------------
    @QtCore.pyqtSlot(float, float, float, int)
    def on_sample(self, t_seconds: float, ch0: float, ch1: float, internal_adc: int) -> None:
        if self._paused:
            return

        self._xs.append(float(t_seconds))
        self._ch0.append(float(ch0))
        self._ch1.append(float(ch1))
        self._iadc.append(int(internal_adc))

        self._trim_time_window()
        self._recompute_window_maxes()
        self._update_max_labels()
        self._redraw()
