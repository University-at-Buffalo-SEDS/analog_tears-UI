#! /usr/bin/env python3
# main.py
from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
import argparse
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Deque

from PyQt6 import QtCore, QtWidgets

from gui import MainWindow
from radio import Radio
from handlePacket import PacketHandler

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
    ch0_zero_raw: Optional[float]
    ch0_poly2: Optional[tuple[float, float, float]]
    ch1_m: Optional[float]
    ch1_b: Optional[float]
    ch1_poly2: Optional[tuple[float, float, float]]
    ch1_zero_raw: Optional[float]


def load_calibration(path: Path) -> Optional[Calibration]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        ch0 = data.get("ch0")
        ch1 = data.get("ch1")
        ch0_fit = data.get("ch0_fit", {})
        ch1_fit = data.get("ch1_fit", {})
        ch0_poly2 = None
        if isinstance(ch0_fit, dict) and ch0_fit.get("type") == "poly2":
            try:
                ch0_poly2 = (
                    float(ch0_fit["a"]),
                    float(ch0_fit["b"]),
                    float(ch0_fit["c"]),
                )
            except Exception:
                ch0_poly2 = None
        ch1_poly2 = None
        if isinstance(ch1_fit, dict) and ch1_fit.get("type") == "poly2":
            try:
                ch1_poly2 = (
                    float(ch1_fit["a"]),
                    float(ch1_fit["b"]),
                    float(ch1_fit["c"]),
                )
            except Exception:
                ch1_poly2 = None

        return Calibration(
            ch0_m=float(ch0["m"]) if ch0 and "m" in ch0 else None,
            ch0_b=float(ch0["b"]) if ch0 and "b" in ch0 else None,
            ch0_zero_raw=float(data.get("ch0_zero_raw")) if data.get("ch0_zero_raw") is not None else None,
            ch0_poly2=ch0_poly2,
            ch1_m=float(ch1["m"]) if ch1 and "m" in ch1 else None,
            ch1_b=float(ch1["b"]) if ch1 and "b" in ch1 else None,
            ch1_poly2=ch1_poly2,
            ch1_zero_raw=float(data.get("ch1_zero_raw")) if data.get("ch1_zero_raw") is not None else None,
        )
    except Exception:
        return None


