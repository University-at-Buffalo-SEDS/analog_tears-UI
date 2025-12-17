# main.py
from __future__ import annotations

import csv
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Deque, Tuple

from PyQt6 import QtCore, QtWidgets

from gui import MainWindow
from radio import Radio


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


class RadioWorker(QtCore.QThread):
    """
    Background serial read loop.
    Emits values to GUI and (optionally) logs to CSV.
    """
    # t_seconds, ch0(float), ch1(float), internal_adc(int)
    sample = QtCore.pyqtSignal(float, float, float, int)
    status = QtCore.pyqtSignal(str)

    def __init__(self, *, com_port: str, parent=None):
        super().__init__(parent)
        self._com_port = com_port
        self._stop = False

        self.radio = Radio(port=self._com_port)

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
                "CRC",
            ])
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

    # ----------------------------
    # Main worker loop
    # ----------------------------
    def run(self) -> None:
        last_status = ""
        t0 = time.monotonic()

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
                    continue

                # --- telemetry packet ---
                packet = ev
                rx_timestamp = datetime.now().isoformat(timespec="milliseconds")
                t_mono = time.monotonic()

                # Build CSV row + dedupe key
                try:
                    # expects packet.to_csv_row(rx_timestamp) returns:
                    # [rx_timestamp, header, seq, timestamp, ch0, ch1, internal_adc, crc]
                    row = packet.to_csv_row(rx_timestamp)
                    key = CsvKey(
                        header=int(row[1]),
                        seq=int(row[2]),
                        timestamp=int(row[3]),
                        crc=int(row[7]),
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
                    ch0_kg = (packet.channel0 / 5.831609e-05) - (-21.2) - 41.3
                    ch1_kg = (packet.channel1 / 2.929497e-06) - (10 - 1.8)
                    iadc = (packet.internal_adc / 1.78)
                    self.sample.emit(
                        float(t),
                        float(ch0_kg),
                        float(ch1_kg),
                        int(iadc),
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
    DEFAULT_CSV_FILENAME = "serial_data.csv"

    app = QtWidgets.QApplication(sys.argv)

    worker = RadioWorker(com_port=COM_PORT)

    win = MainWindow(history=2000, send_command=worker.send_command)
    win.resize(1100, 860)
    win.showMaximized()

    # Telemetry -> GUI
    worker.sample.connect(win.on_sample)

    # GUI -> Worker logging controls (queued across threads)
    win.start_saving.connect(worker.start_logging, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.stop_saving.connect(worker.stop_logging, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.save_last_10s.connect(worker.save_last_10s, type=QtCore.Qt.ConnectionType.QueuedConnection)

    # initial filename in GUI
    win.set_filename(DEFAULT_CSV_FILENAME)

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
