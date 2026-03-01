#! /usr/bin/env python3
# main.py
from __future__ import annotations

import csv
import json
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Deque

from PyQt6 import QtCore, QtWidgets

from gui import MainWindow
from radio import Radio

frontend_disable_pilot = False


@dataclass(frozen=True)
class CsvKey:
    header: int
    seq: int
    timestamp: int
    crc: int


@dataclass
class BufferedRow:
    t_mono: float
    key: CsvKey
    row: list


@dataclass(frozen=True)
class Calibration:
    ch0_m: Optional[float]
    ch0_b: Optional[float]
    ch1_m: Optional[float]
    ch1_b: Optional[float]


def load_calibration(path: Path) -> Optional[Calibration]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        ch0 = data.get("ch0")
        ch1 = data.get("ch1")
        return Calibration(
            ch0_m=float(ch0["m"]) if ch0 and "m" in ch0 else None,
            ch0_b=float(ch0["b"]) if ch0 and "b" in ch0 else None,
            ch1_m=float(ch1["m"]) if ch1 and "m" in ch1 else None,
            ch1_b=float(ch1["b"]) if ch1 and "b" in ch1 else None,
        )
    except Exception:
        return None


class RadioWorker(QtCore.QThread):
    """
    Background serial read loop.
    Emits values to GUI and (optionally) logs to CSV.
    """
    # t_seconds, ch0(float), ch1(float), internal_adc(int), battery_voltage(float)
    sample = QtCore.pyqtSignal(float, float, float, int, float)
    raw_sample = QtCore.pyqtSignal(float, float)  # ch0_raw, ch1_raw
    status = QtCore.pyqtSignal(str)

    def __init__(self, *, com_port: str, calibration_path: Path, parent=None):
        super().__init__(parent)
        self._com_port = com_port
        self._stop = False

        self.radio = Radio(port=self._com_port)
        self._calibration_path = calibration_path
        self._calibration: Optional[Calibration] = load_calibration(self._calibration_path)

        # --- logging state ---
        self._logging_enabled = False
        self._csv_path: Optional[Path] = None
        self._csv_file = None
        self._csv_writer: Optional[csv.writer] = None

        # recent telemetry buffer (for “save last 10s”)
        self._recent: Deque[BufferedRow] = deque(maxlen=20000)  # plenty for high-rate

        # dedupe for current file
        self._written_keys: Deque[CsvKey] = deque(maxlen=100000)  # bounds memory
        self._written_set: set[CsvKey] = set()

    def stop(self) -> None:
        self._stop = True

    # ----------------------------
    # Thread-safe UI -> worker API
    # ----------------------------
    @QtCore.pyqtSlot(str)
    def start_logging(self, filename: str) -> None:
        path = Path(filename)
        self._csv_path = path
        self._open_csv_if_needed()
        self._logging_enabled = True
        self.status.emit(f"Saving: ON → {path.resolve()}")

    @QtCore.pyqtSlot()
    def stop_logging(self) -> None:
        self._logging_enabled = False
        self._close_csv()
        self.status.emit("Saving: OFF")

    @QtCore.pyqtSlot(str)
    def save_last_10s(self, filename: str) -> None:
        """
        Writes last ~10 seconds of buffered telemetry to CSV,
        without duplicating rows already written to the same file.
        """
        path = Path(filename)
        # Switch target file for snapshot (if different)
        if self._csv_path != path:
            # close old file; dedupe resets per-file (safer + expected)
            self._logging_enabled = False
            self._close_csv(reset_dedupe=True)
            self._csv_path = path

        self._open_csv_if_needed()

        now = time.monotonic()
        cutoff = now - 10.0
        rows = [br for br in self._recent if br.t_mono >= cutoff]

        wrote = 0
        for br in rows:
            if self._write_row_dedup(br.key, br.row):
                wrote += 1

        self.status.emit(f"Saved last 10s: wrote {wrote} row(s) → {path.resolve()}")

    def send_command(self, command: str, on: bool) -> None:
        self.radio.send_command(command, on)

    @QtCore.pyqtSlot()
    def reload_calibration(self) -> None:
        self._calibration = load_calibration(self._calibration_path)
        if self._calibration is None:
            self.status.emit("Calibration: not loaded (using defaults)")
        else:
            self.status.emit("Calibration: loaded")
            if self._logging_enabled:
                try:
                    self._open_csv_if_needed()
                    self._write_calibration_row()
                except Exception as e:
                    self.status.emit(f"Calibration CSV write error: {e}")

    # ----------------------------
    # CSV helpers (worker thread)
    # ----------------------------
    def _open_csv_if_needed(self) -> None:
        if self._csv_path is None:
            return
        if self._csv_file is not None and self._csv_writer is not None:
            return

        self._csv_path.parent.mkdir(parents=True, exist_ok=True)

        new_file = not self._csv_path.exists() or self._csv_path.stat().st_size == 0
        self._csv_file = open(self._csv_path, "a", newline="")
        self._csv_writer = csv.writer(self._csv_file)

        if new_file:
            self._csv_writer.writerow([
                "Rx_Timestamp",
                "Header",
                "Seq",
                "Timestamp",
                "Ch0",
                "Ch1",
                "Internal ADC",
                "Battery Voltage",
                "CRC",
            ])
            self._write_calibration_row()
            self._csv_file.flush()

        # If we opened a new/different file, we should ensure dedupe structures match.
        # (We reset dedupe when we close with reset_dedupe=True.)
        self.status.emit(f"Logging to {self._csv_path.resolve()}")

    def _close_csv(self, *, reset_dedupe: bool = False) -> None:
        try:
            if self._csv_file is not None:
                try:
                    self._csv_file.flush()
                except Exception:
                    pass
                self._csv_file.close()
        finally:
            self._csv_file = None
            self._csv_writer = None

        if reset_dedupe:
            self._written_keys.clear()
            self._written_set.clear()

    def _write_row_dedup(self, key: CsvKey, row: list) -> bool:
        if self._csv_writer is None:
            return False

        if key in self._written_set:
            return False

        # write
        self._csv_writer.writerow(row)
        try:
            self._csv_file.flush()
        except Exception:
            pass

        # record
        self._written_keys.append(key)
        self._written_set.add(key)

        # prune set alongside deque maxlen behavior
        while len(self._written_set) > len(self._written_keys):
            # should never happen, but keep safe
            self._written_set = set(self._written_keys)

        return True

    def _format_calib_value(self, m: Optional[float], b: Optional[float]) -> str:
        if m is None or b is None:
            return ""
        return f"m={m:.10g},b={b:.10g}"

    def _write_calibration_row(self) -> None:
        if self._csv_writer is None:
            return
        if self._calibration is None:
            return

        ch0 = self._format_calib_value(self._calibration.ch0_m, self._calibration.ch0_b)
        ch1 = self._format_calib_value(self._calibration.ch1_m, self._calibration.ch1_b)
        if not ch0 and not ch1:
            return

        row = [
            "CALIBRATION",
            "",
            "",
            "",
            ch0,
            ch1,
            "",
            "",
            "",
        ]
        self._csv_writer.writerow(row)
        try:
            self._csv_file.flush()
        except Exception:
            pass

    # ----------------------------
    # Main worker loop
    # ----------------------------
    def run(self) -> None:
        last_status = ""
        t0 = time.monotonic()
        seen_igniter_command = False
        turn_p_off = False
        p_start = time.monotonic()

        try:
            while not self._stop:
                # --- connection management ---
                if not self.radio.is_connected():
                    ok = self.radio.reconnect()
                    new_status = "Reconnected" if ok else "Disconnected (retrying...)"
                    if new_status != last_status:
                        self.status.emit(new_status)
                        last_status = new_status
                    time.sleep(0.25)
                    continue

                # --- unified RX (telemetry + ACKs) ---
                try:
                    ev = self.radio.poll_event()
                except Exception:
                    # poll_event should already mark disconnected
                    continue

                if ev is None:
                    continue

                # --- ACK event ---
                if isinstance(ev, tuple):
                    cmd, state = ev
                    self.status.emit(f"ACK: {cmd} {'ON' if state else 'OFF'}")
                    if frontend_disable_pilot:
                        if cmd == "I":
                            seen_igniter_command = True

                        if seen_igniter_command and cmd == "P" and state == True:
                            turn_p_off = True
                            p_start = time.monotonic()
                    continue
                if frontend_disable_pilot:
                    if turn_p_off and time.monotonic() - p_start > 1.5:
                        self.send_command("P", False)
                        seen_igniter_command = False
                        turn_p_off = False

                # --- telemetry packet ---
                packet = ev
                rx_timestamp = datetime.now().isoformat(timespec="milliseconds")
                t_mono = time.monotonic()

                # Build CSV row + dedupe key
                try:
                    # expects packet.to_csv_row(rx_timestamp) returns:
                    # [rx_timestamp, header, seq, timestamp, ch0, ch1, internal_adc, battery_voltage, crc]
                    row = packet.to_csv_row(rx_timestamp)
                    key = CsvKey(
                        header=int(row[1]),
                        seq=int(row[2]),
                        timestamp=int(row[3]),
                        crc=int(row[8]),
                    )
                except Exception as e:
                    self.status.emit(f"Packet->CSV error: {e}")
                    continue

                # Buffer for snapshots
                self._recent.append(BufferedRow(t_mono=t_mono, key=key, row=row))

                # CSV output (if enabled)
                if self._logging_enabled:
                    try:
                        self._open_csv_if_needed()
                        self._write_row_dedup(key, row)
                    except Exception as e:
                        self.status.emit(f"CSV write error: {e}")

                # GUI update
                try:
                    t = time.monotonic() - t0
                    ch0_raw = float(packet.channel0)
                    ch1_raw = float(packet.channel1)
                    if self._calibration is None:
                        ch0_kg = (5.0/12.0) * ((ch0_raw / 5.831609e-05) - 9.2) + (5.0/6.0)
                        ch1_kg = (10.0 * ((ch1_raw / 2.929497e-06) - (10 - 1.8) + 3.8)) - 14
                    else:
                        if self._calibration.ch0_m is not None and self._calibration.ch0_b is not None:
                            ch0_kg = self._calibration.ch0_m * ch0_raw + self._calibration.ch0_b
                        else:
                            ch0_kg = (5.0/12.0) * ((ch0_raw / 5.831609e-05) - 9.2) + (5.0/6.0)

                        if self._calibration.ch1_m is not None and self._calibration.ch1_b is not None:
                            ch1_kg = self._calibration.ch1_m * ch1_raw + self._calibration.ch1_b
                        else:
                            ch1_kg = (10.0 * ((ch1_raw / 2.929497e-06) - (10 - 1.8) + 3.8)) - 14
                    iadc = (packet.internal_adc / 1.78)
                    batt_v = float(packet.battery_voltage)
                    self.raw_sample.emit(ch0_raw, ch1_raw)
                    self.sample.emit(
                        float(t),
                        float(ch0_kg),
                        float(ch1_kg),
                        int(iadc),
                        batt_v,
                    )
                except Exception as e:
                    self.status.emit(f"Emit error: {e}")

        finally:
            try:
                self.radio.close()
            except Exception:
                pass
            self._close_csv()
            self.status.emit("Radio closed.")


