from dataclasses import dataclass


@dataclass  # type annotations
class DataPacket:
    header: int
    sequence: int
    timestamp: int
    channel0: float
    channel1: float
    internal_adc: int
    crc: int

    def __init__(self, header=0xAC, sequence=0, timestamp=0,
                 channel0=0.0, channel1=0.0, internal_adc=0, crc=0):
        self.header = header
        self.sequence = sequence
        self.timestamp = timestamp
        self.channel0 = channel0
        self.channel1 = channel1
        self.internal_adc = internal_adc
        self.crc = crc

    # Getters (-> returns expected type)
    def get_header(self) -> int:
        return self.header

    def get_sequence(self) -> int:
        return self.sequence

    def get_timestamp(self) -> int:
        return self.timestamp

    def get_channel0(self) -> float:
        return self.channel0

    def get_channel1(self) -> float:
        return self.channel1

    def get_internal_adc(self) -> int:
        return self.internal_adc

    def get_crc(self) -> int:
        return self.crc

    def get_all_data(self) -> tuple[int, int, int, float, float, int, int]:
        return (self.header, self.sequence, self.timestamp,
                self.channel0, self.channel1, self.internal_adc, self.crc)

    # Setters (w/ type annotations)
    def set_header(self, header: int):
        self.header = header

    def set_sequence(self, sequence: int):
        self.sequence = sequence

    def set_timestamp(self, timestamp: int):
        self.timestamp = timestamp

    def set_channel0(self, channel0: int):
        self.channel0 = channel0

    def set_channel1(self, channel1: int):
        self.channel1 = channel1

    def set_internal_adc(self, internal_adc: int):
        self.internal_adc = internal_adc

    def set_crc(self, crc: int):
        self.crc = crc

    def to_csv_row(self, rx_timestamp) -> list:
        return [rx_timestamp, self.header, self.sequence, self.timestamp,
                self.channel0, self.channel1, self.internal_adc, self.crc]

    def to_display_string(self) -> str:
        return (f"0x{self.header:02X} | {self.sequence:3d} | "
                f"{self.timestamp:8d} | {self.channel0:7d} | "
                f"{self.channel1:7d} | {self.internal_adc:5d} | "
                f"{self.crc:4d}")
