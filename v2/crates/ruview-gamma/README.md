# ruview-gamma — Adaptive Sensory Neuromodulation (ADR-250)

> **The most valuable thing here is not 40 Hz. It is a governed personalization
> engine that refuses to overpromise.**

The control brain for an adaptive light-and-sound neuromodulation device. The
device plays stimulation; **RuView** reads the body as feedback; **RuVector**
learns the personal response map; **RuFlo** governs the safety, audit trail, and
claim boundary. The breakthrough is not speed alone — it is **safe adaptive
personalization with proof discipline**.

It starts from 40 Hz as the research prior, then learns whether a person
responds better at 38.5, 40, 41.2, or another safe setting — watching breathing,
stillness, restlessness, adherence, and sensor confidence. If something goes
wrong, the session locks. If a program has not proven entrainment, safety,
adherence, and repeatability, it cannot advertise a benefit — it returns
*research use only*.

## Benchmarks (this container — indicative)

| Path | Current | Role |
|------|---------|------|
| Safety tick | ~8 ns | real-time stop path |
| Recommendation | ~15 µs | per-session decision |
| Cohort kNN (500 profiles) | ~15 µs | warm-start matching |
| Calibration sweep | ~115 µs | setup and tuning |
| Full acceptance grading | ~425 µs | enrollment-only (offline) |

The per-session control loop is microseconds; the heavier acceptance grading is
enrollment-time work, not on the loop. No regression across the optimization
passes.

## The hard claim gate

A program's benefit claim is releasable through exactly one invariant
(`acceptance::claim_allowed`), used everywhere:

```text
claim_allowed = entrainment_pass AND safety_pass
             AND adherence_pass  AND repeatability_pass
```

Anything short of all four returns `research use only — … no claim`
(`acceptance::NO_CLAIM`). The marketing claim is unreadable except through the
gate.

## Next milestone — hardware in the loop (`hil`)

The software core is proven against a deterministic simulator; the next
acceptance bar is a real LED + speaker actuator (e.g. ESP32-driven) plus the
stop path. `hil::verify_hil` grades a captured bench measurement against fixed
targets:

| Test | Target |
|------|--------|
| LED frequency accuracy | ±0.1 Hz |
| Audio-visual sync drift | < 5 ms |
| Stop signal → actuator off | < 100 ms |
| Session-hash reproducibility | 100% |
| EEG entrainment lift vs fixed 40 Hz | ≥ 20% |

---

Governed, deterministic, **safety-constrained** personalization of 40 Hz-prior
multisensory (light + sound) stimulation. Treats 40 Hz as the evidence-based
*starting prior*, then learns each person's safe entrainment response curve using
passive RuView sensing, optional EEG, a constrained optimizer, and auditable
RuFlo workflows.

> **Not medical advice / not a medical device.** This crate is a research and
> engineering platform. The only claim it makes is **"personalized entrainment
> optimization"** (`ruview_gamma::PRODUCT_CLAIM`) — never Alzheimer's treatment,
> amyloid clearance, or any clinical outcome (ADR-250 §19). It performs **no
> hardware actuation**: real stimulus delivery, RF sensing, and EEG arrive
> through external adapters behind feature flags after this governed software
> core ships (ADR-250 §21, Milestones 2–4).

## Why it exists

The field mostly treats 40 Hz as a fixed protocol. But individual brains differ
by baseline gamma, arousal, sleep, sensory acuity, medication, age, and comfort
(the 2025 PLOS One 36–44 Hz re-evaluation). Fixed 40 Hz (1) assumes one
frequency fits all, (2) never verifies entrainment, (3) ignores physiological
state, and (4) cannot safely optimize over time. This crate closes that loop.

## The safety invariant

