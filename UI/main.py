# main.py
from __future__ import annotations

import csv
import sys
import time
from datetime import datetime
from pathlib import Path

from PyQt6 import QtCore, QtWidgets

from gui import MainWindow
from radio import Radio


class RadioWorker(QtCore.QThread):
    """
    Background serial read loop.
    Emits values to GUI and logs to CSV.
    """
    # t_seconds, ch0(float), ch1(float), internal_adc(int)
    sample = QtCore.pyqtSignal(float, float, float, int)
    status = QtCore.pyqtSignal(str)

    def __init__(self, *, com_port: str, csv_filename: str, parent=None):
        super().__init__(parent)
        self._com_port = com_port
        self._csv_filename = csv_filename
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        radio = Radio(port=self._com_port)
        csv_path = Path(self._csv_filename)

        self.status.emit(f"Logging to {csv_path.resolve()}")

        t0 = time.monotonic()
        try:
            with open(csv_path, "w", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["Rx_Timestamp", "Header", "Seq", "Timestamp", "Ch0", "Ch1", "Internal ADC", "CRC"])

                while not self._stop:
                    packet = radio.read_packet()
                    if not packet:
                        self.msleep(1)
                        continue

                    rx_timestamp = datetime.now().isoformat(timespec="milliseconds")

                    # CSV output
                    try:
                        writer.writerow(packet.to_csv_row(rx_timestamp))
                    except Exception as e:
                        self.status.emit(f"CSV write error: {e}")

                    # GUI update
                    try:
                        t = time.monotonic() - t0
                        self.sample.emit(
                            float(t),
                            float(packet.channel0),
                            float(packet.channel1),
                            int(packet.internal_adc),
                        )
                    except Exception as e:
                        self.status.emit(f"Emit error: {e}")

        finally:
            try:
                radio.close()
            except Exception:
                pass
            self.status.emit("Radio closed.")


def main():
    COM_PORT = "DUMMY"
    CSV_FILENAME = "serial_data.csv"

    app = QtWidgets.QApplication(sys.argv)

    # Pass send_command if you want buttons enabled:
    # worker/radio wiring would need a thread-safe command path (we can do that next).
    win = MainWindow(history=2000)
    win.resize(1100, 800)
    win.show()

    worker = RadioWorker(com_port=COM_PORT, csv_filename=CSV_FILENAME)
    worker.sample.connect(win.on_sample)

    worker.status.connect(lambda s: win.setWindowTitle(f"Serial Telemetry Viewer — {s}"))

    app.aboutToQuit.connect(lambda: (worker.stop(), worker.wait(2000)))

    worker.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
