# gui.py
from __future__ import annotations

from collections import deque
from typing import Optional

import pyqtgraph as pg
from PyQt6 import QtCore, QtWidgets


class MainWindow(QtWidgets.QMainWindow):
    def __init__(
            self,
            *,
            history: int = 5000,
            initial_window_seconds: float = 10.0,
            parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Serial Telemetry Viewer")

        self._paused = False
        self._history = history
        self._window_seconds = float(initial_window_seconds)

        # Data buffers
        self._xs = deque(maxlen=history)
        self._ch0 = deque(maxlen=history)
        self._ch1 = deque(maxlen=history)
        self._iadc = deque(maxlen=history)

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
        self._max_ch0 = max(self._ch0)
        self._max_ch1 = max(self._ch1)
        self._max_iadc = max(self._iadc)

    def _update_max_labels(self) -> None:
        def fmt(v: Optional[float]) -> str:
            return "—" if v is None else f"{v:.0f}"

        ws = int(self._window_seconds)
        self.max_ch0_lbl.setText(f"Max Ch0 ({ws}s): {fmt(self._max_ch0)}")
        self.max_ch1_lbl.setText(f"Max Ch1 ({ws}s): {fmt(self._max_ch1)}")
        self.max_iadc_lbl.setText(f"Max Internal ADC ({ws}s): {fmt(self._max_iadc)}")

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

    @QtCore.pyqtSlot(float, int, int, int)
    def on_sample(self, t_seconds: float, ch0: int, ch1: int, internal_adc: int) -> None:
        if self._paused:
            return

        self._xs.append(t_seconds)
        self._ch0.append(ch0)
        self._ch1.append(ch1)
        self._iadc.append(internal_adc)

        self._trim_time_window()
        self._recompute_window_maxes()
        self._update_max_labels()
        self._redraw()
