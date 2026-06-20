"""
router_profile.py

Per-router (per-SSID or per-BSSID) adaptive baseline learning for the floor
motion detector addon.

This module is intentionally standalone: it does NOT modify
v1/src/sensing/rssi_collector.py. WifiSample objects from that module carry
no router identity (no BSSID/SSID field), so this module captures the
currently-connected SSID separately via the same `netsh wlan show
interfaces` call the collector already uses internally.

Design
------
- One JSON file on disk (default: router_profiles.json) holds profiles
  keyed by SSID or BSSID (e.g. "MyRouter" or "40b076235450").
- Each profile tracks a running mean/std of the motion score observed
  while the detector judges the room "still" (EMA-based, slow-moving).
- The adaptive threshold for a profile is derived from its learned
  baseline: threshold = mean_score + k * std_score, clamped to a sane
  range so it can never become unusably low/high while a profile is new
  or has too little data.
- A profile is ONLY updated when the score at the time of observation is
  below the *current* threshold -- i.e. movement never pollutes the
  "quiet" baseline.
- Only one profile is "active" at a time, matching the reality of a
  normal WiFi adapter only being associated with one SSID at once. The
  caller is expected to poll get_current_ssid() periodically and switch
  the active profile when it changes (e.g. moving the laptop to a
  different room/router).
- Profiles can now have an optional location label for cross-network
  consensus scoring (e.g. "kitchen", "bedroom", "neighbor-unknown").
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, List, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSID lookup (Windows only, mirrors WindowsWifiCollector's netsh usage)
# ---------------------------------------------------------------------------

def get_current_ssid(timeout: float = 5.0) -> Optional[str]:
    """
    Return the SSID of the currently-connected WiFi network, or None if it
    can't be determined (not connected, netsh missing, parse failure).

    Uses ``netsh wlan show interfaces`` -- the same command
    WindowsWifiCollector already relies on -- so no new OS dependency is
    introduced.
    """
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("get_current_ssid: netsh unavailable (%s)", exc)
        return None

    for line in result.stdout.splitlines():
        stripped = line.strip()
        # Match "SSID" but not "BSSID" (BSSID line also contains "SSID").
        if re.match(r"^SSID\s*\d*\s*:", stripped, re.IGNORECASE):
            try:
                ssid = stripped.split(":", 1)[1].strip()
                return ssid if ssid else None
            except IndexError:
                return None
    return None


# ---------------------------------------------------------------------------
# Per-router profile
# ---------------------------------------------------------------------------

# Defaults chosen to match the original script's fixed 0.28 threshold for a
# brand-new profile with no learned data yet, then adapt from there.
DEFAULT_MEAN_SCORE = 0.10
DEFAULT_STD_SCORE = 0.06
THRESHOLD_K = 3.0          # threshold = mean + K * std
THRESHOLD_MIN = 0.15       # never let the threshold get unusably sensitive
THRESHOLD_MAX = 0.60       # never let the threshold get unusably insensitive
EMA_ALPHA = 0.02           # slow adaptation: ~50 "still" samples to mostly converge
MIN_SAMPLES_FOR_ADAPT_DISPLAY = 20  # cosmetic: when to call a profile "learned"


@dataclass
class RouterProfile:
    """
    Profile for a single WiFi router/network (identified by SSID or BSSID).

    For multi-network mode, profiles are keyed by BSSID (e.g. "40b076235450")
    and can have an optional location label (e.g. "kitchen", "bedroom").
    """
    ssid: str                      # SSID name (for single-network mode) or empty for BSSID-only
    bssid: str = ""                 # BSSID (empty for backwards compat)
    label: str = ""                 # Optional location label (e.g. "kitchen")
    mean_score: float = DEFAULT_MEAN_SCORE
    std_score: float = DEFAULT_STD_SCORE
    sample_count: int = 0
    last_updated: float = field(default_factory=time.time)

    @property
    def threshold(self) -> float:
        raw = self.mean_score + THRESHOLD_K * self.std_score
        return min(THRESHOLD_MAX, max(THRESHOLD_MIN, raw))

    def update_if_still(self, score: float) -> bool:
        """
        Update the running baseline with `score`, but ONLY if it's below
        the profile's current threshold (i.e. the room is judged still).
        Returns True if the baseline was updated.
        """
        if score >= self.threshold:
            return False

        # EMA update of mean
        delta = score - self.mean_score
        self.mean_score += EMA_ALPHA * delta

        # EMA update of std (Welford-ish online approximation)
        self.std_score = max(
            0.01,
            (1 - EMA_ALPHA) * self.std_score + EMA_ALPHA * abs(delta),
        )

        self.sample_count += 1
        self.last_updated = time.time()
        return True

    def to_dict(self) -> dict:
        d = asdict(self)
        # Only include fields that are not default/empty
        if self.bssid:
            d["bssid"] = self.bssid
        if self.label:
            d["label"] = self.label
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "RouterProfile":
        # Handle both SSID-keyed and BSSID-keyed profiles
        ssid = data.get("ssid", "")
        bssid = data.get("bssid", "")
        label = data.get("label", "")

        # If both SSID and BSSID are present, prefer BSSID for multi-network
        if bssid:
            return cls(
                ssid=ssid,
                bssid=bssid,
                label=label,
                mean_score=data.get("mean_score", DEFAULT_MEAN_SCORE),
                std_score=data.get("std_score", DEFAULT_STD_SCORE),
                sample_count=data.get("sample_count", 0),
                last_updated=data.get("last_updated", time.time()),
            )
        else:
            return cls(
                ssid=ssid,
                mean_score=data.get("mean_score", DEFAULT_MEAN_SCORE),
                std_score=data.get("std_score", DEFAULT_STD_SCORE),
                sample_count=data.get("sample_count", 0),
                last_updated=data.get("last_updated", time.time()),
            )


# ---------------------------------------------------------------------------
# Profile store (load/save all known router profiles)
# ---------------------------------------------------------------------------

class RouterProfileStore:
    """
    Holds all known RouterProfiles, keyed by SSID, persisted to a single
    JSON file. One profile is "active" (the one currently learning, tied
    to whichever SSID the adapter is connected to).
    """

    def __init__(self, path: str = "router_profiles.json", autosave_interval: float = 10.0):
        self.path = path
        self.autosave_interval = autosave_interval
        self._profiles: Dict[str, RouterProfile] = {}
        self._last_save = 0.0
        self.load()

    # -- persistence -----------------------------------------------------

    def load(self) -> None:
        if not os.path.exists(self.path):
            self._profiles = {}
            return
        try:
            with open(self.path, "r") as f:
                raw = json.load(f)
            self._profiles = {
                ssid: RouterProfile.from_dict(data) for ssid, data in raw.items()
            }
            logger.info("Loaded %d router profile(s) from %s", len(self._profiles), self.path)
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("Could not load %s (%s) -- starting fresh", self.path, exc)
            self._profiles = {}

    def save(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_save) < self.autosave_interval:
            return
        try:
            tmp_path = self.path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(
                    {ssid: p.to_dict() for ssid, p in self._profiles.items()},
                    f, indent=2,
                )
            os.replace(tmp_path, self.path)  # atomic on Windows + POSIX
            self._last_save = now
        except OSError as exc:
            logger.warning("Could not save %s (%s)", self.path, exc)

    # -- profile access ----------------------------------------------------

    def get_or_create(self, key: str) -> RouterProfile:
        """
        Get or create a profile for the given key (SSID or BSSID).

        In multi-network mode, use BSSID keys (e.g. "40b076235450").
        In single-network mode, use SSID keys.
        """
        if key not in self._profiles:
            logger.info("New router profile created for key '%s'", key)
            self._profiles[key] = RouterProfile(ssid=key)
        return self._profiles[key]

    def get_or_create_with_label(
        self,
        bssid: str,
        ssid: str = "",
        label: str = "",
    ) -> RouterProfile:
        """
        Create a new profile with BSSID and optional label.
        """
        profile = RouterProfile(
            ssid=ssid,
            bssid=bssid,
            label=label,
        )
        self._profiles[bssid] = profile
        logger.info(
            "New multi-network profile created for BSSID '%s' "
            "(label: %s)",
            bssid,
            label,
        )
        return profile

    def set_label(self, bssid: str, label: str) -> bool:
        """
        Set the location label for a tracked network.
        """
        profile = self._profiles.get(bssid)
        if profile:
            profile.label = label
            profile.last_updated = time.time()
            return True
        logger.warning("No profile found for BSSID '%s'", bssid)
        return False

    def get_label(self, bssid: str) -> str:
        """
        Get the location label for a tracked network.
        """
        profile = self._profiles.get(bssid)
        return profile.label if profile else ""

    def known_ssids(self) -> List[str]:
        """
        Return all known keys (SSIDs for single-network, BSSIDs for multi-network).
        """
        return sorted(self._profiles.keys())

    def known_bssids(self) -> List[str]:
        """
        Return all BSSID-keyed profiles (for multi-network mode).
        """
        return sorted([
            key for key, profile in self._profiles.items()
            if profile.bssid
        ])

    def get(self, key: str) -> Optional[RouterProfile]:
        return self._profiles.get(key)

    def get_all_profiles(self) -> Dict[str, RouterProfile]:
        """
        Return a copy of all profiles.
        """
        return self._profiles.copy()
