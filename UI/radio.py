import serial
from handlePacket import PacketHandler

class Radio:
    # default parameters unless specified otherwise
    def __init__(self, port='COM3', baudrate=115200):
        self.ser = serial.Serial(port, baudrate, timeout=1)
    
    def read_packet(self):
        if not self.is_connected():
            raise ConnectionError("Not connected to serial port")
        
        try:
            # Read 16 bytes
            data = self.ser.read(PacketHandler.PACKET_SIZE)
            
            if len(data) == 0:
                # Timeout occurred
                return None
                
            if len(data) < PacketHandler.PACKET_SIZE:
                # Incomplete data
                print(f"Warning: Incomplete packet ({len(data)} bytes)")
                return None
            
            # Decoding packet
            packet = PacketHandler.decode_packet(data)
            return packet
        
        # if reading from serial port fails
        except Exception as e:
            print(f"Error reading from serial port: {e}")
            return None
    
    def close(self):
        if self.ser.is_open:
            self.ser.close()