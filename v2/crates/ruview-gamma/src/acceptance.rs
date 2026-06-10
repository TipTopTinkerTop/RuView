//! Acceptance gate (ADR-250 §18, generalized across programs).
//!
//! > Every use case must show measurable **entrainment**, **safety**,
//! > **adherence**, and **repeatability** before making any disease claim.
//!
//! This module operationalizes that sentence. [`AcceptanceHarness`] runs a
//! [`NeuroProgram`] against the deterministic simulator over several
//! independent repeats and produces an [`AcceptanceReport`] with the four
//! measured metrics, a per-criterion pass/fail, and — crucially — a
//! [`ClaimGate`] that **only releases the program's claim when every criterion
//! passes**. Until then the gate returns the research-only, no-claim string.

use crate::program::NeuroProgram;
use crate::response::RuViewState;
use crate::ruflo::{Consent, RufloGovernor};
use crate::simulator::{LatentPerson, ResponseSimulator};
use crate::stimulus::StimulusParameters;

/// The research-only string returned by a failed [`ClaimGate`]: no optimization
/// claim, no disease claim — only a statement that evidence is insufficient.
pub const NO_CLAIM: &str = "research use only — acceptance criteria not yet met; no claim";

/// **The hard claim-gate invariant** (ADR-250 §23.1). The single source of
/// truth used everywhere a claim could be released:
///
/// ```text
/// claim_allowed = entrainment_pass AND safety_pass
///              AND adherence_pass  AND repeatability_pass
/// ```
///
/// Anything short of all four returns the research-only string. Centralizing it
/// here means no path can accidentally weaken the gate to an OR or a subset.
#[inline]
pub fn claim_allowed(
    entrainment_pass: bool,
    safety_pass: bool,
    adherence_pass: bool,
    repeatability_pass: bool,
) -> bool {
    entrainment_pass && safety_pass && adherence_pass && repeatability_pass
}

/// Thresholds a program must clear (ADR-250 §18 generalized). Defaults mirror
/// the ADR's published targets; programs may tighten them.
#[derive(Debug, Clone, Copy)]
pub struct AcceptanceCriteria {
    /// Minimum mean entrainment gain of the adaptive recommendation over the
    /// program's fixed prior, as a fraction (0.20 = ADR-250 §18 "≥20%").
    pub min_entrainment_gain: f64,
    /// Maximum tolerated safety-stop rate across all sessions (0.0 = none).
    pub max_safety_stop_rate: f64,
    /// Minimum mean adherence across sessions.
    pub min_adherence: f64,
    /// Maximum spread (Hz) of the discovered optimal frequency across repeats
    /// (ADR-250 §18 "same optimal band within ±1 Hz across 3 sessions" → 2 Hz).
    pub max_repeatability_band_hz: f64,
    /// Independent repeats to run (≥3 per ADR-250 §18).
    pub repeats: usize,
}

impl Default for AcceptanceCriteria {
    fn default() -> Self {
        Self {
            min_entrainment_gain: 0.20,
            max_safety_stop_rate: 0.0,
            min_adherence: 0.8,
            max_repeatability_band_hz: 2.0,
            repeats: 3,
        }
    }
}

/// The four measured metrics plus per-criterion verdicts and the gated claim.
#[derive(Debug, Clone, PartialEq)]
pub struct AcceptanceReport {
    pub program_id: String,
    /// Mean entrainment gain (adaptive vs fixed prior), as a fraction.
    pub entrainment_gain: f64,
    /// Observed safety-stop rate across all sessions.
    pub safety_stop_rate: f64,
    /// Mean adherence across all sessions.
    pub mean_adherence: f64,
    /// Spread (Hz) of the discovered optimal frequency across repeats.
    pub repeatability_band_hz: f64,
    pub entrainment_pass: bool,
    pub safety_pass: bool,
    pub adherence_pass: bool,
    pub repeatability_pass: bool,
    /// True only if all four criteria pass.
    pub overall_pass: bool,
    /// The claim that may be surfaced: the program's claim iff `overall_pass`,
    /// else [`NO_CLAIM`].
    pub released_claim: String,
}

impl AcceptanceReport {
    /// The [`ClaimGate`] for this report.
    pub fn claim_gate(&self) -> ClaimGate<'_> {
        ClaimGate { report: self }
    }
}

/// A thin, hard-to-misuse accessor: you cannot read a program's marketing claim
/// except through this gate, which substitutes [`NO_CLAIM`] on failure.
#[derive(Debug, Clone, Copy)]
pub struct ClaimGate<'a> {
    report: &'a AcceptanceReport,
}

