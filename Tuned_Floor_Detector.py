import time
import numpy as np
from collections import deque
import matplotlib.pyplot as plt
from v1.src.sensing.rssi_collector import WindowsWifiCollector

plt.ion()

class TunedRSSI_Explorer:
    def __init__(self):
        self.collector = WindowsWifiCollector(interface='Wi-Fi', sample_rate_hz=5.0)
        self.rssi_history = deque(maxlen=150)
        self.scores = deque(maxlen=150)
        self.start_time = time.time()

        self.fig, (self.ax1, self.ax2) = plt.subplots(2, 1, figsize=(11, 8))

    def compute_motion_score(self, rssi_vals):
        rssi = np.array(rssi_vals)
        if len(rssi) < 30:
            return 0.0

        # Stronger low-frequency emphasis for floor movement
        detrended = rssi - np.mean(rssi)
        fft_vals = np.abs(np.fft.fft(detrended))
        low_freq = np.sum(fft_vals[1:10]) / len(rssi)

        std_score = min(np.std(rssi) / 2.0, 1.0)
        diff_score = min(np.mean(np.abs(np.diff(rssi))) / 1.0, 1.0)
        freq_score = min(low_freq / 2.5, 1.0)

        return 0.35*std_score + 0.35*diff_score + 0.3*freq_score

    def run(self):
        self.collector.start()
        print('=== Tuned Floor Detector Running ===')
        print('Threshold now adaptive (~0.28+ = movement)\n')

        try:
            while True:
                time.sleep(0.7)
                samples = list(self.collector.get_samples())[-150:]
                if len(samples) < 30:
                    continue

                rssi_list = [s.rssi_dbm for s in samples]
                current = rssi_list[-1]

                score = self.compute_motion_score(rssi_list)
                status = 'FLOOR MOVEMENT' if score > 0.28 else 'STILL'

                print(f'RSSI: {current:6.1f} dBm | Score: {score:.3f} | {status}')

                # Update history
                self.rssi_history.append(current)
                self.scores.append(score)

                # Live plot
                t = np.arange(len(self.rssi_history))
                self.ax1.clear()
                self.ax1.plot(t, list(self.rssi_history), 'b-', label='Raw RSSI')
                self.ax1.set_ylabel('RSSI (dBm)')
                self.ax1.grid(True)
                self.ax1.legend()

                self.ax2.clear()
                self.ax2.plot(t, list(self.scores), 'g-', label='Motion Score')
                self.ax2.axhline(0.28, color='r', linestyle='--', label='Threshold')
                self.ax2.set_ylim(0, 1)
                self.ax2.set_ylabel('Motion Score')
                self.ax2.grid(True)
                self.ax2.legend()

                plt.pause(0.01)

        except KeyboardInterrupt:
            self.collector.stop()
            plt.close()
            print('\nStopped. Good session!')

if __name__ == '__main__':
    exp = TunedRSSI_Explorer()
    exp.run()