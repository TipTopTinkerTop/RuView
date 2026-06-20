"""
network_scanner.py

Multi-network WiFi scanner for cross-network consensus scoring.

This module polls netsh wlan show networks mode=bssid to see all visible
WiFi networks, not just the one you're connected to. It implements the
"strong + consistent" filtering rule: only networks with reasonably strong
signal that have appeared consistently across recent scans are treated as
tracked sources (not one-off flickers into view).

This is separate from WindowsWifiCollector which uses the simpler
netsh wlan show interfaces call that only returns the connected network.

Design
------
- Poll netsh wlan show networks mode=bssid every SCAN_INTERVAL seconds
- Parse out SSID, BSSID, signal% for each visible network
- Filter by SIGNAL_FLOOR (default -55 dBm, ~70% signal strength)
- Track appearance count; require MIN_CONSECUTIVE_SCANS to qualify as "tracked"
- Returns a list of NetworkInfo dataclasses with BSSID, SSID, signal_dbm, and tracked status
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional, List

logger = logging.getLogger(__name__)


# Configuration
SCAN_INTERVAL = 2.0              # seconds between full network scans
SIGNAL_FLOOR = -70.0             # dBm floor for "strong" networks (~70% signal)
MIN_SIGNAL_RECENT_SCORE = 0.6   # fraction of recent scans with strong signal to qualify
MIN_CONSECUTIVE_SCANS = 3       # number of consecutive scans to require


@dataclass
class NetworkInfo:
    """
    Information about a single visible WiFi network.
    """
    bssid: str                    # e.g. "AA:BB:CC:DD:EE:FF"
    ssid: str                     # SSID name, may be empty for hidden networks
    signal_dbm: float             # signal strength in dBm
    is_strong: bool               # signal >= SIGNAL_FLOOR
    appearance_count: int         # total times seen (across all scans)
    consecutive_strong_scans: int # consecutive scans where this was "strong"
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    tracked: bool = False         # has this qualified as a tracked source?


class NetworkScanner:
    """
    Multi-network WiFi scanner that polls netsh wlan show networks mode=bssid.
    """

    def __init__(
        self,
        scan_interval: float = SCAN_INTERVAL,
        signal_floor: float = SIGNAL_FLOOR,
        min_consecutive_scans: int = MIN_CONSECUTIVE_SCANS,
        min_signal_recent_score: float = MIN_SIGNAL_RECENT_SCORE,
    ):
        self.scan_interval = scan_interval
        self.signal_floor = signal_floor
        self.min_consecutive_scans = min_consecutive_scans
        self.min_signal_recent_score = min_signal_recent_score

        # Store network state across scans
        self._networks: dict[str, NetworkInfo] = {}
        self._last_scan_time = 0.0
        self._last_networks: dict[str, NetworkInfo] = {}  # previous scan's networks
        self._tracked_networks: List[NetworkInfo] = []  # networks that qualify

    def _parse_netsh_output(self, output: str) -> dict[str, NetworkInfo]:
        """
        Parse netsh wlan show networks mode=bssid output.

        Windows netsh output groups multiple lines per network.
        We need to:
        1. Extract all BSSID lines
        2. Extract all SSID lines that follow BSSID lines
        3. Extract all Signal lines
        4. Match them together by network group

        Returns a dict keyed by BSSID (normalized with colons removed).
        """
        networks: dict[str, NetworkInfo] = {}
        bssid_lines = []  # List of (line_index, bssid_raw) tuples

        # First pass: collect all BSSID lines
        for i, line in enumerate(output.splitlines()):
            # Match BSSID: require exactly 6 pairs of hex digits separated by colons
            # Format: "BSSID N : XX:XX:XX:XX:XX:XX"
            # Use word boundary and negative lookbehind/lookahead to avoid partial matches
            bssid_match = re.search(
                r'BSSID\s*\d*\s*:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})',
                line
            )
            if bssid_match:
                bssid_raw = bssid_match.group(1).strip()
                bssid = re.sub(r'[:-]', '', bssid_raw)  # normalize
                bssid_lines.append((i, bssid, bssid_raw))

        # Second pass: for each BSSID line, find the most recent SSID and Signal
        for line_idx, _, bssid in bssid_lines:
            # Find the SSID that comes before this BSSID line (in the same network group)
            # Look backwards from the BSSID line to find the last SSID line
            lines = output.splitlines()
            current_ssid: str | None = None
            for j in range(line_idx - 1, -1, -1):
                prev_line = lines[j]
                ssid_match = re.search(r"SSID\s*\d*\s*:\s*(\S+)", prev_line)
                if ssid_match:
                    ssid_val = ssid_match.group(1)
                    # If there's also a BSSID on this line, it's a new network
                    bssid_in_this_line = re.search(r"BSSID\s*\d*\s*:\s*[0-9A-Fa-f]", prev_line)
                    if bssid_in_this_line:
                        break  # Don't use this SSID, it belongs to a different network
                    current_ssid = ssid_val if ssid_val else "(hidden)"
                    break  # Found the SSID for this network

            # Find the most recent Signal line before the next BSSID line
            signal_dbm = -100.0  # placeholder
            signal_line_idx = line_idx + 1
            while signal_line_idx < len(lines):
                next_line = lines[signal_line_idx]
                if re.search(r"BSSID\s*\d*\s*:", next_line):
                    break  # Next network starting
                signal_match = re.search(r"Signal\s*:\s*(\d+)%?", next_line)
                if signal_match:
                    signal_pct = int(signal_match.group(1))
                    # Empirical conversion: map netsh signal % to dBm
                    # 100% -> -30 dBm (excellent), 0% -> -100 dBm (no signal)
                    signal_dbm = -100.0 + 0.7 * signal_pct
                signal_line_idx += 1

            # Create or update network entry
            if bssid not in networks:
                networks[bssid] = NetworkInfo(
                    bssid=bssid,
                    ssid=current_ssid if current_ssid else "(hidden)",
                    signal_dbm=signal_dbm,
                    is_strong=signal_dbm >= self.signal_floor,
                    appearance_count=0,
                    consecutive_strong_scans=0,
                )
            networks[bssid].last_seen = time.time()

        return networks

    def scan(self) -> List[NetworkInfo]:
        """
        Perform a single network scan and update tracking state.

        Returns a list of networks that have qualified as "tracked" (strong
        and consistent across scans).
        """
        now = time.time()

        # Rate-limit scans
        if now - self._last_scan_time < self.scan_interval:
            return self._tracked_networks

        try:
            result = subprocess.run(
                ["netsh", "wlan", "show", "networks", "mode=bssid"],
                capture_output=True, text=True, timeout=3.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("Network scanner: netsh unavailable (%s)", exc)
            return self._tracked_networks

        if result.returncode != 0:
            logger.warning("netsh returned non-zero: %s", result.stderr)
            return self._tracked_networks

        # Parse this scan
        new_networks = self._parse_netsh_output(result.stdout)

        # Update state: track appearance count and consecutive strong scans
        for bssid, net in new_networks.items():
            if bssid in self._networks:
                old = self._networks[bssid]
                net.appearance_count = old.appearance_count + 1
                if old.consecutive_strong_scans >= self.min_consecutive_scans:
                    net.consecutive_strong_scans = 0  # reset if we missed a scan
                elif net.is_strong:
                    net.consecutive_strong_scans += 1
            else:
                net.appearance_count = 1
                net.consecutive_strong_scans = 1 if net.is_strong else 0

            # Update last_seen
            net.last_seen = now

        # Carry over networks that disappeared from this scan
        for bssid, old_net in self._networks.items():
            if bssid not in new_networks:
                # Network disappeared, reset its streak
                self._networks[bssid].consecutive_strong_scans = 0
            else:
                new_net = new_networks[bssid]
                new_net.appearance_count += old_net.appearance_count
                new_net.consecutive_strong_scans = (
                    old_net.consecutive_strong_scans +
                    new_net.consecutive_strong_scans
                )
                self._networks[bssid] = new_net

        self._networks = new_networks
        self._last_scan_time = now

        # Determine which networks qualify as "tracked"
        self._tracked_networks = [
            net for net in self._networks.values()
            if net.is_strong and net.consecutive_strong_scans >= self.min_consecutive_scans
        ]

        logger.debug(
            "Network scan: %d total networks, %d tracked, "
            "interval=%.1fs",
            len(self._networks),
            len(self._tracked_networks),
            now - self._last_scan_time,
        )

        return self._tracked_networks

    def get_tracked_networks(self) -> List[NetworkInfo]:
        """
        Get the current list of tracked networks without performing a new scan.
        """
        return self._tracked_networks.copy()

    def get_all_networks(self) -> dict[str, NetworkInfo]:
        """
        Get all known networks (tracked and untracked).
        """
        return self._networks.copy()
