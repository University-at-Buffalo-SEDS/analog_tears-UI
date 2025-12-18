import csv
import struct

import serial

COM_PORT = 'COM3'
# serial.Serial() used to open and communicate with serial port
ser = serial.Serial(COM_PORT, 115200, timeout=1)  # (COM port name, baud rate, latencey)

filename = "serial_data.csv"
with open(filename, mode='w', newline='') as file:
    writer = csv.writer(file)
    writer.writerow(["Header", "Seq", "Timestamp", "Ch0", "Ch1", "Internal ADC", "CRC"])
    print("Header | Seq | Timestamp | Ch0 | Ch1 | Internal ADC | CRC")
    try:  # runs until interrupted by exception
        while (True):  # while on and running
            # data = ser.read(16) # read 16 byte packet
            data = ser.read(16)
            if (len(data) == 16 and data[0] == 0xAC):  # ensures data is 16 bytes and correct header
                # struct.unpack() converts binary into python values
                header, seq, timestamp = struct.unpack('<BBI', data[:6])  # (format string, data) *read below*
                ch0 = (data[6] << 16) | (data[7] << 8) | data[8]  # bit shift 16 | bit shift 8 | no bit shift
                ch1 = (data[9] << 16) | (data[10] << 8) | data[11]
                adc, crc = struct.unpack('<HH', data[12:16])
                writer.writerow([header, seq, timestamp, ch0, ch1, adc, crc])
                # formatting packet
                print(f"0x{header:02X} | {seq:3d} | {timestamp:8d} | {ch0:7d} | {ch1:7d} | {adc:5d} | {crc:4d}")
                # header: '0x' + hex format | seq: 3 decimal | timestamp: 8 decimal | ch0: 7 decimal | ch1: 7 decimal
                # | adc: 5 decimal | crc: 4 decimal
    except KeyboardInterrupt:
        print("\nStopping...")
        print("\nProgram interrupted by user")
    finally:
        ser.close()  # Always closes the serial port after running