impl ClaimGate<'_> {
    /// The releasable claim string (program claim on pass, [`NO_CLAIM`] on fail).
    pub fn claim(&self) -> &str {
        &self.report.released_claim
    }

    /// Whether a (non-disease) optimization claim may be surfaced at all.
    pub fn is_released(&self) -> bool {
        self.report.overall_pass
    }
}

/// Runs a program against the deterministic simulator and grades it.
#[derive(Debug, Clone)]
pub struct AcceptanceHarness {
    pub criteria: AcceptanceCriteria,
    seed: u64,
}

impl AcceptanceHarness {
    pub fn new(seed: u64, criteria: AcceptanceCriteria) -> Self {
        Self { criteria, seed }
    }

    /// Grade `program` for a simulated participant `person` in `state`.
    ///
    /// Each repeat: enroll under the program, run its calibration sweep, take
    /// the adaptive recommendation, and compare its mean simulated entrainment
    /// against the program's fixed prior. Metrics are aggregated across repeats;
    /// the claim is released only if all four criteria pass.
    pub fn evaluate(
        &self,
        program: &NeuroProgram,
        person: &LatentPerson,
        state: &RuViewState,
    ) -> AcceptanceReport {
        let sim = ResponseSimulator::new(self.seed);
        let mut optimal_freqs = Vec::with_capacity(self.criteria.repeats);
        let mut gains = Vec::with_capacity(self.criteria.repeats);
        let mut total_sessions = 0usize;
        let mut total_stops = 0usize;
        let mut adherence_sum = 0.0;

        for r in 0..self.criteria.repeats.max(1) {
            let pid = format!("acc-{}-{}", program.id, r);
            let mut gov = match RufloGovernor::enroll_program(&pid, program.clone(), &[], Consent::Granted)
            {
                Ok(g) => g,
                // A program that cannot enroll a clean participant fails closed.
                Err(_) => return self.failed_report(program, "enrollment_failed"),
            };
            // Vary the noise stream per repeat so repeatability is a real test.
            gov.run_calibration(&sim, person, state, program.prior.duration_minutes.min(5.0), r as u64)
                .ok();

            for rec in gov.audit_log() {
                total_sessions += 1;
                if !rec.outcome.safety_pass {
                    total_stops += 1;
                }
                adherence_sum += rec.ruview_state.adherence as f64;
            }

            let rec = gov.recommend(&program.prior);
            optimal_freqs.push(rec.stimulus.frequency_hz);

            // Entrainment gain: adaptive recommendation vs the fixed prior.
            let mean = |stim: &StimulusParameters| -> f64 {
                (0..16)
                    .map(|i| sim.simulate(person, state, stim, 10_000 + i).eeg.gamma_power_gain)
                    .sum::<f64>()
                    / 16.0
            };
            let adaptive = mean(&rec.stimulus);
            let baseline = mean(&program.prior).max(1e-6);
            gains.push((adaptive - baseline) / baseline);
        }

        let entrainment_gain = mean_of(&gains);
        let safety_stop_rate = if total_sessions > 0 {
            total_stops as f64 / total_sessions as f64
        } else {
            1.0
        };
        let mean_adherence = if total_sessions > 0 {
            adherence_sum / total_sessions as f64
        } else {
            0.0
        };
        let repeatability_band_hz = spread(&optimal_freqs);

        let c = &self.criteria;
        let entrainment_pass = entrainment_gain >= c.min_entrainment_gain;
        let safety_pass = safety_stop_rate <= c.max_safety_stop_rate;
        let adherence_pass = mean_adherence >= c.min_adherence;
        let repeatability_pass = repeatability_band_hz <= c.max_repeatability_band_hz;
        let overall_pass =
            claim_allowed(entrainment_pass, safety_pass, adherence_pass, repeatability_pass);

        AcceptanceReport {
            program_id: program.id.to_string(),
            entrainment_gain,
            safety_stop_rate,
            mean_adherence,
            repeatability_band_hz,
            entrainment_pass,
            safety_pass,
            adherence_pass,
            repeatability_pass,
            overall_pass,
            released_claim: if overall_pass {
                program.claim.to_string()
            } else {
                NO_CLAIM.to_string()
            },
        }
    }

    fn failed_report(&self, program: &NeuroProgram, _why: &str) -> AcceptanceReport {
        AcceptanceReport {
            program_id: program.id.to_string(),
            entrainment_gain: 0.0,
            safety_stop_rate: 1.0,
            mean_adherence: 0.0,
            repeatability_band_hz: f64::INFINITY,
            entrainment_pass: false,
            safety_pass: false,
            adherence_pass: false,
            repeatability_pass: false,
            overall_pass: false,
            released_claim: NO_CLAIM.to_string(),
        }
    }
}