**No recommendation, calibration step, bandit arm, or closed-loop nudge can ever
emit a `StimulusParameters` outside the `SafetyEnvelope`.** Every emitting path
clamps to the envelope and is asserted against `SafetyEnvelope::contains` in
tests. The optimizer never widens the envelope — only an operator constructs a
wider one deliberately (ADR-250 §12). Non-finite (NaN/∞) inputs clamp toward the
conservative floor, never the cap.

## Module map

| Module | Role (ADR-250 §) | Highlights |
|--------|------------------|------------|
| `stimulus` | §5, §12 | `StimulusParameters`, `SafetyEnvelope` (validate / clamp / grids) |
| `safety` | §12 | exclusion screen, latched `SafetyMonitor`, hard-stop reasons |
| `response` | §6, §9, §10 | `RuViewState`, optional `EegMeasurement`, 20-field `PersonResponseVector` (RuVector memory) with sticky adverse flag |
| `objective` | §7 | safe-entrainment score; safety is a hard gate, not a weight; RF-only proxy when EEG absent |
| `simulator` | §21 M1 | deterministic ChaCha20 `frequency_response_curve(person, state, stimulus)` |
| `optimizer` | §8 | Phase-1 calibration sweep, Phase-2 GP + Expected-Improvement, Phase-4 closed-loop control |
| `bandit` | §8 P3 | LinUCB contextual bandit over envelope-safe arms |
| `ruvector` | §10 items 3–6 | anonymized `ProfileStore` (one-way hashed tags), deterministic kNN, cohort warm-start priors (down-weighted pseudo-observations), `DriftDetector` over the physiological sub-vector, deterministic k-means clustering |
| `program` | §23 | `NeuroProgram` catalog (7 use cases) — per-program envelope, prior, objective, state-gating, evidence level, and gated claim |
| `acceptance` | §18/§23.1 | `AcceptanceHarness` + `ClaimGate` + the `claim_allowed` invariant — entrainment/safety/adherence/repeatability gate; a program's claim is unreadable until all four pass |
| `hil` | §17/§21 M2 | hardware-in-the-loop contract: `verify_hil` grades a captured actuator measurement (LED ±0.1 Hz, A/V sync < 5 ms, stop < 100 ms, hash 100%, EEG lift ≥ 20%) |
| `session` | §11, §13 | hashable `SessionRecord`, reproducible `session_hash` (SHA-256, quantized canonical form) |
| `ruflo` | §11 | consent → exclusion → envelope → run → monitor → score → update → witnessed audit; trial/sham mode; clinician export; claim discipline |
| `proof` | — | deterministic bundle witness (mirrors `nvsim` / `verify.py`) |
| `math` | — | dependency-light numerics (erf, normal CDF/PDF, Cholesky, RBF) |

## Quick start

```rust
use ruview_gamma::{
    ruflo::{Consent, RufloGovernor},
    response::RuViewState,
    simulator::{LatentPerson, ResponseSimulator},
    stimulus::{SafetyEnvelope, StimulusParameters},
};

let envelope = SafetyEnvelope::conservative();
let mut gov = RufloGovernor::enroll("subject-001", envelope, &[], Consent::Granted)
    .expect("cleared to participate");

// Milestone 1: drive the governed loop with the deterministic simulator.
let sim = ResponseSimulator::new(42);
let latent = LatentPerson::from_id("subject-001");
let state = RuViewState::calm_baseline();
gov.run_calibration(&sim, &latent, &state, 5.0, 0).unwrap();

let rec = gov.recommend(&StimulusParameters::prior());
assert!(envelope.contains(&rec.stimulus)); // always inside the envelope
```

## Test / validate / benchmark

```bash
cargo test  -p ruview-gamma --no-default-features    # 64 unit/integration + 1 doctest
cargo bench -p ruview-gamma --no-default-features     # criterion micro-benchmarks
```

Determinism is proven, not assumed: `proof::Proof::reference_witness()` runs a
fixed reference participant through the full governed pipeline and pins the
bundle SHA-256 (`Proof::EXPECTED_WITNESS`); the test fails on any silent drift in
the optimizer, simulator, response update, or session hashing.

