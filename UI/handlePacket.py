import struct
from packet import DataPacket

class PacketHandler:
    PACKET_SIZE = 16
    EXPECTED_HEADER = 0xAC
    
    @staticmethod
    def decode_packet(data: bytes) -> DataPacket:
        try:
            # Check packet size
            if len(data) != PacketHandler.PACKET_SIZE:
                raise ValueError(
                    f"Invalid packet size: {len(data)} bytes, "
                    f"expected {PacketHandler.PACKET_SIZE}"
                )
            
            # Check header
            if data[0] != PacketHandler.EXPECTED_HEADER:
                raise ValueError(
                    f"Invalid header: 0x{data[0]:02X}, "
                    f"expected 0x{PacketHandler.EXPECTED_HEADER:02X}"
                )
            
            # Unpack struct data
            header, seq, timestamp = struct.unpack('<BBI', data[:6])
            
            # Bitshift channel data (24-bit values)
            ch0 = (data[6] << 16) | (data[7] << 8) | data[8]
            ch1 = (data[9] << 16) | (data[10] << 8) | data[11]
            
            # Unpack ADC and CRC
            adc, crc = struct.unpack('<HH', data[12:16])
            
            # Create and return a packet object
            return DataPacket(
                header=header,
                sequence=seq,
                timestamp=timestamp,
                channel0=ch0,
                channel1=ch1,
                internal_adc=adc,
                crc=crc
            )
        
        # if struct unpacking fails 
        except struct.error as e:
            print(f"Struct unpack error: {e}")
            return None
        
        # if validation fails
        except ValueError as e:
            print(f"Packet validation error: {e}")
            return None
        
        # if data is too short 
        except IndexError as e:
            print(f"Index error during decoding: {e}")
            return None
        
        # if an unexpected error occurs
        except Exception as e:
            print(f"Unexpected error during packet decoding: {e}")
            return None
    
    @staticmethod
    def encode_packet(packet: DataPacket) -> bytes:
        data = bytearray(PacketHandler.PACKET_SIZE)
        
        # Pack struct data
        data[0:6] = struct.pack('<BBI', 
                               packet.get_header(), 
                               packet.get_sequence(), 
                               packet.get_timestamp())
        
        # Pack channel data (24-bit values)
        data[6] = (packet.get_channel0() >> 16) & 0xFF
        data[7] = (packet.get_channel0() >> 8) & 0xFF
        data[8] = packet.get_channel0() & 0xFF
        
        data[9] = (packet.get_channel1() >> 16) & 0xFF
        data[10] = (packet.get_channel1() >> 8) & 0xFF
        data[11] = packet.get_channel1() & 0xFF
        
        # Pack ADC and CRC
        data[12:16] = struct.pack('<HH', 
                                 packet.get_internal_adc(), 
                                 packet.get_crc())
        
        return bytes(data)
    
    @staticmethod
    def is_valid_packet(data: bytes) -> bool:
        return (len(data) == PacketHandler.PACKET_SIZE and 
                data[0] == PacketHandler.EXPECTED_HEADER)