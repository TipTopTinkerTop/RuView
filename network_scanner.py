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
- Filter by SIGNAL_FLOOR_PERCENT (default 35%, the percentage netsh
  actually reports -- no invented dBm conversion. Windows does not expose
  true dBm for networks you're not connected to, so working in the native
  percent unit avoids manufacturing false precision.)
- Track appearance count; require MIN_CONSECUTIVE_SCANS to qualify as "tracked"
- Returns a list of NetworkInfo dataclasses with BSSID, SSID, signal_percent,
  and tracked status
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)


# Configuration
SCAN_INTERVAL = 2.0              # seconds between full network scans
SIGNAL_FLOOR_PERCENT = 35.0      # netsh's own 0-100% signal reading; skip weaker networks
MIN_CONSECUTIVE_SCANS = 3        # number of consecutive qualifying scans to require


@dataclass
class NetworkInfo:
    """
    Information about a single visible WiFi network.
    """
    bssid: str                    # normalized lowercase, no separators, e.g. "40b076235450"
    ssid: str                     # SSID name, "(hidden)" if none reported
    signal_percent: float         # 0-100, exactly as netsh reports it
    is_strong: bool                # signal_percent >= SIGNAL_FLOOR_PERCENT
    appearance_count: int         # total scans this BSSID has appeared in
    consecutive_strong_scans: int  # consecutive scans where this was "strong"
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    # Set in exactly one place (NetworkScanner.scan(), after computing
    # consecutive_strong_scans) -- never computed redundantly elsewhere,
    # so it can't drift out of sync with the scanner's own qualification rule.
    tracked: bool = False


class NetworkScanner:
    """
    Multi-network WiFi scanner that polls netsh wlan show networks mode=bssid.
    """

    def __init__(
        self,
        scan_interval: float = SCAN_INTERVAL,
        signal_floor_percent: float = SIGNAL_FLOOR_PERCENT,
        min_consecutive_scans: int = MIN_CONSECUTIVE_SCANS,
    ):
        self.scan_interval = scan_interval
        self.signal_floor_percent = signal_floor_percent
        self.min_consecutive_scans = min_consecutive_scans

        # Single source of truth for all per-BSSID state across scans.
        self._networks: dict[str, NetworkInfo] = {}
        self._last_scan_time = 0.0
        self._tracked_networks: List[NetworkInfo] = []

    def _parse_netsh_output(self, output: str) -> "dict[str, tuple[str, float]]":
        """
        Parse netsh wlan show networks mode=bssid output.

        Single forward pass: track the current SSID as we go, and emit one
        (bssid -> (ssid, signal_percent)) entry per BSSID line, taking that
        BSSID's nearest following "Signal : NN%" line. This is simpler and
        less error-prone than scanning backwards/forwards from each BSSID
        line independently.

        Returns a dict keyed by the NORMALIZED bssid (lowercase, no
        separators) -> (ssid, signal_percent).
        """
        parsed: "dict[str, tuple[str, float]]" = {}
        current_ssid = "(hidden)"
        pending_bssid: "str | None" = None

        for line in output.splitlines():
            stripped = line.strip()

            ssid_match = re.match(r"^SSID\s+\d+\s*:\s*(.*)$", stripped, re.IGNORECASE)
            if ssid_match:
                current_ssid = ssid_match.group(1).strip() or "(hidden)"
                pending_bssid = None
                continue

            bssid_match = re.match(
                r"^BSSID\s+\d+\s*:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})",
                stripped, re.IGNORECASE,
            )
            if bssid_match:
                bssid_raw = bssid_match.group(1)
                pending_bssid = re.sub(r"[:-]", "", bssid_raw).lower()
                continue

            signal_match = re.match(r"^Signal\s*:\s*(\d+)\s*%", stripped, re.IGNORECASE)
            if signal_match and pending_bssid is not None:
                signal_percent = float(signal_match.group(1))
                parsed[pending_bssid] = (current_ssid, signal_percent)
                pending_bssid = None  # this BSSID is fully resolved
                continue

        return parsed

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
                capture_output=True, text=True, timeout=8.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("Network scanner: netsh unavailable (%s)", exc)
            return self._tracked_networks

        if result.returncode != 0:
            logger.warning("netsh returned non-zero: %s", result.stderr)
            return self._tracked_networks

        self._last_scan_time = now
        parsed = self._parse_netsh_output(result.stdout)

        # Single consolidated pass: for every BSSID seen THIS scan, compute
        # its new state from its OLD state (or fresh, if new) exactly once.
        # No second loop re-touches these fields -- that double-update is
        # what caused the old appearance_count/streak inflation bug.
        updated: dict[str, NetworkInfo] = {}
        for bssid, (ssid, signal_percent) in parsed.items():
            is_strong = signal_percent >= self.signal_floor_percent
            old = self._networks.get(bssid)

            if old is None:
                appearance_count = 1
                consecutive_strong_scans = 1 if is_strong else 0
                first_seen = now
            else:
                appearance_count = old.appearance_count + 1
                consecutive_strong_scans = (
                    old.consecutive_strong_scans + 1 if is_strong else 0
                )
                first_seen = old.first_seen

            updated[bssid] = NetworkInfo(
                bssid=bssid,
                ssid=ssid,
                signal_percent=signal_percent,
                is_strong=is_strong,
                appearance_count=appearance_count,
                consecutive_strong_scans=consecutive_strong_scans,
                first_seen=first_seen,
                last_seen=now,
                tracked=consecutive_strong_scans >= self.min_consecutive_scans,
            )

        # BSSIDs that were known before but didn't show up this scan: keep
        # them (so labels/history aren't lost on a transient miss) but reset
        # their streak, since "stable" means consecutive, not cumulative.
        for bssid, old in self._networks.items():
            if bssid not in updated:
                old.consecutive_strong_scans = 0
                old.tracked = False
                updated[bssid] = old

        self._networks = updated
        self._tracked_networks = [n for n in self._networks.values() if n.tracked]

        logger.debug(
            "Network scan: %d total networks, %d tracked",
            len(self._networks), len(self._tracked_networks),
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