### Measured (this container, indicative — not a regression gate)

| Bench | Median | Note |
|-------|--------|------|
| `gamma_safety_tick` | ~9.3 ns | vs ADR-250 §17 < 500 ms hard-stop latency bound |
| `gamma_bandit_select` | ~74 ns | LinUCB decision |
| `gamma_bayesian_recommend` | ~19 µs | GP + EI over the 0.1 Hz envelope grid (was ~105 µs: the GP is now factorized once per recommend, not once per grid candidate — −81%, bit-identical) |
| `gamma_calibration_sweep` | ~135 µs | full 9-session enroll → simulate → score → update → witness (was ~486 µs, −71%) |
| `gamma_cohort_knn_500` | ~15 µs | exact kNN over 500 anonymized profiles |
| `gamma_cohort_warm_start_500` | ~16 µs | full cohort prior construction (runs once per enrollment) |
| `gamma_acceptance_grade_program` | ~425 µs | full 3-repeat program acceptance grading (offline gate) |

## Adaptive sensory neuromodulation platform (ADR-250 §23)

40 Hz is one prior in one program — the engine is a general personal
neural-rhythm optimization platform. `NeuroProgram::catalog()` ships seven use
cases (Alzheimer's research, post-stroke cognition, sleep optimization,
attention/working-memory, mood/arousal, home wellness, drug+device trial
infrastructure), each with its **own** safety envelope, prior, objective
weighting, physiological-state gating (the sleep program permits `Asleep` and
caps brightness near-dark; attention requires wakefulness), evidence level, and
a single non-disease claim. `RufloGovernor::enroll_program` wires it all in;
`enroll` stays the bare Alzheimer's-defaults path (so the pinned witness holds).

**Claim discipline is executable.** A program's claim can only be read through
the acceptance gate:

```rust
use ruview_gamma::acceptance::{AcceptanceHarness, AcceptanceCriteria};
use ruview_gamma::program::NeuroProgram;
# use ruview_gamma::simulator::LatentPerson;
# use ruview_gamma::response::RuViewState;

let harness = AcceptanceHarness::new(42, AcceptanceCriteria::default());
let report = harness.evaluate(
    &NeuroProgram::sleep_optimization(),
    &LatentPerson::from_id("subject"),
    &RuViewState::calm_baseline(),
);
// Returns the program claim ONLY if entrainment + safety + adherence +
// repeatability all pass; otherwise the research-only NO_CLAIM string.
let _claim = report.claim_gate().claim();
```

## Self-learning across people (ADR-250 §10)

`RufloGovernor::export_anonymized_profile()` publishes a participant's 20-field
vector + per-frequency scores from **safe sessions only** under a one-way hashed
tag; `seed_from_cohort(&store, k)` warm-starts a new person's optimizer from the
k nearest responders as **down-weighted pseudo-observations**
(`observe_prior`, ≥25× the real-observation noise). Priors shape where the
optimizer looks first but never count as measured data — they are excluded from
the EI incumbent, the audit log, and the clinician report. Per-session
`drift_status()` (Welford centroid over the *physiological* sub-vector —
stimulus inputs masked out) flags when recalibration is warranted.

## Roadmap (ADR-250 §21)

M1 simulator ✅ · M2 device harness (envelope + e-stop contract) ✅ · M3 RuView
state contract ✅ · M4 optional EEG input ✅ · M5 adaptive optimizer (BO + bandit
+ closed-loop) ✅ · M6 trial mode (sham/blinding + clinician export) ✅ ·
§10 RuVector self-learning (cohort warm-start, drift detection, clustering) ✅.
Hardware actuation, real RF sensing, and real EEG land behind feature-flagged
adapters. An HNSW backend (the `ruvector` crates) drops in for `ProfileStore`
once cohorts grow past ~10⁵ profiles.