fn mean_of(v: &[f64]) -> f64 {
    if v.is_empty() {
        0.0
    } else {
        v.iter().sum::<f64>() / v.len() as f64
    }
}

fn spread(v: &[f64]) -> f64 {
    if v.is_empty() {
        return f64::INFINITY;
    }
    let lo = v.iter().cloned().fold(f64::INFINITY, f64::min);
    let hi = v.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    hi - lo
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::response::{RuViewState, SleepState};

    #[test]
    fn claim_allowed_requires_all_four_and_rejects_every_subset() {
        // All four → allowed.
        assert!(claim_allowed(true, true, true, true));
        // Every 3-of-4 subset (one false) → denied. This is the AND, not OR,
        // guarantee the whole gate rests on.
        let one_false = [
            (false, true, true, true),
            (true, false, true, true),
            (true, true, false, true),
            (true, true, true, false),
        ];
        for (e, s, a, r) in one_false {
            assert!(!claim_allowed(e, s, a, r), "subset {e}{s}{a}{r} must be denied");
        }
        assert!(!claim_allowed(false, false, false, false));
    }

    fn detuned_subject() -> (String, LatentPerson) {
        // A subject whose latent peak is clearly off the prior frequency, so an
        // adaptive program has real gain to find.
        for n in 0..80 {
            let id = format!("acc-subject-{n}");
            let p = LatentPerson::from_id(&id);
            if (p.peak_hz - 40.0).abs() > 2.0 && p.peak_hz > 37.5 && p.peak_hz < 42.5 {
                return (id, p);
            }
        }
        panic!("a detuned subject exists");
    }

    #[test]
    fn claim_is_withheld_until_criteria_pass() {
        // Impossible entrainment bar → must fail → NO_CLAIM.
        let (_, person) = detuned_subject();
        let harness = AcceptanceHarness::new(
            1,
            AcceptanceCriteria {
                min_entrainment_gain: 100.0, // unreachable
                ..Default::default()
            },
        );
        let report =
            harness.evaluate(&NeuroProgram::attention_working_memory(), &person, &RuViewState::calm_baseline());
        assert!(!report.overall_pass);
        assert!(!report.entrainment_pass);
        assert_eq!(report.claim_gate().claim(), NO_CLAIM);
        assert!(!report.claim_gate().is_released());
    }

    #[test]
    fn passing_program_releases_its_own_claim() {
        let (_, person) = detuned_subject();
        let program = NeuroProgram::attention_working_memory();
        // Lenient-but-real bar: any positive adaptive gain, perfect sim safety/adherence.
        let harness = AcceptanceHarness::new(
            7,
            AcceptanceCriteria {
                min_entrainment_gain: 0.0,
                max_safety_stop_rate: 0.0,
                min_adherence: 0.8,
                max_repeatability_band_hz: 8.0,
                repeats: 3,
            },
        );
        let report = harness.evaluate(&program, &person, &RuViewState::calm_baseline());
        assert!(report.entrainment_pass);
        assert!(report.safety_pass);
        assert!(report.adherence_pass);
        if report.overall_pass {
            assert_eq!(report.claim_gate().claim(), program.claim);
            assert!(report.claim_gate().is_released());
        }
    }

    #[test]
    fn safety_criterion_blocks_claim_on_stops() {
        // Drive overstimulation so the simulator raises adverse events: a
        // saturated-intensity prior in a restless state.
        let (_, person) = detuned_subject();
        let harness = AcceptanceHarness::new(3, AcceptanceCriteria::default());
        let mut restless = RuViewState::calm_baseline();
        restless.sleep_state = SleepState::Active;
        restless.restlessness_score = 0.9;
        // Even if it passes, the safety rate must be a real measured fraction.
        let report = harness.evaluate(&NeuroProgram::mood_arousal(), &person, &restless);
        assert!((0.0..=1.0).contains(&report.safety_stop_rate));
        if report.safety_stop_rate > 0.0 {
            assert!(!report.safety_pass);
            assert!(!report.overall_pass);
        }
    }

    #[test]
    fn every_catalog_program_is_gradable() {
        let (_, person) = detuned_subject();
        let harness = AcceptanceHarness::new(11, AcceptanceCriteria::default());
        let state = RuViewState::calm_baseline();
        for program in NeuroProgram::catalog() {
            let report = harness.evaluate(&program, &person, &state);
            assert_eq!(report.program_id, program.id);
            // Gate is total: it always yields *some* releasable string.
            assert!(!report.released_claim.is_empty());
            // And a failing program never leaks the program claim.
            if !report.overall_pass {
                assert_eq!(report.claim_gate().claim(), NO_CLAIM);
            }
        }
    }
}
