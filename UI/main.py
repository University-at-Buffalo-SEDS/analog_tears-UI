# modeled main.py off of ui.py, 
import csv
from datetime import datetime
from radio import Radio

def main():
    COM_PORT = 'COM3' 
    CSV_FILENAME = "serial_data.csv"
    radio = Radio(port=COM_PORT)
    
    try:
        with open(CSV_FILENAME, 'w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(["Rx_Timestamp", "Header", "Seq", "Timestamp", "Ch0", "Ch1", "Internal ADC", "CRC"])
            print("Rx_Timestamp | Header | Seq | Timestamp | Ch0 | Ch1 | Internal ADC | CRC")
            
            try:
                while True:
                    packet = radio.read_packet()
                    if packet:
                        # Add timestamp to CSV
                        rx_timestamp = datetime.now().isoformat(timespec='milliseconds')
                        writer.writerow(packet.to_csv_row(rx_timestamp))
                        # Display packet
                        print(f"{rx_timestamp} | " + packet.to_display_string())
                            
            except KeyboardInterrupt:
                print("\nStopping...")
                print("\nProgram interrupted by user")
                
    finally:
        radio.close()
        print("Program ended")

if __name__ == "__main__":
    main()