class RadioWorker(QtCore.QThread):
    """
    Background serial read loop.
    Emits values to GUI and (optionally) logs to CSV.
    """
    # t_mono, ch0(float), ch1(float), internal_adc(int), battery_voltage(float)
    sample = QtCore.pyqtSignal(float, float, float, int, float)
    raw_sample = QtCore.pyqtSignal(float, float, float)  # t_mono, ch0_raw, ch1_raw
    status = QtCore.pyqtSignal(str)
    data_rate = QtCore.pyqtSignal(float, float)  # bytes_per_s, packets_per_s

    def __init__(
        self,
        *,
        com_port: str,
        calibration_path: Path,
        baudrate: int = 57600,
        sim_rate_hz: float | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._com_port = com_port
        self._baudrate = int(baudrate)
        self._stop = False

        radio_kwargs = {}
        if sim_rate_hz is not None:
            radio_kwargs["sim_rate_hz"] = float(sim_rate_hz)
        self.radio = Radio(port=self._com_port, baudrate=self._baudrate, **radio_kwargs)
        self._calibration_path = calibration_path
        self._calibration: Optional[Calibration] = load_calibration(self._calibration_path)

        # --- logging state ---
        self._logging_enabled = False
        self._logging_paused = False
        self._csv_path: Optional[Path] = None
        self._csv_file = None
        self._csv_writer: Optional[csv.writer] = None
        self._raw_stream_enabled = False
        self._display_raw_values = False
        self._ui_emit_in_flight = False
        self._reader_stop = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._event_queue: deque = deque(maxlen=5000)
        self._event_lock = threading.Lock()
        self._low_latency_display_mode = True
        self._latest_display_packet: Optional[tuple[object, float]] = None
        self._rate_lock = threading.Lock()
        self._ingress_packets = 0
        self._ingress_bytes = 0

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
        self._logging_paused = False
        self.status.emit(f"Saving: ON → {path.resolve()}")

    @QtCore.pyqtSlot()
    def stop_logging(self) -> None:
        self._logging_enabled = False
        self._logging_paused = False
        self._close_csv()
        # Return to low-latency display mode after save sessions end.
        self._csv_path = None
        self.status.emit("Saving: OFF")

    @QtCore.pyqtSlot(bool)
    def set_logging_paused(self, paused: bool) -> None:
        self._logging_paused = bool(paused)
        if self._logging_enabled:
            self.status.emit("Saving: PAUSED" if self._logging_paused else "Saving: RUNNING")

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

        # Snapshot writes should not force persistent non-display-only mode.
        if not self._logging_enabled:
            self._close_csv()
            self._csv_path = None

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

    @QtCore.pyqtSlot(bool)
    def set_raw_stream_enabled(self, enabled: bool) -> None:
        self._raw_stream_enabled = bool(enabled)

    @QtCore.pyqtSlot(bool)
    def set_display_raw_values(self, enabled: bool) -> None:
        self._display_raw_values = bool(enabled)

    @QtCore.pyqtSlot()
    def on_ui_sample_consumed(self) -> None:
        self._ui_emit_in_flight = False

    def _enqueue_event(self, ev, t_read_mono: float) -> None:
        with self._event_lock:
            if len(self._event_queue) >= self._event_queue.maxlen:
                self._event_queue.popleft()
            self._event_queue.append((ev, t_read_mono))

    def _dequeue_event(self):
        with self._event_lock:
            if not self._event_queue:
                return None
            return self._event_queue.popleft()

    def _set_latest_display_packet(self, ev, t_read_mono: float) -> None:
        with self._event_lock:
            self._latest_display_packet = (ev, t_read_mono)

    def _take_latest_display_packet(self):
        with self._event_lock:
            item = self._latest_display_packet
            self._latest_display_packet = None
            return item

    def _add_ingress_telemetry(self, packet_count: int, byte_count: int) -> None:
        with self._rate_lock:
            self._ingress_packets += int(packet_count)
            self._ingress_bytes += int(byte_count)

    def _take_ingress_totals(self) -> tuple[int, int]:
        with self._rate_lock:
            return self._ingress_packets, self._ingress_bytes

    def _reader_loop(self) -> None:
        while not self._reader_stop.is_set():
            if self._low_latency_display_mode:
                # Keep latency low, but avoid over-aggressive flushing that can starve decoding.
                self.radio.drop_os_backlog_if_needed(8192)
                self.radio.prune_rx_buffer_to_latest(4096)
            try:
                ev = self.radio.poll_event()
            except Exception:
                time.sleep(0.001)
                continue
            if ev is None:
                # Yield CPU to keep UI responsive during high-rate test/sim loops.
                time.sleep(0.0001 if self.radio.simulate else 0.0005)
                continue
            t_now = time.monotonic()
            if not isinstance(ev, tuple):
                self._add_ingress_telemetry(1, PacketHandler.PACKET_SIZE)
                # Always keep newest telemetry packet available for low-latency UI display.
                self._set_latest_display_packet(ev, t_now)
                if self._low_latency_display_mode:
                    continue
            self._enqueue_event(ev, t_now)

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
                "Ch0_kg",
                "Ch1_kg",
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
            "",
            "",
        ]
        self._csv_writer.writerow(row)
        try:
            self._csv_file.flush()
        except Exception:
            pass

    def _calibrate_channels(self, ch0_raw: float, ch1_raw: float) -> tuple[float, float]:
        if self._calibration is None:
            ch0_kg = (5.0 / 12.0) * ((ch0_raw / 5.831609e-05) - 9.2) + (5.0 / 6.0)
            ch1_kg = (10.0 * ((ch1_raw / 2.929497e-06) - (10 - 1.8) + 3.8)) - 14
            return float(ch0_kg), float(ch1_kg)

        if self._calibration.ch0_poly2 is not None:
            a, b, c = self._calibration.ch0_poly2
            if self._calibration.ch0_zero_raw is not None:
                x = ch0_raw - self._calibration.ch0_zero_raw
                ch0_kg = (a * x * x) + (b * x) + c
            else:
                ch0_kg = (a * ch0_raw * ch0_raw) + (b * ch0_raw) + c
        elif self._calibration.ch0_m is not None and self._calibration.ch0_b is not None:
            if self._calibration.ch0_zero_raw is not None:
                ch0_kg = self._calibration.ch0_m * (ch0_raw - self._calibration.ch0_zero_raw)
            else:
                ch0_kg = self._calibration.ch0_m * ch0_raw + self._calibration.ch0_b
        else:
            ch0_kg = (5.0 / 12.0) * ((ch0_raw / 5.831609e-05) - 9.2) + (5.0 / 6.0)

        if self._calibration.ch1_poly2 is not None:
            a, b, c = self._calibration.ch1_poly2
            if self._calibration.ch1_zero_raw is not None:
                x = ch1_raw - self._calibration.ch1_zero_raw
                ch1_kg = (a * x * x) + (b * x) + c
            else:
                ch1_kg = (a * ch1_raw * ch1_raw) + (b * ch1_raw) + c
        elif self._calibration.ch1_m is not None and self._calibration.ch1_b is not None:
            if self._calibration.ch1_zero_raw is not None:
                ch1_kg = self._calibration.ch1_m * (ch1_raw - self._calibration.ch1_zero_raw)
            else:
                ch1_kg = self._calibration.ch1_m * ch1_raw + self._calibration.ch1_b
        else:
            ch1_kg = (10.0 * ((ch1_raw / 2.929497e-06) - (10 - 1.8) + 3.8)) - 14

        return float(ch0_kg), float(ch1_kg)

    # ----------------------------
    # Main worker loop
    # ----------------------------
    def run(self) -> None:
        last_status = ""
        last_ui_emit = 0.0
        last_raw_emit = 0.0
        ui_emit_period = 1.0 / 120.0
        raw_emit_period = 1.0 / 60.0
        rate_period = 0.25
        last_rate_emit = time.monotonic()
        prev_ingress_packets = 0
        prev_ingress_bytes = 0
        crc_verify_enabled = True
        seen_igniter_command = False
        turn_p_off = False
        p_start = time.monotonic()

        self._reader_stop.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, name="radio-reader", daemon=True)
        self._reader_thread.start()

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
                display_only = (not self._logging_enabled) and (self._csv_path is None) and (not self._raw_stream_enabled)
                self._low_latency_display_mode = display_only

                want_crc = self._logging_enabled or (self._csv_path is not None) or self._raw_stream_enabled
                if want_crc != crc_verify_enabled:
                    self.radio.set_crc_verification(want_crc)
                    crc_verify_enabled = want_crc

                item = self._dequeue_event()
                if item is None and display_only:
                    item = self._take_latest_display_packet()
                if item is None:
                    time.sleep(0.0005)
                    continue
                ev, ev_t_mono = item

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
                packet_t_mono = ev_t_mono
                if display_only:
                    # Under high-rate streaming, keep only the newest packet for UI.
                    # This prevents unbounded "catch-up" latency growth.
                    while True:
                        nxt_item = self._dequeue_event()
                        if nxt_item is None:
                            break
                        nxt, nxt_t_mono = nxt_item
                        if isinstance(nxt, tuple):
                            cmd, state = nxt
                            self.status.emit(f"ACK: {cmd} {'ON' if state else 'OFF'}")
                            if frontend_disable_pilot:
                                if cmd == "I":
                                    seen_igniter_command = True
                                if seen_igniter_command and cmd == "P" and state is True:
                                    turn_p_off = True
                                    p_start = time.monotonic()
                            continue
                        packet = nxt
                        packet_t_mono = nxt_t_mono

                t_mono = packet_t_mono
                ch0_raw = float(packet.channel0)
                ch1_raw = float(packet.channel1)

                # CSV work is expensive; skip it unless a save session has been started.
                if self._logging_enabled or self._csv_path is not None:
                    rx_timestamp = datetime.now().isoformat(timespec="milliseconds")
                    try:
                        # expects packet.to_csv_row(rx_timestamp) returns:
                        # [rx_timestamp, header, seq, timestamp, ch0, ch1, internal_adc, battery_voltage, crc]
                        row = packet.to_csv_row(rx_timestamp)
                        ch0_kg_csv, ch1_kg_csv = self._calibrate_channels(ch0_raw, ch1_raw)
                        row.extend([ch0_kg_csv, ch1_kg_csv])
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
                    if self._logging_enabled and (not self._logging_paused):
                        try:
                            self._open_csv_if_needed()
                            self._write_row_dedup(key, row)
                        except Exception as e:
                            self.status.emit(f"CSV write error: {e}")

                # GUI update
                try:
                    ui_packet = packet
                    ui_t_mono = t_mono
                    latest_ui = self._take_latest_display_packet()
                    if latest_ui is not None and (not isinstance(latest_ui[0], tuple)):
                        ui_packet, ui_t_mono = latest_ui

                    ch0_raw_ui = float(ui_packet.channel0)
                    ch1_raw_ui = float(ui_packet.channel1)

                    if self._raw_stream_enabled and (ui_t_mono - last_raw_emit) >= raw_emit_period:
                        self.raw_sample.emit(float(ui_t_mono), ch0_raw_ui, ch1_raw_ui)
                        last_raw_emit = ui_t_mono

                    if (ui_t_mono - last_ui_emit) >= ui_emit_period:
                        if self._display_raw_values:
                            ch0_out = float(ch0_raw_ui)
                            ch1_out = float(ch1_raw_ui)
                        else:
                            ch0_out, ch1_out = self._calibrate_channels(ch0_raw_ui, ch1_raw_ui)
                        iadc = (ui_packet.internal_adc / 1.78)
                        batt_v = float(ui_packet.battery_voltage)
                        self.sample.emit(
                            float(ui_t_mono),
                            float(ch0_out),
                            float(ch1_out),
                            int(iadc),
                            batt_v,
                        )
                        last_ui_emit = ui_t_mono
                except Exception as e:
                    self.status.emit(f"Emit error: {e}")

                now_rate = time.monotonic()
                dt_rate = now_rate - last_rate_emit
                if dt_rate >= rate_period:
                    ingress_packets, ingress_bytes = self._take_ingress_totals()
                    pkt_rate = float(ingress_packets - prev_ingress_packets) / dt_rate
                    byte_rate = float(ingress_bytes - prev_ingress_bytes) / dt_rate
                    prev_ingress_packets = ingress_packets
                    prev_ingress_bytes = ingress_bytes
                    self.data_rate.emit(byte_rate, pkt_rate)
                    last_rate_emit = now_rate

        finally:
            self._reader_stop.set()
            if self._reader_thread is not None:
                try:
                    self._reader_thread.join(timeout=1.0)
                except Exception:
                    pass
                self._reader_thread = None
            try:
                self.radio.close()
            except Exception:
                pass
            self._close_csv()
            self.status.emit("Radio closed.")


