#! /usr/bin/env python3
# main.py
from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
import threading
import time
import argparse
import signal
from collections import deque
from queue import Empty, Queue
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
    ch1_m: Optional[float]
    ch1_b: Optional[float]
    ch1_poly2: Optional[tuple[float, float, float]]
    ch1_zero_raw: Optional[float]
    iadc_m: Optional[float]
    iadc_b: Optional[float]
    iadc_zero_raw: Optional[float]


def load_calibration(path: Path) -> Optional[Calibration]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        ch1 = data.get("ch1")
        iadc = data.get("iadc")
        ch1_fit = data.get("ch1_fit", {})
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
            ch1_m=float(ch1["m"]) if ch1 and "m" in ch1 else None,
            ch1_b=float(ch1["b"]) if ch1 and "b" in ch1 else None,
            ch1_poly2=ch1_poly2,
            ch1_zero_raw=float(data.get("ch1_zero_raw")) if data.get("ch1_zero_raw") is not None else None,
            iadc_m=float(iadc["m"]) if iadc and "m" in iadc else None,
            iadc_b=float(iadc["b"]) if iadc and "b" in iadc else None,
            iadc_zero_raw=float(data.get("iadc_zero_raw")) if data.get("iadc_zero_raw") is not None else None,
        )
    except Exception:
        return None


