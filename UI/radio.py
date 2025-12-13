# radio.py
from __future__ import annotations

import random
import time
import serial
from typing import Optional

from handlePacket import PacketHandler
from packet import DataPacket


class Radio:
    """
    Radio interface.

    If port == "dummy" (case-insensitive), simulated packets are generated
    instead of reading from a serial port.
    """

    def __init__(
        self,
        port: str = "COM3",
        baudrate: int = 115200,
        *,
        sim_rate_hz: float = 50.0,
        sim_seed: Optional[int] = None,
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
            self.ser = serial.Serial(port, baudrate, timeout=1)
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

        # Rate limit simulation
        if self._sim_period > 0 and (now - self._last_sim_time) < self._sim_period:
            return None

        self._last_sim_time = now
        self._sim_seq += 1

        # Adjust ranges to match hardware
        ch0 = self._rng.randint(0, 4095)
        ch1 = self._rng.randint(0, 4095)
        iadc = self._rng.randint(0, 1023)

        # Simple fake CRC
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
    # Public API
    # -------------------------

    def read_packet(self):
        if self.simulate:
            return self._read_simulated_packet()

        if not self.is_connected():
            raise ConnectionError("Not connected to serial port")

        try:
            data = self.ser.read(PacketHandler.PACKET_SIZE)

            if len(data) == 0:
                return None

            if len(data) < PacketHandler.PACKET_SIZE:
                print(f"Warning: Incomplete packet ({len(data)} bytes)")
                return None

            return PacketHandler.decode_packet(data)

        except Exception as e:
            print(f"Error reading from serial port: {e}")
            return None

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