def main():
    default_com_port = os.environ.get("ANALOG_TEARS_PORT", "/dev/tty.usbserial-BG00HPF3")
    default_baudrate = int(os.environ.get("ANALOG_TEARS_BAUD", "57600"))
    default_csv_filename = str(Path(__file__).parent / "Data" / "HTF_Data.csv")
    default_calibration_path = str(Path(__file__).with_name("loadcell_calibration.json"))

    parser = argparse.ArgumentParser(description="Analog Tears UI")
    parser.add_argument("--port", "--com-port", dest="port", default=default_com_port, help="Serial COM port/device")
    parser.add_argument("port_positional", nargs="?", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--baud", type=int, default=default_baudrate, help="Serial baud rate")
    parser.add_argument("--csv", default=default_csv_filename, help="Default CSV output filename")
    parser.add_argument(
        "--calibration",
        default=default_calibration_path,
        help="Path to load cell calibration JSON",
    )
    parser.add_argument(
        "--sim-bytes-per-sec",
        type=float,
        default=0.0,
        help="Dummy mode target throughput in bytes/s (e.g. 120000 for 120 kB/s)",
    )
    parser.add_argument(
        "--sim-rate-hz",
        type=float,
        default=0.0,
        help="Dummy mode packet rate in packets/s (overrides --sim-bytes-per-sec)",
    )
    # Keep unknown CLI args available for Qt instead of failing argparse.
    args, qt_args = parser.parse_known_args()

    com_port = str(args.port_positional or args.port).strip()
    if com_port.lower() in {"test", "testing"}:
        com_port = "dummy"
    baudrate = int(args.baud)
    default_csv_filename = str(args.csv)
    calibration_path = Path(str(args.calibration))
    sim_rate_hz: float | None = None
    if com_port.lower() in {"dummy", "sim", "simulation"}:
        if float(args.sim_rate_hz) > 0.0:
            sim_rate_hz = float(args.sim_rate_hz)
        elif float(args.sim_bytes_per_sec) > 0.0:
            sim_rate_hz = float(args.sim_bytes_per_sec) / float(PacketHandler.PACKET_SIZE)
        if sim_rate_hz is not None:
            print(
                f"[Test] Dummy source configured for ~{sim_rate_hz:.1f} pkt/s "
                f"(~{sim_rate_hz * PacketHandler.PACKET_SIZE:.0f} B/s)"
            )

    app = QtWidgets.QApplication([sys.argv[0], *qt_args])

    worker = RadioWorker(
        com_port=com_port,
        calibration_path=calibration_path,
        baudrate=baudrate,
        sim_rate_hz=sim_rate_hz,
    )

    # Keep enough samples for long time windows at high refresh rates.
    win = MainWindow(history=120000, send_command=worker.send_command)
    win.resize(1100, 860)
    win.showMaximized()

    # Telemetry -> GUI
    worker.sample.connect(win.on_sample)
    worker.raw_sample.connect(win.on_raw_sample)
    worker.data_rate.connect(win.on_data_rate)

    # GUI -> Worker logging controls (queued across threads)
    win.start_saving.connect(worker.start_logging, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.stop_saving.connect(worker.stop_logging, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.pause_saving.connect(worker.set_logging_paused, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.save_last_10s.connect(worker.save_last_10s, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.calibration_saved.connect(worker.reload_calibration, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.raw_stream_enabled.connect(worker.set_raw_stream_enabled, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.display_raw_values.connect(worker.set_display_raw_values, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.sample_consumed.connect(worker.on_ui_sample_consumed, type=QtCore.Qt.ConnectionType.QueuedConnection)

    # initial filename in GUI
    win.set_filename(default_csv_filename)
    win.set_calibration_filename(str(calibration_path))

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