def main():
    COM_PORT = "/dev/tty.usbserial-BG00HPF3"
    DEFAULT_CSV_FILENAME = str(Path(__file__).parent / "Data" / "HTF_Data.csv")
    CALIBRATION_PATH = Path(__file__).with_name("loadcell_calibration.json")

    app = QtWidgets.QApplication(sys.argv)

    worker = RadioWorker(com_port=COM_PORT, calibration_path=CALIBRATION_PATH)

    win = MainWindow(history=2000, send_command=worker.send_command)
    win.resize(1100, 860)
    win.showMaximized()

    # Telemetry -> GUI
    worker.sample.connect(win.on_sample)
    worker.raw_sample.connect(win.on_raw_sample)

    # GUI -> Worker logging controls (queued across threads)
    win.start_saving.connect(worker.start_logging, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.stop_saving.connect(worker.stop_logging, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.save_last_10s.connect(worker.save_last_10s, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.calibration_saved.connect(worker.reload_calibration, type=QtCore.Qt.ConnectionType.QueuedConnection)

    # initial filename in GUI
    win.set_filename(DEFAULT_CSV_FILENAME)
    win.set_calibration_filename(str(CALIBRATION_PATH))

    def handle_status(s: str) -> None:
        if s.startswith("ACK:"):
            win.cmd_status_lbl.setText(s)
        else:
            win.setWindowTitle(f"Serial Telemetry Viewer — {s}")

    worker.status.connect(handle_status)
    app.aboutToQuit.connect(lambda: (worker.stop(), worker.wait(2000)))

    worker.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
