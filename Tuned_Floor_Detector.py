import time
import numpy as np
from collections import deque
import matplotlib.pyplot as plt
from matplotlib.widgets import RadioButtons
from v1.src.sensing.rssi_collector import WindowsWifiCollector
from router_profile import RouterProfileStore, get_current_ssid
from network_scanner import NetworkScanner

plt.ion()

# Single-network mode settings
SSID_POLL_INTERVAL = 5.0   # seconds -- SSID doesn't change every sample, no need to shell out constantly
UNKNOWN_SSID_KEY = "(unknown network)"

# Multi-network mode settings
MULTI_NETWORK_MODE = False  # Set to True to enable multi-network sensing
NETWORK_SCAN_INTERVAL = 2.0  # seconds between full network scans
MIN_TRACKED_NETWORKS = 2  # minimum networks needed for combined verdict


class TunedRSSI_Explorer:
    def __init__(self):
        self.collector = WindowsWifiCollector(interface='Wi-Fi', sample_rate_hz=5.0)
        self.rssi_history = deque(maxlen=150)
        self.scores = deque(maxlen=150)
        self.start_time = time.time()

        self.store = RouterProfileStore(path="router_profiles.json")
        self.active_ssid = None
        self.active_profile = None
        self._last_ssid_check = 0.0

        # Multi-network scanner (only active if MULTI_NETWORK_MODE).
        # signal_floor_percent uses netsh's own 0-100% signal reading
        # directly -- no invented dBm conversion (Windows doesn't expose
        # true dBm for networks you're not connected to).
        self.network_scanner = NetworkScanner(
            scan_interval=NETWORK_SCAN_INTERVAL,
            signal_floor_percent=35.0,
            min_consecutive_scans=3,
        ) if MULTI_NETWORK_MODE else None
        self.tracked_networks = []  # List of (bssid, profile) tuples
        self._pending_label = None  # Pending label for new network

        self.fig, (self.ax1, self.ax2) = plt.subplots(2, 1, figsize=(11, 8))
        # More room on the right for both router selector and network breakdown
        if MULTI_NETWORK_MODE:
            self.fig.subplots_adjust(right=0.90)
            # Add third axis for network breakdown
            self.ax3 = self.fig.add_axes([0.93, 0.15, 0.05, 0.7])
        else:
            self.fig.subplots_adjust(right=0.78)

        # Side panel: lets you browse other learned router profiles without
        # affecting which one is actively learning (that's always whichever
        # SSID the adapter is actually connected to).
        self.ax_radio = self.fig.add_axes([0.81, 0.5, 0.17, 0.4])
        self.ax_radio.set_title('Saved routers', fontsize=9)
        self.radio = None
        self.viewing_ssid = None  # which profile's stats are shown in the info box
        self._rebuild_radio_panel()

        # Info box (active router, sample count, live threshold)
        self.ax_info = self.fig.add_axes([0.81, 0.08, 0.17, 0.35])
        self.ax_info.axis('off')
        self.info_text = self.ax_info.text(
            0, 1, '', va='top', ha='left', fontsize=8.5, family='monospace'
        )

    # -- SSID / profile switching --------------------------------------------

    def _refresh_active_profile(self):
        """Poll the current SSID periodically and switch the active learning
        profile if the network has changed (e.g. laptop moved to another
        room/router)."""
        now = time.time()
        if now - self._last_ssid_check < SSID_POLL_INTERVAL and self.active_profile is not None:
            return
        self._last_ssid_check = now

        ssid = get_current_ssid() or UNKNOWN_SSID_KEY
        if ssid != self.active_ssid:
            self.active_ssid = ssid
            self.active_profile = self.store.get_or_create(ssid)
            if self.viewing_ssid is None:
                self.viewing_ssid = ssid
            self._rebuild_radio_panel()
            print(f"[router] Active profile switched to '{ssid}' "
                  f"(learned samples: {self.active_profile.sample_count})")

    # -- UI: router selector --------------------------------------------------

    def _rebuild_radio_panel(self):
        """Rebuild the RadioButtons widget from currently known SSIDs."""
        self.ax_radio.clear()
        self.ax_radio.set_title('Saved routers', fontsize=9)

        ssids = self.store.known_ssids()
        if self.active_ssid and self.active_ssid not in ssids:
            ssids = sorted(ssids + [self.active_ssid])
        if not ssids:
            ssids = [UNKNOWN_SSID_KEY]

        active_index = ssids.index(self.viewing_ssid) if self.viewing_ssid in ssids else 0
        self.radio = RadioButtons(self.ax_radio, ssids, active=active_index)
        for label in self.radio.labels:
            label.set_fontsize(7)
        self.radio.on_clicked(self._on_router_selected)

    def _on_router_selected(self, label):
        """User clicked a router in the side panel -- just changes which
        profile's stats are displayed in the info box. Does NOT change
        which profile is actively learning (that's tied to the real SSID)."""
        self.viewing_ssid = label

    # -- motion scoring (unchanged from original) ----------------------------

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

    # -- info panel text -------------------------------------------------

    def _compute_combined_score(self) -> float:
        """
        Compute the combined motion score from all tracked networks.
        Uses mean of individual scores.
        """
        if not self.tracked_networks:
            return 0.0
        return np.mean([profile.mean_score for _, profile in self.tracked_networks])

    def _compute_combined_threshold(self) -> float:
        """
        Compute the combined threshold from all tracked networks.
        Uses the minimum threshold (most sensitive network).
        """
        if not self.tracked_networks:
            return 0.6  # High threshold if no networks
        return min([profile.threshold for _, profile in self.tracked_networks])

    def _update_info_text(self, current_threshold: float, multi_mode: bool = False):
        """
        Update the info text panel.
        """
        viewed = self.store.get(self.viewing_ssid) if self.viewing_ssid else None
        lines = []

        if MULTI_NETWORK_MODE:
            # Multi-network mode info
            lines.append("=== Multi-Network Mode ===")
            lines.append(f"Combined Score: {self._compute_combined_score():.3f}")
            lines.append(f"Combined Threshold: {self._compute_combined_threshold():.3f}")
            lines.append(f"Tracked Networks: {len(self.tracked_networks)}")
            lines.append("")
            lines.append("Active: " + (self.active_ssid or '...'))
            lines.append(f"Threshold: {current_threshold:.3f}")
            lines.append("")
            lines.append("Network Breakdown:")

            # Add per-network info
            for bssid, profile in self.tracked_networks:
                net_status = "TRACKED" if (bssid, profile) in self.tracked_networks else "LEARNING"
                lines.append(f"  {profile.ssid or '(hidden)'} "
                             f"[{profile.label or '(unknown)'}] "
                             f"Score: {profile.mean_score:.3f}/thresh: {profile.threshold:.3f} "
                             f"{net_status}")
        else:
            # Single-network mode info
            lines = [
                f"Active: {self.active_ssid or '...'}",
                f"Threshold: {current_threshold:.3f}",
                "",
                f"Viewing: {self.viewing_ssid or '-'}",
            ]

        if viewed:
            lines += [
                f"  samples:   {viewed.sample_count}",
                f"  mean:      {viewed.mean_score:.3f}",
                f"  std:       {viewed.std_score:.3f}",
                f"  threshold: {viewed.threshold:.3f}",
            ]
        else:
            lines.append("  (no data yet)")

        self.info_text.set_text("\n".join(lines))

    def run(self):
        self.collector.start()
        print('=== Tuned Floor Detector Running (per-router adaptive) ===')
        print('Threshold adapts per-router based on learned quiet baseline.\n')

        try:
            while True:
                time.sleep(0.7)

                self._refresh_active_profile()
                profile = self.active_profile

                samples = list(self.collector.get_samples())[-150:]
                if len(samples) < 30:
                    continue

                rssi_list = [s.rssi_dbm for s in samples]
                current = rssi_list[-1]

                score = self.compute_motion_score(rssi_list)
                threshold = profile.threshold
                status = 'FLOOR MOVEMENT' if score > threshold else 'STILL'

                # Learn from this sample only if it's judged still --
                # movement is never allowed to pollute the quiet baseline.
                profile.update_if_still(score)
                self.store.save()  # internally rate-limited, cheap to call every loop

                print(f'RSSI: {current:6.1f} dBm | Score: {score:.3f} | '
                      f'Threshold: {threshold:.3f} | {status} | [{self.active_ssid}]')

                # Update history
                self.rssi_history.append(current)
                self.scores.append(score)

                # Live plot
                t = np.arange(len(self.rssi_history))
                self.ax1.clear()
                self.ax1.plot(t, list(self.rssi_history), 'b-', label='Raw RSSI')
                self.ax1.set_ylabel('RSSI (dBm)')
                self.ax1.set_title(f"Router: {self.active_ssid}")
                self.ax1.grid(True)
                self.ax1.legend()

                self.ax2.clear()
                self.ax2.plot(t, list(self.scores), 'g-', label='Motion Score')
                self.ax2.axhline(threshold, color='r', linestyle='--',
                                  label=f'Adaptive Threshold ({threshold:.2f})')
                self.ax2.set_ylim(0, 1)
                self.ax2.set_ylabel('Motion Score')
                self.ax2.grid(True)
                self.ax2.legend()

                self._update_info_text(threshold)

                plt.pause(0.01)

        except KeyboardInterrupt:
            self.collector.stop()
            self.store.save(force=True)
            plt.close()
            print('\nStopped. Profiles saved. Good session!')

if __name__ == '__main__':
    exp = TunedRSSI_Explorer()
    exp.run()
