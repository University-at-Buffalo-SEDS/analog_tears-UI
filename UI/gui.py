# gui.py
from __future__ import annotations

from collections import deque
from typing import Optional, Callable, Tuple

import pyqtgraph as pg
from PyQt6 import QtCore, QtWidgets


IGNITER_PASSWORD = "67"


class MainWindow(QtWidgets.QMainWindow):
    # logging control signals (GUI -> worker)
    start_saving = QtCore.pyqtSignal(str)     # filename
    stop_saving = QtCore.pyqtSignal()
    pause_saving = QtCore.pyqtSignal(bool)    # True=paused, False=running
    save_last_10s = QtCore.pyqtSignal(str)    # filename

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

        # UI state
        self._paused = False                 # display pause
        self._saving_active = False          # saving on/off
        self._saving_paused = False          # saving paused/unpaused

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
        # Saving / Logging row
        # -------------------------------------------------
        save_row = QtWidgets.QHBoxLayout()
        layout.addLayout(save_row)

        save_row.addWidget(QtWidgets.QLabel("CSV file:"))
        self.filename_edit = QtWidgets.QLineEdit()
        self.filename_edit.setPlaceholderText("serial_data.csv")
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
    # Public helper (main.py convenience)
    # -------------------------------------------------
    def set_filename(self, filename: str) -> None:
        self.filename_edit.setText(filename)

    def _get_filename(self) -> str:
        name = self.filename_edit.text().strip()
        return name if name else "serial_data.csv"

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
        if pw.strip() != IGNITER_PASSWORD:
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

        if not xs:
            return [], [], [], []

        cutoff = xs[-1] - self._window_seconds
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
        mode = "F" if self._filter_enabled else "R"

        self.max_ch0_lbl.setText(f"Max Ch0 ({ws}s) [{mode}]: {ff(self._max_ch0)}")
        self.max_ch1_lbl.setText(f"Max Ch1 ({ws}s) [{mode}]: {ff(self._max_ch1)}")
        self.max_iadc_lbl.setText(f"Max IADC ({ws}s) [{mode}]: {fi(self._max_iadc)}")

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

        self._xs.append(float(t_seconds))
        self._raw_ch0.append(float(ch0))
        self._raw_ch1.append(float(ch1))
        self._raw_iadc.append(int(internal_adc))

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