class CsvSpooler:
    """
    Dedicated writer thread:
    - ingests rows from memory queue
    - batches to SQLite (crash-resilient journal)
    - incrementally flushes journaled rows to CSV
    """

    CSV_COLUMNS = [
        "Rx_Timestamp",
        "Header",
        "Seq",
        "Timestamp",
        "1000kg Raw",
        "Tank Pressure Raw",
        "Battery Voltage",
        "CRC",
        "1000kg Calibrated",
        "Weight",
        "Thrust",
        "Tank Pressure Calibrated",
    ]

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._q: Queue = Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._active_session_id: Optional[int] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="csv-spooler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put(("shutdown",))
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def cleanup_storage(self) -> None:
        """
        Remove spool DB artifacts when there are no unfinished sessions.
        Leaves files in place if recovery data is still needed.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        if not self._db_path.exists():
            return
        try:
            conn = sqlite3.connect(self._db_path, timeout=2.0)
            try:
                row = conn.execute(
                    "SELECT COUNT(1) FROM sqlite_master WHERE type='table' AND name='sessions';"
                ).fetchone()
                has_sessions = bool(row and int(row[0]) > 0)
                if has_sessions:
                    unfinished = conn.execute(
                        "SELECT COUNT(1) FROM sessions WHERE finalized=0;"
                    ).fetchone()
                    if unfinished and int(unfinished[0]) > 0:
                        return
            finally:
                conn.close()
        except Exception:
            return

        for p in (
            self._db_path,
            Path(str(self._db_path) + "-wal"),
            Path(str(self._db_path) + "-shm"),
        ):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

    def start_session(self, csv_path: Path) -> None:
        self._q.put(("start_session", str(Path(csv_path).expanduser())))

    def stop_session(self) -> None:
        self._q.put(("stop_session",))

    def enqueue_row(self, row: list) -> None:
        self._q.put(("row", row))

    def recover_pending_sessions(self) -> None:
        self._q.put(("recover",))

    def flush_now(self) -> None:
        self._q.put(("flush_now",))

    @staticmethod
    def _ensure_csv_header(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 0:
            return
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            w.writerow(CsvSpooler.CSV_COLUMNS)
            f.flush()

    def _init_db(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                csv_path TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                last_flushed_row_id INTEGER NOT NULL DEFAULT 0,
                finalized INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rows(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                rx_timestamp TEXT NOT NULL,
                header INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                ts INTEGER NOT NULL,
                ch1_raw REAL NOT NULL,
                tank_raw INTEGER NOT NULL,
                batt_v REAL NOT NULL,
                crc INTEGER NOT NULL,
                ch1_cal REAL NOT NULL,
                weight REAL NOT NULL,
                thrust REAL NOT NULL,
                tank_cal REAL NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            );
            """
        )
        conn.commit()

    def _insert_rows(self, conn: sqlite3.Connection, session_id: int, rows: list[list]) -> None:
        if not rows:
            return
        conn.executemany(
            """
            INSERT INTO rows(
                session_id, rx_timestamp, header, seq, ts, ch1_raw, tank_raw, batt_v, crc,
                ch1_cal, weight, thrust, tank_cal
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            [
                (
                    session_id,
                    str(r[0]),
                    int(r[1]),
                    int(r[2]),
                    int(r[3]),
                    float(r[4]),
                    int(r[5]),
                    float(r[6]),
                    int(r[7]),
                    float(r[8]),
                    float(r[9]),
                    float(r[10]),
                    float(r[11]),
                )
                for r in rows
            ],
        )
        conn.commit()

    def _flush_session_to_csv(self, conn: sqlite3.Connection, session_id: int) -> None:
        row = conn.execute(
            "SELECT csv_path, last_flushed_row_id FROM sessions WHERE id=?;",
            (session_id,),
        ).fetchone()
        if row is None:
            return
        csv_path = Path(row[0])
        last_flushed = int(row[1] or 0)
        self._ensure_csv_header(csv_path)

        pending = conn.execute(
            """
            SELECT id, rx_timestamp, header, seq, ts, ch1_raw, tank_raw, batt_v, crc, ch1_cal, weight, thrust, tank_cal
            FROM rows
            WHERE session_id=? AND id > ?
            ORDER BY id ASC
            LIMIT 5000;
            """,
            (session_id, last_flushed),
        ).fetchall()
        if not pending:
            return

        with open(csv_path, "a", newline="") as f:
            w = csv.writer(f)
            for r in pending:
                w.writerow(r[1:])
            f.flush()

        new_last = int(pending[-1][0])
        conn.execute(
            "UPDATE sessions SET last_flushed_row_id=? WHERE id=?;",
            (new_last, session_id),
        )
        conn.commit()

    def _recover_pending(self, conn: sqlite3.Connection) -> None:
        pending = conn.execute(
            "SELECT id FROM sessions WHERE finalized=0 ORDER BY id ASC;"
        ).fetchall()
        for (sid,) in pending:
            while True:
                before = conn.execute(
                    "SELECT last_flushed_row_id FROM sessions WHERE id=?;",
                    (sid,),
                ).fetchone()
                if before is None:
                    break
                old_last = int(before[0] or 0)
                self._flush_session_to_csv(conn, sid)
                after = conn.execute(
                    "SELECT last_flushed_row_id FROM sessions WHERE id=?;",
                    (sid,),
                ).fetchone()
                if after is None or int(after[0] or 0) == old_last:
                    break
            conn.execute(
                "UPDATE sessions SET finalized=1, ended_at=COALESCE(ended_at, ?) WHERE id=?;",
                (datetime.now().isoformat(timespec="seconds"), sid),
            )
            conn.commit()

    def _run(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        try:
            self._init_db(conn)
            self._recover_pending(conn)
            batch: list[list] = []
            last_flush = time.monotonic()
            flush_period_s = 0.25
            while (not self._stop.is_set()) or (not self._q.empty()) or batch:
                try:
                    msg = self._q.get(timeout=0.05)
                except Empty:
                    msg = None

                if msg is not None:
                    kind = msg[0]
                    if kind == "start_session":
                        csv_path = Path(msg[1])
                        rec = conn.execute(
                            "INSERT INTO sessions(csv_path, started_at) VALUES (?, ?);",
                            (str(csv_path), datetime.now().isoformat(timespec="seconds")),
                        )
                        self._active_session_id = int(rec.lastrowid)
                        conn.commit()
                    elif kind == "stop_session":
                        if self._active_session_id is not None:
                            sid = self._active_session_id
                            if batch:
                                self._insert_rows(conn, sid, batch)
                                batch.clear()
                            while True:
                                before = conn.execute(
                                    "SELECT last_flushed_row_id FROM sessions WHERE id=?;",
                                    (sid,),
                                ).fetchone()
                                old_last = int(before[0] or 0) if before else 0
                                self._flush_session_to_csv(conn, sid)
                                after = conn.execute(
                                    "SELECT last_flushed_row_id FROM sessions WHERE id=?;",
                                    (sid,),
                                ).fetchone()
                                if after is None or int(after[0] or 0) == old_last:
                                    break
                            conn.execute(
                                "UPDATE sessions SET ended_at=?, finalized=1 WHERE id=?;",
                                (datetime.now().isoformat(timespec="seconds"), sid),
                            )
                            conn.commit()
                            self._active_session_id = None
                    elif kind == "row":
                        if self._active_session_id is not None:
                            batch.append(msg[1])
                    elif kind == "recover":
                        self._recover_pending(conn)
                    elif kind == "flush_now":
                        if self._active_session_id is not None and batch:
                            self._insert_rows(conn, self._active_session_id, batch)
                            batch.clear()
                        if self._active_session_id is not None:
                            self._flush_session_to_csv(conn, self._active_session_id)
                    elif kind == "shutdown":
                        pass

                now = time.monotonic()
                if (now - last_flush) >= flush_period_s:
                    if self._active_session_id is not None and batch:
                        self._insert_rows(conn, self._active_session_id, batch)
                        batch.clear()
                    if self._active_session_id is not None:
                        self._flush_session_to_csv(conn, self._active_session_id)
                    last_flush = now
        finally:
            conn.close()


class RadioWorker(QtCore.QThread):
    """
    Background serial read loop.
    Emits values to GUI and (optionally) logs to CSV.
    """
    # t_mono, ch0_raw, ch1_raw, ch0_cal, ch1_cal, tank_pressure, battery_voltage
    sample = QtCore.pyqtSignal(float, float, float, float, float, float, float)
    raw_sample = QtCore.pyqtSignal(float, float, float, float)  # t_mono, ch0_raw, ch1_raw, iadc_raw
    status = QtCore.pyqtSignal(str)
    data_rate = QtCore.pyqtSignal(float, float)  # bytes_per_s, packets_per_s
    link_state = QtCore.pyqtSignal(bool)  # True=connected, False=disconnected
    ACK_GUARD_SECONDS = 0.5
    ACK_WAIT_TIMEOUT_SECONDS = 2.0

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
        self._last_csv_flush_mono = 0.0
        self._csv_flush_interval_s = 0.25
        self._spooler = CsvSpooler(Path(__file__).parent / "Data" / "save_spool.db")
        self._raw_stream_enabled = False
        self._ui_emit_in_flight = False
        self._ui_state: dict = {}
        self._event_log_path: Optional[Path] = None
        self._event_log_file = None
        self._event_log_lock = threading.Lock()
        self._reader_stop = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        # Unbounded queue: do not silently drop telemetry while saving.
        self._event_queue: deque = deque()
        self._event_lock = threading.Lock()
        self._low_latency_display_mode = True
        self._latest_display_packet: Optional[tuple[object, float]] = None
        self._ack_guard_until = 0.0
        self._ack_guard_lock = threading.Lock()
        self._expected_acks: deque[tuple[str, bool, float]] = deque()
        self._expected_acks_lock = threading.Lock()
        self._tx_queue: deque[tuple[str, bool]] = deque()
        self._tx_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._ingress_packets = 0
        self._ingress_bytes = 0
        self._post_reconnect_grace_until = 0.0
        self._force_live_until = 0.0

        # recent telemetry buffer (for “save last 10s”)
        self._recent: Deque[BufferedRow] = deque(maxlen=20000)  # plenty for high-rate

        # dedupe for current file
        self._written_keys: Deque[CsvKey] = deque(maxlen=100000)  # bounds memory
        self._written_set: set[CsvKey] = set()

    def _start_reader_thread(self) -> None:
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, name="radio-reader", daemon=True)
        self._reader_thread.start()

    def stop(self) -> None:
        self._stop = True

    # ----------------------------
    # Thread-safe UI -> worker API
    # ----------------------------
    @QtCore.pyqtSlot(str)
    def start_logging(self, filename: str) -> None:
        if self._logging_enabled:
            self.status.emit("Saving already active; start ignored")
            return
        path = self._next_non_overwriting_csv_path(Path(filename))
        self._csv_path = path
        self._open_event_log_for_csv(path)
        self._spooler.start()
        self._spooler.recover_pending_sessions()
        self._spooler.start_session(path)
        self._logging_enabled = True
        self._logging_paused = False
        self._log_event("info", "saving_start", f"Saving started: {path.resolve()}")
        self.status.emit(f"Saving: ON → {path.resolve()}")

    @QtCore.pyqtSlot()
    def stop_logging(self) -> None:
        self._logging_enabled = False
        self._logging_paused = False
        self._spooler.stop_session()
        self._spooler.stop()
        self._spooler.cleanup_storage()
        self._close_csv()
        # Return to low-latency display mode after save sessions end.
        self._csv_path = None
        self._log_event("info", "saving_stop", "Saving stopped")
        self._close_event_log()
        self.status.emit("Saving: OFF")

    @QtCore.pyqtSlot(bool)
    def set_logging_paused(self, paused: bool) -> None:
        self._logging_paused = bool(paused)
        self._log_event("info", "saving_paused" if self._logging_paused else "saving_resumed", "")
        if self._logging_enabled:
            self.status.emit("Saving: PAUSED" if self._logging_paused else "Saving: RUNNING")

    @QtCore.pyqtSlot(str)
    def update_ui_state(self, ui_state_json: str) -> None:
        try:
            self._ui_state = json.loads(ui_state_json)
        except Exception:
            self._ui_state = {"raw": str(ui_state_json)}
        self._log_event("info", "ui_state", "")

    @QtCore.pyqtSlot(str)
    def save_last_10s(self, filename: str) -> None:
        """
        Writes last ~10 seconds of buffered telemetry to CSV,
        without duplicating rows already written to the same file.
        """
        path = Path(filename)
        was_logging = bool(self._logging_enabled)
        active_path = self._csv_path

        now = time.monotonic()
        cutoff = now - 10.0
        rows = [br for br in self._recent if br.t_mono >= cutoff]

        wrote = 0
        if was_logging and active_path is not None and Path(active_path) != path:
            # While continuous saving is active, snapshots to a different file
            # must never mutate active logging state.
            path.parent.mkdir(parents=True, exist_ok=True)
            new_file = (not path.exists()) or (path.stat().st_size == 0)
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow([
                        "Rx_Timestamp",
                        "Header",
                        "Seq",
                        "Timestamp",
                        "1000kg Raw",
                        "Tank Pressure Raw",
                        "Battery Voltage",
                        "CRC",
                        "1000kg Calibrated",
                        "Weight",
                        "Thrust",
                        "Tank Pressure Calibrated",
                    ])
                for br in rows:
                    w.writerow(br.row)
                    wrote += 1
                f.flush()
        else:
            # Snapshot to active file (or while not actively logging): keep dedupe behavior.
            self._csv_path = path
            self._open_csv_if_needed()
            for br in rows:
                if self._write_row_dedup(br.key, br.row):
                    wrote += 1
            if not was_logging:
                self._close_csv()
                self._csv_path = active_path

        self.status.emit(f"Saved last 10s: wrote {wrote} row(s) → {path.resolve()}")

    def send_command(self, command: str, on: bool) -> None:
        cmd = str(command).upper()[:1]
        state = bool(on)
        if not cmd:
            return
        self._register_expected_ack(command, on)
        # Arm before TX so very fast ACKs are still protected.
        self._arm_ack_guard(extra_s=self.ACK_WAIT_TIMEOUT_SECONDS)
        with self._tx_lock:
            self._tx_queue.append((cmd, state))

    def _arm_ack_guard(self, extra_s: float = 0.0) -> None:
        guard_for = max(0.0, self.ACK_GUARD_SECONDS + float(extra_s))
        until = time.monotonic() + guard_for
        with self._ack_guard_lock:
            if until > self._ack_guard_until:
                self._ack_guard_until = until

    def _ack_guard_active(self) -> bool:
        now = time.monotonic()
        with self._ack_guard_lock:
            if now < self._ack_guard_until:
                return True
        return self._has_pending_expected_ack(now)

    def _register_expected_ack(self, command: str, on: bool) -> None:
        cmd = str(command).upper()[:1]
        state = bool(on)
        deadline = time.monotonic() + self.ACK_WAIT_TIMEOUT_SECONDS
        with self._expected_acks_lock:
            self._expected_acks.append((cmd, state, deadline))

    def _has_pending_expected_ack(self, now: float | None = None) -> bool:
        t_now = time.monotonic() if now is None else now
        with self._expected_acks_lock:
            while self._expected_acks and self._expected_acks[0][2] < t_now:
                self._expected_acks.popleft()
            return bool(self._expected_acks)

    def _consume_expected_ack(self, cmd: str, state: bool) -> None:
        t_now = time.monotonic()
        want_cmd = str(cmd).upper()[:1]
        want_state = bool(state)
        with self._expected_acks_lock:
            if not self._expected_acks:
                return
            kept: deque[tuple[str, bool, float]] = deque()
            consumed = False
            while self._expected_acks:
                exp_cmd, exp_state, deadline = self._expected_acks.popleft()
                if deadline < t_now:
                    continue
                if (not consumed) and exp_cmd == want_cmd and exp_state == want_state:
                    consumed = True
                    continue
                kept.append((exp_cmd, exp_state, deadline))
            self._expected_acks = kept

    def _drain_tx_queue(self) -> None:
        pending: list[tuple[str, bool]] = []
        with self._tx_lock:
            while self._tx_queue:
                pending.append(self._tx_queue.popleft())
        for cmd, state in pending:
            self.radio.send_command(cmd, state)

    @QtCore.pyqtSlot()
    def reload_calibration(self) -> None:
        self._calibration = load_calibration(self._calibration_path)
        if self._calibration is None:
            self.status.emit("Calibration: not loaded (using defaults)")
        else:
            self.status.emit("Calibration: loaded")

    @QtCore.pyqtSlot(bool)
    def set_raw_stream_enabled(self, enabled: bool) -> None:
        self._raw_stream_enabled = bool(enabled)

    @QtCore.pyqtSlot()
    def on_ui_sample_consumed(self) -> None:
        self._ui_emit_in_flight = False

    def _enqueue_event(self, ev, t_read_mono: float, *, coalesce_telemetry: bool = False) -> None:
        with self._event_lock:
            # Keep command ACKs ordered.
            # Optional telemetry coalescing is only for lowest-latency display paths.
            if coalesce_telemetry and (not isinstance(ev, tuple)):
                while self._event_queue and (not isinstance(self._event_queue[-1][0], tuple)):
                    self._event_queue.pop()
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

    def _clear_pending_rx_backlog(self) -> tuple[int, int]:
        """
        Drop buffered RX events so reconnect resumes at live data instead of
        replaying stale queue history.
        Returns (queued_events_dropped, latest_display_dropped_flag).
        """
        with self._event_lock:
            dropped_events = len(self._event_queue)
            self._event_queue.clear()
            dropped_latest = 1 if self._latest_display_packet is not None else 0
            self._latest_display_packet = None
        return dropped_events, dropped_latest

    def _add_ingress_telemetry(self, packet_count: int, byte_count: int) -> None:
        with self._rate_lock:
            self._ingress_packets += int(packet_count)
            self._ingress_bytes += int(byte_count)

    def _take_ingress_totals(self) -> tuple[int, int]:
        with self._rate_lock:
            return self._ingress_packets, self._ingress_bytes

    def _reader_loop(self) -> None:
        while not self._reader_stop.is_set():
            try:
                self._drain_tx_queue()
                now = time.monotonic()
                in_reconnect_grace = now < self._post_reconnect_grace_until
                force_live = now < self._force_live_until
                if (self._low_latency_display_mode or force_live) and (not self._ack_guard_active()):
                    # After reconnect, trim more aggressively so UI snaps to live data.
                    if force_live:
                        self.radio.drop_os_backlog_if_needed(64)
                        self.radio.prune_rx_buffer_to_latest(128)
                    elif in_reconnect_grace:
                        self.radio.drop_os_backlog_if_needed(256)
                        self.radio.prune_rx_buffer_to_latest(512)
                    else:
                        # Keep latency low, but avoid over-aggressive flushing that can starve decoding.
                        self.radio.drop_os_backlog_if_needed(8192)
                        self.radio.prune_rx_buffer_to_latest(4096)
                ev = self.radio.poll_event()
            except Exception:
                time.sleep(0.001)
                continue
            if ev is None:
                # Yield CPU to keep UI responsive during high-rate test/sim loops.
                time.sleep(0.0001 if self.radio.simulate else 0.0002)
                continue
            t_now = time.monotonic()
            if not isinstance(ev, tuple):
                self._add_ingress_telemetry(1, PacketHandler.PACKET_SIZE)
                # Always keep newest telemetry packet available for low-latency UI display.
                self._set_latest_display_packet(ev, t_now)
                full_fidelity_required = self._logging_enabled or (self._csv_path is not None)
                low_latency_only = self._low_latency_display_mode or (t_now < self._force_live_until)
                if low_latency_only and (not full_fidelity_required):
                    continue
                self._enqueue_event(ev, t_now, coalesce_telemetry=False)
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
        self._last_csv_flush_mono = time.monotonic()

        if new_file:
            self._csv_writer.writerow([
                "Rx_Timestamp",
                "Header",
                "Seq",
                "Timestamp",
                "1000kg Raw",
                "Tank Pressure Raw",
                "Battery Voltage",
                "CRC",
                "1000kg Calibrated",
                "Weight",
                "Thrust",
                "Tank Pressure Calibrated",
            ])
            self._write_calibration_row()
            self._csv_file.flush()

        # If we opened a new/different file, we should ensure dedupe structures match.
        # (We reset dedupe when we close with reset_dedupe=True.)
        self.status.emit(f"Logging to {self._csv_path.resolve()}")

    def _next_non_overwriting_csv_path(self, path: Path) -> Path:
        p = Path(path).expanduser()
        if not p.exists():
            return p
        suffix = p.suffix
        stem = p.stem or "data"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = p.with_name(f"{stem}_{ts}{suffix}")
        idx = 2
        while candidate.exists():
            candidate = p.with_name(f"{stem}_{ts}_{idx}{suffix}")
            idx += 1
        return candidate

    def _open_event_log_for_csv(self, csv_path: Path) -> None:
        p = Path(csv_path).expanduser()
        log_path = p.with_name(f"{p.name}.events.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._event_log_path = log_path
        with self._event_log_lock:
            if self._event_log_file is not None:
                try:
                    self._event_log_file.close()
                except Exception:
                    pass
            self._event_log_file = open(log_path, "a", encoding="utf-8")
        self._log_event("info", "log_open", f"Event log opened: {log_path.resolve()}")

    def _close_event_log(self) -> None:
        with self._event_log_lock:
            if self._event_log_file is None:
                return
            try:
                self._event_log_file.flush()
                self._event_log_file.close()
            except Exception:
                pass
            self._event_log_file = None

    def _log_event(self, level: str, event: str, message: str, extra: Optional[dict] = None) -> None:
        with self._event_log_lock:
            if self._event_log_file is None:
                return
            rec = {
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "level": str(level),
                "event": str(event),
                "message": str(message),
                "save_state": {
                    "enabled": bool(self._logging_enabled),
                    "paused": bool(self._logging_paused),
                    "csv_path": str(self._csv_path) if self._csv_path is not None else None,
                },
                "ui_state": self._ui_state,
            }
            if extra:
                rec["extra"] = extra
            try:
                self._event_log_file.write(json.dumps(rec, separators=(",", ":")) + "\n")
                self._event_log_file.flush()
            except Exception:
                pass

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
        self._flush_csv_if_due()

        # record
        self._written_keys.append(key)
        self._written_set.add(key)

        # prune set alongside deque maxlen behavior
        while len(self._written_set) > len(self._written_keys):
            # should never happen, but keep safe
            self._written_set = set(self._written_keys)

        return True

    def _write_row(self, row: list) -> bool:
        if self._csv_writer is None:
            return False
        self._csv_writer.writerow(row)
        self._flush_csv_if_due()
        return True

    def _flush_csv_if_due(self, *, force: bool = False) -> None:
        if self._csv_file is None:
            return
        now = time.monotonic()
        if (not force) and ((now - self._last_csv_flush_mono) < self._csv_flush_interval_s):
            return
        try:
            self._csv_file.flush()
            self._last_csv_flush_mono = now
        except Exception:
            pass

    def _format_calib_value(self, m: Optional[float], b: Optional[float]) -> str:
        if m is None or b is None:
            return ""
        return f"m={m:.10g},b={b:.10g}"

    def _write_calibration_row(self) -> None:
        if self._csv_writer is None:
            return
        if self._calibration is None:
            return

        ch1 = self._format_calib_value(self._calibration.ch1_m, self._calibration.ch1_b)
        iadc = self._format_calib_value(self._calibration.iadc_m, self._calibration.iadc_b)
        if not ch1 and not iadc:
            return

        row = [
            "CALIBRATION",
            "",
            "",
            "",
            ch1,
            iadc,
            "",
            "",
            "",
            "",
            "",
            "",
        ]
        self._csv_writer.writerow(row)
        self._flush_csv_if_due(force=True)

    def _calibrate_channels(self, _ch0_raw: float, ch1_raw: float) -> tuple[float, float]:
        # Ch0 (legacy 50kg) is ignored by design. Keep API shape for GUI compatibility.
        ch0_kg = 0.0
        if self._calibration is None:
            ch1_kg = (10.0 * ((ch1_raw / 2.929497e-06) - (10 - 1.8) + 3.8)) - 14
            return float(ch0_kg), float(ch1_kg)

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

    def _calibrate_tank_pressure(self, iadc_raw: float) -> float:
        if self._calibration is not None and self._calibration.iadc_m is not None and self._calibration.iadc_b is not None:
            if self._calibration.iadc_zero_raw is not None:
                return float(self._calibration.iadc_m * (iadc_raw - self._calibration.iadc_zero_raw))
            return float(self._calibration.iadc_m * iadc_raw + self._calibration.iadc_b)
        return float(iadc_raw / 1.78)

    @staticmethod
    def _split_weight_thrust(value: float) -> tuple[float, float]:
        signed = float(value)
        weight = max(0.0, -signed)
        thrust = max(0.0, signed)
        return weight, thrust

    # ----------------------------
    # Main worker loop
    # ----------------------------
    def run(self) -> None:
        last_status = ""
        last_ui_emit = 0.0
        last_raw_emit = 0.0
        ui_emit_period = 1.0 / 120.0
        raw_emit_period = 1.0 / 120.0
        rate_period = 0.25
        last_rate_emit = time.monotonic()
        prev_ingress_packets = 0
        prev_ingress_bytes = 0
        last_pkt_rate = 0.0
        last_byte_rate = 0.0
        crc_verify_enabled = True
        seen_igniter_command = False
        turn_p_off = False
        p_start = time.monotonic()
        was_connected = False
        last_link_state: Optional[bool] = None
        no_data_reconnect_s = 0.75
        last_rx_item_mono = time.monotonic()
        last_no_data_status = 0.0
        last_log_heartbeat = time.monotonic()

        self._spooler.start()
        self._spooler.recover_pending_sessions()
        self._start_reader_thread()

        try:
            while not self._stop:
                if (self._reader_thread is None) or (not self._reader_thread.is_alive()):
                    self.status.emit("Reader thread restarted")
                    self._start_reader_thread()
                # --- connection management ---
                is_connected = self.radio.is_connected()
                if not is_connected:
                    if was_connected:
                        dropped_events, dropped_latest = self._clear_pending_rx_backlog()
                        if (dropped_events + dropped_latest) > 0:
                            self.status.emit(
                                f"Dropped stale backlog after disconnect "
                                f"({dropped_events + dropped_latest} item(s))"
                            )
                    ok = self.radio.reconnect()
                    new_status = "Reconnected" if ok else "Disconnected (retrying...)"
                    if new_status != last_status:
                        self.status.emit(new_status)
                        self._log_event("warning" if not ok else "info", "link_status", new_status)
                        last_status = new_status
                    if last_link_state is not bool(ok):
                        self.link_state.emit(bool(ok))
                        last_link_state = bool(ok)
                    if ok:
                        # On reconnect, hard-flush stale serial/parser/event backlog.
                        dropped_os, dropped_rx = self.radio.clear_backlog()
                        dropped_events, dropped_latest = self._clear_pending_rx_backlog()
                        dropped_total = dropped_os + dropped_rx + dropped_events + dropped_latest
                        if dropped_total > 0:
                            self.status.emit(f"Reconnect sync: dropped {dropped_total} stale item(s)")
                            self._log_event("info", "reconnect_sync", f"dropped={dropped_total}")
                        now_ok = time.monotonic()
                        # Short grace window uses aggressive low-latency trimming in reader loop.
                        self._post_reconnect_grace_until = now_ok + 0.4
                        # For a few seconds after reconnect, force newest-only telemetry
                        # to avoid replaying stale buffered stream.
                        self._force_live_until = now_ok + 5.0
                        last_rx_item_mono = now_ok
                    was_connected = bool(ok)
                    time.sleep(0.01 if ok else 0.10)
                    continue
                was_connected = True
                if last_link_state is not True:
                    self.link_state.emit(True)
                    last_link_state = True

                # --- unified RX (telemetry + ACKs) ---
                display_only = (not self._logging_enabled) and (self._csv_path is None) and (not self._raw_stream_enabled)
                self._low_latency_display_mode = display_only

                want_crc = True
                if want_crc != crc_verify_enabled:
                    self.radio.set_crc_verification(want_crc)
                    crc_verify_enabled = want_crc

                item = self._dequeue_event()
                if item is None and (display_only or (time.monotonic() < self._force_live_until)):
                    item = self._take_latest_display_packet()
                if item is None:
                    now = time.monotonic()
                    if (now - last_rx_item_mono) >= no_data_reconnect_s:
                        if (now - last_no_data_status) >= 1.0:
                            self.status.emit("No data (forcing reconnect...)")
                            self._log_event("warning", "no_data_reconnect", "No data, forcing reconnect")
                            last_no_data_status = now
                        try:
                            self.radio.close()
                        except Exception:
                            pass
                        was_connected = False
                        self._post_reconnect_grace_until = 0.0
                        time.sleep(0.01)
                        continue
                    time.sleep(0.0001)
                    continue
                ev, ev_t_mono = item
                last_rx_item_mono = time.monotonic()

                # --- ACK event ---
                if isinstance(ev, tuple):
                    cmd, state = ev
                    self._consume_expected_ack(cmd, state)
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
                            self._consume_expected_ack(cmd, state)
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
                ch1_raw = float(packet.channel1)

                # CSV work is expensive; skip it unless a save session has been started.
                if self._logging_enabled or self._csv_path is not None:
                    rx_timestamp = datetime.now().isoformat(timespec="milliseconds")
                    try:
                        _, ch1_kg_csv = self._calibrate_channels(0.0, ch1_raw)
                        weight_csv, thrust_csv = self._split_weight_thrust(ch1_kg_csv)
                        tank_p_csv = self._calibrate_tank_pressure(float(packet.internal_adc))
                        row = [
                            rx_timestamp,
                            int(packet.header),
                            int(packet.sequence),
                            int(packet.timestamp),
                            float(packet.channel1),
                            int(packet.internal_adc),
                            float(packet.battery_voltage),
                            int(packet.crc),
                            ch1_kg_csv,
                            weight_csv,
                            thrust_csv,
                            tank_p_csv,
                        ]
                        key = CsvKey(
                            header=int(packet.header),
                            seq=int(packet.sequence),
                            timestamp=int(packet.timestamp),
                            crc=int(packet.crc),
                        )
                    except Exception as e:
                        self.status.emit(f"Packet->CSV error: {e}")
                        self._log_event("error", "packet_to_csv_error", str(e))
                        continue

                    # Buffer for snapshots
                    self._recent.append(BufferedRow(t_mono=t_mono, key=key, row=row))

                    # CSV output (if enabled)
                    if self._logging_enabled and (not self._logging_paused):
                        try:
                            self._spooler.enqueue_row(row)
                        except Exception as e:
                            self.status.emit(f"CSV write error: {e}")
                            self._log_event("error", "csv_write_error", str(e))

                # GUI update
                try:
                    ui_packet = packet
                    ui_t_mono = t_mono
                    latest_ui = self._take_latest_display_packet()
                    if latest_ui is not None and (not isinstance(latest_ui[0], tuple)):
                        ui_packet, ui_t_mono = latest_ui

                    ch1_raw_ui = float(ui_packet.channel1)

                    if self._raw_stream_enabled and (ui_t_mono - last_raw_emit) >= raw_emit_period:
                        self.raw_sample.emit(float(ui_t_mono), 0.0, ch1_raw_ui, float(ui_packet.internal_adc))
                        last_raw_emit = ui_t_mono

                    if ((ui_t_mono - last_ui_emit) >= ui_emit_period) and (not self._ui_emit_in_flight):
                        _, ch1_cal = self._calibrate_channels(0.0, ch1_raw_ui)
                        tank_pressure = self._calibrate_tank_pressure(float(ui_packet.internal_adc))
                        batt_v = float(ui_packet.battery_voltage)
                        self._ui_emit_in_flight = True
                        self.sample.emit(
                            float(ui_t_mono),
                            0.0,
                            float(ch1_raw_ui),
                            0.0,
                            float(ch1_cal),
                            float(tank_pressure),
                            batt_v,
                        )
                        last_ui_emit = ui_t_mono
                except Exception as e:
                    self.status.emit(f"Emit error: {e}")
                    self._log_event("error", "emit_error", str(e))

                now_rate = time.monotonic()
                dt_rate = now_rate - last_rate_emit
                if dt_rate >= rate_period:
                    ingress_packets, ingress_bytes = self._take_ingress_totals()
                    pkt_rate = float(ingress_packets - prev_ingress_packets) / dt_rate
                    byte_rate = float(ingress_bytes - prev_ingress_bytes) / dt_rate
                    last_pkt_rate = pkt_rate
                    last_byte_rate = byte_rate
                    prev_ingress_packets = ingress_packets
                    prev_ingress_bytes = ingress_bytes
                    self.data_rate.emit(byte_rate, pkt_rate)
                    last_rate_emit = now_rate
                if self._logging_enabled and ((now_rate - last_log_heartbeat) >= 1.0):
                    self._log_event(
                        "info",
                        "save_heartbeat",
                        "",
                        extra={"ingress_packets": int(prev_ingress_packets), "pkt_rate": float(last_pkt_rate), "byte_rate": float(last_byte_rate)},
                    )
                    last_log_heartbeat = now_rate

        finally:
            self._reader_stop.set()
            try:
                self._spooler.stop_session()
            except Exception:
                pass
            try:
                self._spooler.stop()
            except Exception:
                pass
            try:
                self._spooler.cleanup_storage()
            except Exception:
                pass
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
            if last_link_state is not False:
                self.link_state.emit(False)
            self._log_event("info", "worker_shutdown", "Radio worker shutting down")
            self._close_event_log()
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

    # Allow Ctrl+C in terminal to close the Qt app cleanly.
    # Use a flag + timer hop so Qt shutdown runs on the GUI thread.
    sigint_requested = threading.Event()
    signal.signal(signal.SIGINT, lambda _sig, _frame: sigint_requested.set())
    sigint_pump = QtCore.QTimer()
    sigint_pump.timeout.connect(lambda: app.quit() if sigint_requested.is_set() else None)
    sigint_pump.start(100)

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
    worker.link_state.connect(win.set_link_connected)

    # GUI -> Worker logging controls (queued across threads)
    win.start_saving.connect(worker.start_logging, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.stop_saving.connect(worker.stop_logging, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.pause_saving.connect(worker.set_logging_paused, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.save_last_10s.connect(worker.save_last_10s, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.calibration_saved.connect(worker.reload_calibration, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.raw_stream_enabled.connect(worker.set_raw_stream_enabled, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.sample_consumed.connect(worker.on_ui_sample_consumed, type=QtCore.Qt.ConnectionType.QueuedConnection)
    win.ui_state_changed.connect(worker.update_ui_state, type=QtCore.Qt.ConnectionType.QueuedConnection)

    # initial filename in GUI
    win.set_filename(default_csv_filename)
    win.set_calibration_filename(str(calibration_path))

    def handle_status(s: str) -> None:
        if s.startswith("ACK:"):
            win.cmd_status_lbl.setText(s)
        elif s.startswith("Calibration: loaded"):
            win.on_calibration_reload_status(True)
            win.setWindowTitle(f"Serial Telemetry Viewer — {s}")
        elif s.startswith("Calibration: not loaded"):
            win.on_calibration_reload_status(False)
            win.setWindowTitle(f"Serial Telemetry Viewer — {s}")
        else:
            win.setWindowTitle(f"Serial Telemetry Viewer — {s}")

    worker.status.connect(handle_status)
    worker.start()

    exit_code = 0
    try:
        exit_code = app.exec()
    except KeyboardInterrupt:
        app.quit()
        exit_code = 130
    finally:
        worker.stop()
        worker.wait(2000)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
