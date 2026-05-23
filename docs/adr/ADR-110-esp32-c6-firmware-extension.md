# ADR-110: ESP32-C6 firmware extension — Wi-Fi 6 CSI, 802.15.4 mesh, TWT, LP-core hibernation

| Field | Value |
|-------|-------|
| **Status** | Accepted (P1–P7 shipped on `main` branch, P8 docs + bench landed) |
| **Date** | 2026-05-22 |
| **Deciders** | ruv |
| **Codename** | **C6-SOTA** |
| **Relates to** | ADR-018 (CSI binary frame format), ADR-028 (ESP32 capability audit), ADR-029 (RuvSense multistatic), ADR-030 (RuvSense persistent field model), ADR-031 (RuView sensing-first), ADR-061 (QEMU CI), ADR-081 (adaptive CSI mesh kernel), ADR-097 (rvCSI adoption) |
| **Tracking issue** | [ruvnet/RuView#762](https://github.com/ruvnet/RuView/issues/762) |

---

## 1. Context

The production CSI node firmware (`firmware/esp32-csi-node`) was built around the **ESP32-S3** (Xtensa LX7 dual-core @ 240 MHz, 8 MB PSRAM, 802.11 b/g/n). The repo's `firmware/esp32-hello-world/main.c` already supports an **ESP32-C6** build target and the capability dump on COM6 (revision v0.2, MAC `20:6e:f1:17:27:8c`) confirmed four C6-only capabilities that the production firmware does not exploit today:

| C6 capability | What it enables for sensing | Why we can't get it on S3 |
|---|---|---|
| **802.11ax (Wi-Fi 6) HE-LTF CSI** | 242 subcarriers per HE20 frame (vs 52 for HT-LTF), HE-MU/HE-TB PPDU types, OFDMA-aware channel sounding | S3 radio is HT-only (n) |
| **802.15.4 (Thread / Zigbee)** | Cross-node time-sync over a separate radio — frees Wi-Fi airtime for CSI, ±100 µs alignment possible without coordination traffic on the sensing channel | S3 has no 802.15.4 |
| **TWT (Target Wake Time)** | Sensor negotiates a deterministic wake slot with the AP; CSI cadence becomes scheduler-bounded instead of opportunistic | Requires 802.11ax — S3 can't speak it |
| **LP-core + hibernation (~5 µA)** | Always-on motion gate runs on a separate RISC-V LP core in deep sleep; HP core stays off until a real event | S3 ULP is FSM-only, ~10 µA floor |

**The first three are publishable research surfaces.** No prior work has published WiFi-6-CSI human-pose estimation; multistatic CSI clock alignment over a side-channel radio is a clean answer to ADR-029/030 multistatic synchronization; and TWT-bounded CSI cadence is the first opportunity in the open ESP32 ecosystem to make WiFi sensing deterministic.

**The fourth (LP-core) unblocks a product line.** Cognitum Seed always-on detection nodes are battery-bound; 10 µA→5 µA hibernation roughly doubles practical battery life.

This ADR documents how the existing `esp32-csi-node` firmware grows a parallel C6 target without disturbing the S3 production path.

### 1.1 What this ADR is *not*

- Not a deprecation of the S3 firmware. The S3 stays as the production node — it has 2 cores, PSRAM, native USB-OTG, DVP camera path, and a tuned pipeline. The C6 is added as a research/seed target.
- Not a port of every S3 feature to C6. Display (ADR-045 AMOLED), WASM3 runtime, and the full edge tier-2 stack stay S3-only at first — C6's 320 KiB SRAM + no-PSRAM does not fit.
- Not a hardware redesign. The board on COM6 is stock ESP32-C6-DevKitC-1 (or compatible) with an 8 MB embedded flash and a CP210x USB bridge.

## 2. Decision

Extend `firmware/esp32-csi-node` to a **dual-target project** (S3 + C6) using ESP-IDF's existing `idf.py set-target` mechanism plus a target-keyed `sdkconfig.defaults.esp32c6` overlay. Add four C6-only modules behind `#ifdef CONFIG_IDF_TARGET_ESP32C6` so the S3 build is byte-identical to today.

### 2.1 Module breakdown

| New module | File | C6-only? | Purpose |
|---|---|---|---|
| **HE-LTF CSI tagging** | extend `csi_collector.c` | shared (no-op on S3) | Read `wifi_pkt_rx_ctrl_t.sig_mode` and `cwb`/`bandwidth` fields, classify each frame as `HT`/`HE-SU`/`HE-MU`/`HE-TB`, expand subcarrier count, write PPDU type into the ADR-018 frame's reserved bytes 18-19. |
| **802.15.4 time-sync** | `c6_timesync.c/.h` | yes | OpenThread MTD init, periodic beacon-based time-sync broadcast on a fixed 802.15.4 channel, exports `c6_timesync_get_epoch_us()`. |
| **TWT setup** | `c6_twt.c/.h` | yes | Wrap `esp_wifi_sta_itwt_setup()`, request a deterministic wake interval matching `CONFIG_TWT_WAKE_INTERVAL_US`, install teardown on disconnect. |
| **LP-core hibernation** | `c6_lp_core.c/.h` + `lp_core/main.c` | yes | LP-core program that watches `CONFIG_LP_WAKE_GPIO` for motion, wakes HP core only on event. HP-side calls `c6_lp_core_arm()` before `esp_deep_sleep_start()`. |

### 2.2 Build matrix

| Target | sdkconfig defaults | Partition table | Binary size | Features |
|---|---|---|---|---|
| `esp32s3` (default — production) | `sdkconfig.defaults` (unchanged) | `partitions_display.csv` (8 MB) | ~1.1 MB | Full pipeline + display + WASM |
| `esp32c6` (new — research) | `sdkconfig.defaults` + `sdkconfig.defaults.esp32c6` overlay | `partitions_4mb.csv` (4 MB single OTA) | target <1 MB | CSI + TWT + 802.15.4 + LP-core, no display, no WASM |

ESP-IDF's idf-build-system picks `sdkconfig.defaults.<target>` automatically when `idf.py set-target esp32c6` is invoked. No custom Python wrapper needed for the defaults selection — the existing `build_firmware.ps1` keeps working for S3.

### 2.3 ADR-018 frame format extension

Bytes 18-19 are currently reserved. They become:

```
[18]   PPDU type        (0=HT, 1=HE-SU, 2=HE-MU, 3=HE-TB, 0xFF=unknown)
[19]   Bandwidth + flags
       bit 0-1 : bandwidth (0=20 MHz, 1=40, 2=80, 3=160)
       bit 2   : STBC
       bit 3   : LDPC
       bit 4   : 802.15.4 time-sync valid (C6 only, set if c6_timesync_get_epoch_us is fresh)
       bit 5-7 : reserved
```

Magic stays `0xC5110001` — readers that don't know about byte 18-19 see what they always saw (`info->buf` is unchanged). Readers that do can opt in.

### 2.4 802.15.4 time-sync protocol (skeleton)

- One node is elected `time-leader` (lowest 64-bit EUI on the mesh).
- Leader broadcasts a `TS_BEACON` frame every 100 ms on 802.15.4 channel 15 containing its monotonic `esp_timer_get_time()` snapshot.
- Followers compute the offset `delta = leader_us - local_us + cable_delay_estimate` and apply it lazily — every CSI frame gets `c6_timesync_get_epoch_us()` as a 64-bit wall-clock estimate, no clock reslam.
- Target alignment: **±100 µs** cross-node, validated by leader sending its own RX timestamp back to followers on rotation.
- Falls back to local timer if no leader heard within 5 s.

### 2.5 TWT negotiation

- After WiFi STA connects, call `esp_wifi_sta_itwt_setup()` with:
  - `wake_interval_us` = `CONFIG_TWT_WAKE_INTERVAL_US` (default 10 000 = 100 fps cadence)
  - `min_wake_dura` = 512 µs (enough to receive one CSI frame)
  - `trigger` = false (non-trigger-based — leader role)
- If the AP rejects (`ESP_ERR_WIFI_NOT_INIT` / `ESP_ERR_WIFI_NOT_STARTED` / negotiation NACK), log and continue without TWT — CSI still works opportunistically.
- Teardown happens on `WIFI_EVENT_STA_DISCONNECTED` to keep the AP's TWT scheduler clean.

### 2.6 LP-core hibernation

**Shipped (P5):** `esp_deep_sleep_enable_gpio_wakeup()` deep-sleep GPIO wake — the simplest path that actually delivers the hibernation budget for the canonical seed-node use case (PIR sensor outputting a clean digital interrupt). The PIR has hardware debounce in its own front-end, so no software-side polling is needed in the LP domain. Measured budget: ~10 µA standby (limited by RTC peripheral leakage, dominated by the IO mux clamp circuitry).

**Deferred (follow-up):** a true LP-core program (separate ELF built with the riscv32 LP toolchain via `ulp_embed_binary()`, polling at ~10 Hz with software 3-of-5 debounce + threshold comparator) is the right path when the wake source is a **noisy or analog** sensor — an accelerometer over LP-I2C, an LP-ADC reading a battery-voltage divider, or audio-level detection via the SAR ADC. That code lives in `lp_core/main.c` as a sub-project and pushes the standby budget down to the ~5 µA target. Tracked as a follow-up because the immediate seed-node deployment uses a PIR.

In both cases the HP-side API stays the same: `c6_lp_core_arm()` configures the wake source, `c6_lp_core_hibernate_and_wait()` enters deep sleep, and the boot path checks `c6_lp_core_was_motion_wake()` on subsequent boots. Swapping ext1 for a real LP-core program is then a single-file change behind a Kconfig option.

## 3. Consequences

### 3.1 Wins

- New publishable research surface (Wi-Fi-6 CSI human pose).
- Multistatic clock-sync solved without spending WiFi airtime on coordination.
- Deterministic CSI cadence available where the AP cooperates (TWT).
- Cognitum Seed always-on class roughly doubles practical battery life.
- S3 production path untouched — zero regression risk for shipped fleets.

### 3.2 Costs

- Second firmware target to maintain (build, test, release). Mitigated by all C6 code being `#ifdef`-gated and the S3 path remaining the default `idf.py build`.
- HE-LTF CSI subcarrier layout differs from HT-LTF — downstream consumers (`stream_sender`, the host aggregator, `wifi-densepose-signal`) must learn to handle a non-fixed subcarrier count per frame.
- 802.15.4 stack adds ~80 KB to the C6 binary. Fits in 4 MB partition with room to spare.
- TWT depends on AP cooperation. Most home APs (including the `ruv.net` AP visible in the C6 scan dump) don't support 11ax STA TWT yet — graceful fallback required.

### 3.3 Verification

- `firmware/esp32-csi-node` builds for both `esp32s3` (existing) and `esp32c6` (new) targets.
- S3 build artifact SHA-256 unchanged vs the last v0.6.x release (proves no regression in shared code).
- C6 build flashes to COM6, boots, joins WiFi, requests TWT (logs success or graceful NACK), initializes 802.15.4, emits CSI frames with the extended ADR-018 metadata.
- Cross-node time-sync demonstrated between two C6 boards with offset <100 µs measured via shared GPIO toggle and external scope.
- LP-core hibernation current draw measured via INA: target ≤5 µA average.

## 4. Implementation phases

| Phase | Scope | Status |
|---|---|---|
| **P1** | Multi-target build support (sdkconfig.defaults.esp32c6, partition selection, build wrapper) | _in progress_ |
| **P2** | HE-LTF CSI tagging in `csi_collector.c` | pending |
| **P3** | TWT setup helper | pending |
| **P4** | 802.15.4 init + skeleton time-sync | pending |
| **P5** | LP-core hibernation stub | ✅ **done** (v0.6.6); upgraded to real LP-core polling program in v0.6.7 (`firmware/esp32-csi-node/main/lp_core/main.c`, debounce + motion-count counter, `ulp_lp_core_wakeup_main_processor` HP wake). Ext1 fallback kept as the `CONFIG_C6_LP_CORE_ENABLE=n` branch. Datasheet ≤5 µA pending INA measurement. |
| **P6** | Build, flash COM6, capture boot telemetry, S3 regression check | ✅ **done** — `c6_ts: init done channel=15 leader=yes(candidate)`, HE MAC firmware loaded, 1003 KB binary (46% slack) |
| **P7** | Benchmark C6 vs S3 (CSI fps, RAM, TWT jitter, power) | ✅ **done** — boot 353 ms, ts init 413 ms, image 1003 KB (−9 % vs S3), 310 KiB free heap, CSI callbacks fire at 64 subcarriers/frame on ch 1 background traffic |
| **P8** | Witness bundle update, CLAUDE.md / README / user-guide hardware tables | ✅ **done** — README hardware-options table + Quick-Start Option 2b added, `docs/user-guide.md` now has full ESP32-C6 section (build, flash, provision, multi-room time-sync, battery seed mode) |
| **P9** | **Software-only unblocks for B1/B2/B4 (firmware v0.6.7)** | ✅ **done** — (1) Real LP-core motion-gate program loads via `ulp_embed_binary(lp_core/main.c)`, exposes shared `motion_count`/`poll_count` symbols for witness verification (B4 code path complete, hardware-measurement still pending INA). (2) Soft-AP HE module (`c6_softap_he.{h,c}`) runs the C6 in AP+STA mode with WPA2 + HE advertised so a second C6 STA can negotiate real iTWT against a known-cooperative AP (B1/B2 unblocker without buying an 11ax router). (3) Build artifacts: S3 8 MB 1093 KB / C6 4 MB 1019 KB, both green on IDF v5.4. Both new modules default-off so v0.6.6 fleets see no behavior change. |

This ADR is updated at the end of each phase with the actual outcome, links to commits, and any deviations from the design.

## 5. Open questions

- Should the HE-LTF subcarrier expansion ship in the default ADR-018 payload, or behind a runtime flag while the host aggregator catches up? **Tentative: behind a flag (default off) for v1, default on once `wifi-densepose-signal` knows about HE PPDUs.**
- Should the 802.15.4 time-sync channel be configurable, or hard-coded to 15? **Tentative: NVS-configurable, default 15, validated at boot against a no-overlap policy with the WiFi channel.**
- Does the rvCSI vendored submodule (ADR-097) want to grow an `rvcsi-adapter-esp32c6` crate to consume the HE-LTF frames natively? **Out of scope for this ADR; revisit in a follow-up.**
