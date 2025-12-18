# radio.py
from __future__ import annotations

import random
import time
from typing import Optional, Tuple, Union

import serial
from serial.serialutil import SerialException

from handlePacket import PacketHandler
from packet import DataPacket


class Radio:
    """
    Radio interface.

    If port == "dummy" (case-insensitive), simulated packets are generated.
    If using real serial, you can also send 4-byte command frames and optionally
    wait for a 4-byte ACK frame.
    """

    def __init__(
        self,
        port: str = "COM3",
        baudrate: int = 57600,
        *,
        sim_rate_hz: float = 50.0,
        sim_seed: Optional[int] = None,
        serial_timeout: float = 0.05,
    ):
        self.port = port
        self.baudrate = baudrate

        self.simulate = port.lower() in ("dummy", "sim", "simulation")

        # Simulation state
        self.sim_rate_hz = sim_rate_hz
        self._sim_period = 1.0 / sim_rate_hz if sim_rate_hz > 0 else 0.0
        self._rng = random.Random(sim_seed)
        self._last_sim_time = 0.0
        self._sim_seq = 0

        # RX buffer for real serial
        self._rx_buf = bytearray()

        # Status print throttle (optional)
        self._last_err_print = 0.0

        if not self.simulate:
            self.ser = None
            # Try opening once at startup; if it fails, we stay disconnected until reconnect()
            self.reconnect(timeout=serial_timeout)
        else:
            self.ser = None
            print("[Radio] Using simulated data source")

    RadioEvent = Union[DataPacket, Tuple[str, bool]]  # packet OR (cmd, state)

    def poll_event(self) -> Optional[RadioEvent]:
        """
        Pull bytes once, then return the next parsed thing:
          - DataPacket (0xAC, 18 bytes)
          - ACK tuple (cmd_char, state) (0xAB, 4 bytes)
        """
        if self.simulate:
            time.sleep(0.01)
            return self._read_simulated_packet()

        if not self.is_connected():
            return None

        self._pull_serial_bytes()
        if not self._rx_buf:
            return None

        hdr_pkt = bytes([PacketHandler.EXPECTED_HEADER])  # 0xAC
        hdr_ack = bytes([PacketHandler.ACK_HEADER])  # 0xAB

        i_pkt = self._rx_buf.find(hdr_pkt)
        i_ack = self._rx_buf.find(hdr_ack)

        if i_pkt == -1 and i_ack == -1:
            self._rx_buf.clear()
            return None

        # choose earliest header so we stay in-order
        if i_pkt == -1:
            kind, idx = "ack", i_ack
        elif i_ack == -1:
            kind, idx = "pkt", i_pkt
        else:
            kind, idx = ("ack", i_ack) if i_ack < i_pkt else ("pkt", i_pkt)

        # drop junk before header
        if idx > 0:
            del self._rx_buf[:idx]

        if kind == "ack":
            if len(self._rx_buf) < 4:
                return None
            frame = bytes(self._rx_buf[:4])
            del self._rx_buf[:4]
            parsed = PacketHandler.decode_ack(frame)
            return parsed  # (cmd, state) or None

        # kind == "pkt"
        if len(self._rx_buf) < PacketHandler.PACKET_SIZE:
            return None
        frame = bytes(self._rx_buf[:PacketHandler.PACKET_SIZE])
        del self._rx_buf[:PacketHandler.PACKET_SIZE]
        pkt = PacketHandler.decode_packet(frame)
        if pkt is None and self._rx_buf:
            del self._rx_buf[:1]  # resync
        return pkt


    def is_connected(self) -> bool:
        if self.simulate:
            return True
        return self.ser is not None and self.ser.is_open

    def _print_err_throttled(self, msg: str, every_s: float = 1.0) -> None:
        now = time.monotonic()
        if (now - self._last_err_print) >= every_s:
            print(msg)
            self._last_err_print = now

    def _open_serial(self, *, timeout: float) -> None:
        self.ser = serial.Serial(
            self.port,
            self.baudrate,
            timeout=timeout,
            write_timeout=0.5,
            dsrdtr=False,
            rtscts=False,
        )
        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception:
            pass

    def _mark_disconnected(self, reason: str | None = None) -> None:
        if reason:
            self._print_err_throttled(f"[Radio] Disconnected: {reason}")
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self._rx_buf.clear()

    def reconnect(self, *, timeout: float = 0.05) -> bool:
        """Try to reopen the serial port. Returns True on success."""
        if self.simulate:
            return True
        if self.ser and self.ser.is_open:
            return True
        try:
            self._open_serial(timeout=timeout)
            return True
        except Exception as e:
            self._mark_disconnected(str(e))
            return False

    # -------------------------
    # Simulation implementation
    # -------------------------

    def _read_simulated_packet(self) -> Optional[DataPacket]:
        now = time.monotonic()

        if self._sim_period > 0 and (now - self._last_sim_time) < self._sim_period:
            return None

        self._last_sim_time = now
        self._sim_seq = (self._sim_seq + 1) & 0xFF

        ch0 = self._rng.uniform(0.0, 1000.0)
        ch1 = self._rng.uniform(0.0, 1000.0)
        iadc = self._rng.randint(0, 1023)

        crc = (int(ch0 * 1000) ^ int(ch1 * 1000) ^ iadc ^ self._sim_seq) & 0xFFFF

        return DataPacket(
            header=0xAC,
            sequence=self._sim_seq,
            timestamp=int(now * 1000),
            channel0=float(ch0),
            channel1=float(ch1),
            internal_adc=iadc,
            crc=crc,
        )

    # -------------------------
    # RX: telemetry packets
    # -------------------------

    def _pull_serial_bytes(self) -> None:
        """Read whatever is available and append to the RX buffer."""
        if not self.ser:
            return

        try:
            n = 0
            try:
                n = int(self.ser.in_waiting or 0)
            except Exception:
                n = 0

            # Non-blocking-ish: read what's waiting; otherwise read 1 with timeout
            chunk = self.ser.read(n) if n > 0 else self.ser.read(1)

            # If OS said bytes were ready but read returned empty, treat as disconnect
            if n > 0 and chunk == b"":
                raise SerialException(
                    "device reports readiness to read but returned no data "
                    "(device disconnected or multiple access on port?)"
                )

            if chunk:
                self._rx_buf += chunk

            # keep buffer bounded
            if len(self._rx_buf) > 4096:
                self._rx_buf = self._rx_buf[-4096:]

        except (SerialException, OSError) as e:
            self._mark_disconnected(str(e))

    def read_packet(self) -> Optional[DataPacket]:
        if self.simulate:
            return self._read_simulated_packet()

        # IMPORTANT: do not raise; just return None so the worker thread can reconnect
        if not self.is_connected():
            return None

        self._pull_serial_bytes()

        if not self._rx_buf:
            return None

        hdr = bytes([PacketHandler.EXPECTED_HEADER])
        idx = self._rx_buf.find(hdr)
        if idx == -1:
            # No header found; drop old junk
            self._rx_buf.clear()
            return None

        # Drop anything before the header (resync)
        if idx > 0:
            del self._rx_buf[:idx]

        # Need full packet
        if len(self._rx_buf) < PacketHandler.PACKET_SIZE:
            return None

        frame = bytes(self._rx_buf[:PacketHandler.PACKET_SIZE])
        del self._rx_buf[:PacketHandler.PACKET_SIZE]

        pkt = PacketHandler.decode_packet(frame)
        if pkt is None:
            # Try to resync by dropping one byte
            if self._rx_buf:
                del self._rx_buf[0:1]
            return None

        return pkt

    # -------------------------
    # TX: commands + ACK
    # -------------------------

    def send_command(self, cmd: str, on: bool) -> None:
        if self.simulate:
            return
        if not self.is_connected() or not self.ser:
            return

        val = PacketHandler.CMD_ON if on else PacketHandler.CMD_OFF
        frame = PacketHandler.encode_command(cmd, val)
        try:
            self.ser.write(frame)
            self.ser.flush()
        except (SerialException, OSError) as e:
            self._mark_disconnected(str(e))

    def close(self) -> None:
        self._mark_disconnected("closed")
