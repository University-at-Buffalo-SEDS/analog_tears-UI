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
            serial_timeout: float = 1.0,
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

        if not self.simulate:
            # IMPORTANT: timeout governs read() calls (including ACK waits)
            self.ser = serial.Serial(port, baudrate, timeout=serial_timeout)
            # clear any stale bytes
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
        self._sim_seq += 1

        ch0 = self._rng.randint(0, 4095)
        ch1 = self._rng.randint(0, 4095)
        iadc = self._rng.randint(0, 1023)
        crc = (ch0 ^ ch1 ^ iadc ^ self._sim_seq) & 0xFFFF

        return DataPacket(
            header=0xAC,
            sequence=self._sim_seq,
            timestamp=int(now * 1000),
            channel0=ch0,
            channel1=ch1,
            internal_adc=iadc,
            crc=crc,
        )

    # -------------------------
    # RX: telemetry packets
    # -------------------------

    def read_packet(self) -> Optional[DataPacket]:
        if self.simulate:
            return self._read_simulated_packet()

        if not self.is_connected():
            raise ConnectionError("Not connected to serial port")

        try:
            data = self.ser.read(PacketHandler.PACKET_SIZE)
            if len(data) == 0:
                return None
            if len(data) < PacketHandler.PACKET_SIZE:
                # If you want, you can buffer and resync instead of dropping.
                print(f"Warning: Incomplete packet ({len(data)} bytes)")
                return None
            return PacketHandler.decode_packet(data)

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
            # Nothing real to do; pretend it succeeded.
            return (cmd, on) if wait_ack else None

        if not self.is_connected():
            raise ConnectionError("Not connected to serial port")

        val = PacketHandler.CMD_ON if on else PacketHandler.CMD_OFF
        frame = PacketHandler.encode_command(cmd, val)

        # Write the 4-byte command
        self.ser.write(frame)
        self.ser.flush()

        if not wait_ack:
            return None

        # Wait for a 4-byte ACK frame, scanning the stream for 0xAB
        deadline = time.monotonic() + max(0.0, ack_timeout_s)
        buf = bytearray()

        while time.monotonic() < deadline:
            chunk = self.ser.read(1)
            if not chunk:
                continue
            buf += chunk

            # Keep buffer from growing forever
            if len(buf) > 32:
                buf = buf[-32:]

            # Try to find an ACK header and parse 4 bytes from there
            idx = buf.find(bytes([PacketHandler.ACK_HEADER]))
            if idx == -1:
                continue
            if len(buf) < idx + 4:
                continue

            cand = bytes(buf[idx: idx + 4])
            parsed = PacketHandler.decode_ack(cand)
            if parsed is not None:
                return parsed

            # If it looked like an ACK header but failed CRC, skip past it
            buf = buf[idx + 1:]

        return None

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
