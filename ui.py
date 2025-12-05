import struct
import serial


# serial.Serial() used to open and communicate with serial port
with serial.Serial('COM3', 115200, timeout=1) as ser: # (COM port name, baud rate, latencey)
    data = ser.read(16)
print("Header | Seq | Timestamp | Ch0 | Ch1 | Internal ADC | CRC")

while (True): # while on and running
    #data = ser.read(16) # read 16 byte packet
    if (len(data) == 16 and data[0] == 0xAC): # ensures data is 16 bytes and correct header
        # struct.unpack() converts binary into python values
        header, seq, timestamp = struct.unpack('<BBI', data[:6]) # (format string, data) *read below*
        ch0 = (data[6]<<16) | (data[7]<<8) | data[8] # bit shift 16 | bit shift 8 | no bit shift 
        ch1 = (data[9]<<16) | (data[10]<<8) | data[11] # ^^^
        adc, crc = struct.unpack('<HH', data[12:16]) # ^^^
        # formatting packet
        print(f"0x{header:02X} | {seq:3d} | {timestamp:8d} | {ch0:7d} | {ch1:7d} | {adc:5d} | {crc:4d}")
        # header: '0x' + hex format | seq: 3 decimal | timestamp: 8 decimal | ch0: 7 decimal | ch1: 7 decimal | adc: 5 decimal | crc: 4 decimal
       

# Format String
# '<'  ~ read LSB first
# 'B'  ~ uint8_t header (1B) --> C: unsigned char --> Python: integer
# 'B'  ~ uint8_t sequence (1B) --> C: unsigned char --> Python: integer
# 'I'  ~ uint32_t timestamp (4B) --> C: unsigned int --> Python: integer
#      ~ uint8_t ad7193_data[2][3] 
# 'H'  ~ uint16_t stm32_adc (2B) --> C: unsigned short --> Python: integer
# 'H'  ~ uint16_t crc (2B) --> C: unsigned short --> Python: integer

# Byte:   0    1    2    3    4    5    6    7    8    9   10   11   12   13   14   15
#        ┌────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┐
# Data:  │ AC │Seq │  Timestamp (4B)   │Ch0 MSB│Ch0 │Ch0 │Ch1 MSB│Ch1 │Ch1 │ADC LSB│ADC MSB│CRC LSB│CRC MSB│
#        └────┴────┴────┴────┴────┴────┴────┴────┴────┴────┴────┴────┴────┴────┴────┴────┘
#        │<───────────── 6 bytes ────────────>│<────── 6 bytes ─────>│< 2 bytes >│< 2 bytes >│
#        │  struct.unpack('<BBI')             │ 24-bit channels      │ internal  │ CRC-16    │
#        │  header, seq, timestamp            │ ch0, ch1             │ ADC       │ checksum  │
