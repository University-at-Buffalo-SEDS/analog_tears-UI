# handlePacket.py
from __future__ import annotations

import struct

from packet import DataPacket


class PacketHandler:
    PACKET_SIZE = 18
    EXPECTED_HEADER = 0xAC

    CMD_HEADER = 0xAA
    ACK_HEADER = 0xAB

    CMD_IGNITER = ord('I')
    CMD_SPARE = ord('S')
    CMD_TANKS = ord('T')
    CMD_PILOT = ord('P')

    CMD_ON = 0x01
    CMD_OFF = 0x02

    @staticmethod
    def crc8_xor(data: bytes) -> int:
        c = 0
        for b in data:
            c ^= b
        return c & 0xFF

    @staticmethod
    def decode_packet(data: bytes) -> DataPacket | None:
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
            # [header:u8][seq:u8][timestamp:u32][ch0:float][ch1:float][adc:u16][crc:u16]
            header, seq, timestamp, ch0, ch1, adc, crc = struct.unpack("<BBIffHH", data)

            return DataPacket(
                header=header,
                sequence=seq,
                timestamp=timestamp,
                channel0=ch0,
                channel1=ch1,
                internal_adc=adc,
                crc=crc,
            )
        except Exception as e:
            print(f"Packet decode error: {e}")
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
