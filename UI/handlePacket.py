# handlePacket.py
from __future__ import annotations

import binascii
import struct

from packet import DataPacket


class PacketHandler:
    PACKET_SIZE = 22
    EXPECTED_HEADER = 0xAC

    CMD_HEADER = 0xAA
    ACK_HEADER = 0xAB

    CMD_IGNITER = ord('I')
    CMD_SPARE = ord('S')
    CMD_TANKS = ord('T')
    CMD_PILOT = ord('P')

    CMD_ON = 0x01
    CMD_OFF = 0x02

    # CRC-16 parameters (adjust to match firmware)
    CRC16_POLY = 0x1021
    CRC16_INIT = 0xFFFF
    CRC16_XOROUT = 0x0000
    CRC16_REFLECT = False

    @staticmethod
    def crc8_xor(data: bytes) -> int:
        c = 0
        for b in data:
            c ^= b
        return c & 0xFF

    @staticmethod
    def _crc16_reflect8(x: int) -> int:
        x &= 0xFF
        r = 0
        for _ in range(8):
            r = (r << 1) | (x & 1)
            x >>= 1
        return r & 0xFF

    @staticmethod
    def crc16(data: bytes) -> int:
        # Fast path in C for CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect, xorout 0x0000)
        if not PacketHandler.CRC16_REFLECT:
            return (binascii.crc_hqx(data, PacketHandler.CRC16_INIT & 0xFFFF) ^ PacketHandler.CRC16_XOROUT) & 0xFFFF

        crc = PacketHandler.CRC16_INIT & 0xFFFF
        poly = PacketHandler.CRC16_POLY & 0xFFFF

        for b in data:
            if PacketHandler.CRC16_REFLECT:
                b = PacketHandler._crc16_reflect8(b)
            crc ^= (b << 8) & 0xFFFF
            for _ in range(8):
                if crc & 0x8000:
                    crc = ((crc << 1) ^ poly) & 0xFFFF
                else:
                    crc = (crc << 1) & 0xFFFF

        if PacketHandler.CRC16_REFLECT:
            # reflect output if using reflected mode
            out = 0
            x = crc & 0xFFFF
            for _ in range(16):
                out = (out << 1) | (x & 1)
                x >>= 1
            crc = out & 0xFFFF

        return (crc ^ PacketHandler.CRC16_XOROUT) & 0xFFFF

    @staticmethod
    def decode_packet(data: bytes, *, verify_crc: bool = True) -> DataPacket | None:
        try:
            if len(data) != PacketHandler.PACKET_SIZE:
                raise ValueError(
                    f"Invalid packet size: {len(data)} bytes, expected {PacketHandler.PACKET_SIZE}"
                )

            if data[0] != PacketHandler.EXPECTED_HEADER:
                raise ValueError(
                    f"Invalid header: 0x{data[0]:02X}, expected 0x{PacketHandler.EXPECTED_HEADER:02X}"
                )

            # Layout (little-endian):
            # [header:u8][seq:u8][timestamp:u32][ch0:float][ch1:float]
            # [adc:u16][battery_voltage:float][crc:u16]
            header, seq, timestamp, ch0, ch1, adc, battery_voltage, crc = struct.unpack(
                "<BBIffHfH",
                data,
            )

            if verify_crc:
                calc_crc = PacketHandler.crc16(data[:-2])
                if (crc & 0xFFFF) != calc_crc:
                    raise ValueError(f"CRC mismatch: got 0x{crc:04X}, expected 0x{calc_crc:04X}")

            return DataPacket(
                header=header,
                sequence=seq,
                timestamp=timestamp,
                channel0=ch0,
                channel1=ch1,
                internal_adc=adc,
                battery_voltage=battery_voltage,
                crc=crc,
            )
        except Exception:
            return None

    # ---- command/ack helpers unchanged ----
    @staticmethod
    def encode_command(cmd: str, val: int) -> bytes:
        if not isinstance(cmd, str) or len(cmd) != 1:
            raise ValueError("cmd must be a single character like 'I','S','T','P'")
        cmd_b = ord(cmd) & 0xFF
        frame3 = bytes([PacketHandler.CMD_HEADER, cmd_b, val & 0xFF])
        crc = PacketHandler.crc8_xor(frame3)
        return frame3 + bytes([crc])

    @staticmethod
    def decode_ack(data: bytes):
        if len(data) != 4:
            return None
        if data[0] != PacketHandler.ACK_HEADER:
            return None
        if PacketHandler.crc8_xor(data[:3]) != data[3]:
            return None
        cmd_char = chr(data[1])
        state = bool(data[2])
        return cmd_char, state
