# radio.py
from __future__ import annotations

import random
import time
from typing import Optional, Tuple

import serial

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
            baudrate: int = 115200,
            *,
            sim_rate_hz: float = 50.0,
            sim_seed: Optional[int] = None,
            serial_timeout: float = 0.05,  # shorter helps buffer-based parsing
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

        if not self.simulate:
            # timeout governs read() calls (including ACK waits)
            self.ser = serial.Serial(port, baudrate, timeout=serial_timeout)
            try:
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
            except Exception:
                pass
        else:
            self.ser = None
            print("[Radio] Using simulated data source")

    def is_connected(self) -> bool:
        if self.simulate:
            return True
        return self.ser is not None and self.ser.is_open

    # -------------------------
    # Simulation implementation
    # -------------------------

    def _read_simulated_packet(self) -> Optional[DataPacket]:
        now = time.monotonic()

        if self._sim_period > 0 and (now - self._last_sim_time) < self._sim_period:
            return None

        self._last_sim_time = now
        self._sim_seq = (self._sim_seq + 1) & 0xFF  # keep it byte-like if you want

        # Simulate float channels (adjust ranges as desired)
        ch0 = self._rng.uniform(-1.0, 1.0)
        ch1 = self._rng.uniform(-1.0, 1.0)

        iadc = self._rng.randint(0, 1023)

        # CRC here is just dummy; your real CRC is in-packet.
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

        # Prefer non-blocking-ish reads if supported by pyserial
        try:
            n = self.ser.in_waiting
        except Exception:
            n = 0

        if n and n > 0:
            chunk = self.ser.read(n)
        else:
            # fall back to reading at least 1 byte (uses timeout)
            chunk = self.ser.read(1)

        if chunk:
            self._rx_buf += chunk

        # keep buffer bounded
        if len(self._rx_buf) > 4096:
            self._rx_buf = self._rx_buf[-4096:]

    def read_packet(self) -> Optional[DataPacket]:
        if self.simulate:
            return self._read_simulated_packet()

        if not self.is_connected():
            raise ConnectionError("Not connected to serial port")

        try:
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
                # If decode failed, try to resync by dropping one byte
                if self._rx_buf:
                    del self._rx_buf[0:1]
                return None

            # If your PacketHandler currently decodes ch0/ch1 as uint32 but you
            # intend them to be float32, you should change PacketHandler to unpack
            # '<BBIffHH' and then this conversion is unnecessary.
            # Otherwise, if pkt.channel0/pkt.channel1 might still be ints, convert:
            try:
                pkt.channel0 = float(pkt.channel0)  # type: ignore[attr-defined]
                pkt.channel1 = float(pkt.channel1)  # type: ignore[attr-defined]
            except Exception:
                pass

            return pkt

        except Exception as e:
            print(f"Error reading from serial port: {e}")
            return None

    # -------------------------
    # TX: commands + ACK
    # -------------------------

    def send_command(
            self,
            cmd: str,
            on: bool,
            *,
            wait_ack: bool = True,
            ack_timeout_s: float = 2.0,
    ) -> Optional[Tuple[str, bool]]:
        """
        Send a command to the STM32.

        cmd: 'I' (igniter), 'P' (pilot), 'T' (tanks), 'S' (spare)
        on: True -> CMD_ON (0x01), False -> CMD_OFF (0x02)

        If wait_ack is True, blocks up to ack_timeout_s waiting for a valid ACK.
        Returns (cmd_char, state_bool) on ACK, or None on timeout/no-ack.
        """
        if self.simulate:
            return (cmd, on) if wait_ack else None

        if not self.is_connected():
            raise ConnectionError("Not connected to serial port")

        val = PacketHandler.CMD_ON if on else PacketHandler.CMD_OFF
        frame = PacketHandler.encode_command(cmd, val)

        self.ser.write(frame)
        self.ser.flush()

        if not wait_ack:
            return None

        deadline = time.monotonic() + max(0.0, ack_timeout_s)
        buf = bytearray()

        while time.monotonic() < deadline:
            chunk = self.ser.read(1)
            if not chunk:
                continue
            buf += chunk

            if len(buf) > 32:
                buf = buf[-32:]

            idx = buf.find(bytes([PacketHandler.ACK_HEADER]))
            if idx == -1:
                continue
            if len(buf) < idx + 4:
                continue

            cand = bytes(buf[idx: idx + 4])
            parsed = PacketHandler.decode_ack(cand)
            if parsed is not None:
                return parsed

            buf = buf[idx + 1:]

        return None

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
