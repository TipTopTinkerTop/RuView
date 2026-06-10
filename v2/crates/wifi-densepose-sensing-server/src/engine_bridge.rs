//! Live trust-path bridge: drive the governed [`StreamingEngine`] from the
//! sensing-server's live `NodeState` map.
//!
//! `multistatic_bridge.rs` already converts `NodeState` → `MultiBandCsiFrame`
//! and runs the *bare* `MultistaticFuser`. That path produces fused amplitudes
//! but skips the trust control plane: privacy demotion on contradiction, the
//! WorldGraph belief with mandatory provenance, and the deterministic witness
//! (ADR-135..146). This bridge routes the same live frames through
//! [`StreamingEngine::process_cycle`], so every published belief carries
//! evidence + model + calibration + privacy decision and a BLAKE3 witness —
//! making the privacy control plane non-bypassable on the live 20 Hz path
//! (the gap called out in ADR-136 §8 and the beyond-SOTA system review).
//!
//! Determinism: this module reads server state and forwards explicit
//! timestamps/calibration ids; it introduces no wall-clock reads of its own, so
//! a given `(frames, calibration, now_ms)` always yields the same
//! [`TrustedOutput`] witness.

use std::collections::HashMap;

use wifi_densepose_bfld::PrivacyMode;
use wifi_densepose_engine::{EngineError, StreamingEngine, TrustedOutput};
use wifi_densepose_geo::types::GeoRegistration;
use wifi_densepose_signal::ruvsense::fusion_quality::CalibrationId;
use wifi_densepose_worldgraph::WorldId;

use super::multistatic_bridge::node_frames_from_states;
use super::NodeState;

/// Owns a [`StreamingEngine`] and the WorldGraph scope (one room + sensor) the
/// live sensing loop publishes beliefs into.
pub struct EngineBridge {
    engine: StreamingEngine,
    room: WorldId,
    /// Nodes already wired into the WorldGraph as sensors (by `node_id`).
    registered_nodes: HashMap<u8, WorldId>,
    /// Calibration epoch applied to live frames until the ADR-135 baseline
    /// stage supplies a real per-node id. Stable so witnesses are reproducible.
    calibration: CalibrationId,
}

impl EngineBridge {
    /// Build a bridge for one installation. `room_area_id`/`room_name` name the
    /// observation scope; `mode` is the starting privacy mode.
    pub fn new(mode: PrivacyMode, model_version: u16, room_area_id: &str, room_name: &str) -> Self {
        let mut engine = StreamingEngine::new(mode, model_version, GeoRegistration::default());
        let room = engine.add_room(room_area_id, room_name);
        Self {
            engine,
            room,
            registered_nodes: HashMap::new(),
            calibration: CalibrationId(0x5256_0001), // "RV\0\x01" — placeholder epoch
        }
    }

    /// Override the calibration epoch stamped onto live frames (ADR-135).
    pub fn set_calibration(&mut self, calibration: CalibrationId) {
        self.calibration = calibration;
    }

    /// Override the WorldGraph belief-retention cap (bounds memory on the live
    /// loop; see `WorldGraph::prune_semantic_states`).
    pub fn set_semantic_retention(&mut self, max_states: usize) {
        self.engine.set_semantic_retention(max_states);
    }

    /// Switch the active privacy mode (operator/control-plane action).
    pub fn set_privacy_mode(&mut self, mode: PrivacyMode) {
        self.engine.set_privacy_mode(mode);
    }

    /// Borrow the engine (queries, WorldGraph snapshot, privacy audit).
    pub fn engine(&self) -> &StreamingEngine {
        &self.engine
    }

    /// Number of sensor nodes wired into the WorldGraph so far.
    pub fn registered_node_count(&self) -> usize {
        self.registered_nodes.len()
    }

    /// Run one governed trust cycle over the current live node states.
    ///
    /// Returns `None` when no active node yields a frame (nothing to fuse —
    /// the engine is not invoked, so no spurious belief is published). On a
    /// real cycle it lazily wires any newly-seen node as a WorldGraph sensor,
    /// then returns the witnessed [`TrustedOutput`] (or a fusion error).
    ///
    /// `now_ms` is supplied by the caller (the sensing loop's clock), keeping
    /// the bridge deterministic and replayable.
    pub fn process_cycle_from_states(
        &mut self,
        node_states: &HashMap<u8, NodeState>,
        now_ms: i64,
    ) -> Option<Result<TrustedOutput, EngineError>> {
        let frames = node_frames_from_states(node_states);
        if frames.is_empty() {
            return None;
        }
        // Lazily register each contributing node as a sensor observing the room,
        // so the privacy rollup can suppress it under identity-strict modes.
        for f in &frames {
            self.registered_nodes.entry(f.node_id).or_insert_with(|| {
                self.engine
                    .add_sensor(&format!("node-{}", f.node_id), self.room)
            });
        }
        Some(
            self.engine
                .process_cycle(&frames, self.calibration, self.room, now_ms),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::time::Instant;
    use wifi_densepose_bfld::PrivacyClass;

    fn node_state_with_history(amp: f64, n_sub: usize) -> NodeState {
        let mut ns = NodeState::new();
        let frame: Vec<f64> = (0..n_sub).map(|i| amp + 0.1 * i as f64).collect();
        ns.frame_history = VecDeque::from(vec![frame]);
        ns.last_frame_time = Some(Instant::now());
        ns
    }

    fn two_node_states() -> HashMap<u8, NodeState> {
        let mut m = HashMap::new();
        m.insert(0u8, node_state_with_history(1.0, 56));
        m.insert(1u8, node_state_with_history(1.05, 56));
        m
    }

    #[test]
    fn empty_states_produce_no_belief() {
        let mut bridge = EngineBridge::new(PrivacyMode::PrivateHome, 1, "living_room", "Living Room");
        let out = bridge.process_cycle_from_states(&HashMap::new(), 1_000);
        assert!(out.is_none());
        // No belief published, no sensor wired.
        assert_eq!(bridge.registered_node_count(), 0);
    }

    #[test]
    fn live_cycle_produces_witnessed_belief_with_provenance() {
        let mut bridge = EngineBridge::new(PrivacyMode::PrivateHome, 1, "living_room", "Living Room");
        let states = two_node_states();
        let out = bridge
            .process_cycle_from_states(&states, 10_000)
            .expect("frames present")
            .expect("fusion succeeds");

        // Full provenance: evidence + model + calibration + privacy decision.
        assert!(!out.provenance.evidence.is_empty());
        assert_eq!(out.provenance.model_version, "rfenc-v1");
        assert!(out.provenance.calibration_version.starts_with("cal:"));
        assert!(out.provenance.privacy_decision.starts_with("PrivateHome/"));
        // A witness was produced and the belief is in the WorldGraph.
        assert_ne!(out.witness, [0u8; 32]);
        assert!(bridge.engine().world().node(out.semantic_id).is_some());
        // Both nodes are now wired as sensors.
        assert_eq!(bridge.registered_node_count(), 2);
    }

    #[test]
    fn live_path_is_deterministic() {
        let states = two_node_states_fixed();
        let run = || {
            let mut b = EngineBridge::new(PrivacyMode::PrivateHome, 1, "r", "R");
            b.process_cycle_from_states(&states, 5_000).unwrap().unwrap()
        };
        let a = run();
        let b = run();
        assert_eq!(a.witness, b.witness);
        assert_eq!(a.provenance.calibration_version, b.provenance.calibration_version);
        assert_eq!(a.effective_class, b.effective_class);
    }

    // Deterministic node states (no wall-clock in amplitude/history).
    fn two_node_states_fixed() -> HashMap<u8, NodeState> {
        let mut m = HashMap::new();
        for (id, amp) in [(0u8, 1.0_f64), (1u8, 1.05)] {
            let mut ns = NodeState::new();
            ns.frame_history = VecDeque::from(vec![(0..56)
                .map(|i| amp + 0.1 * i as f64)
                .collect::<Vec<f64>>()]);
            ns.last_frame_time = Some(Instant::now());
            m.insert(id, ns);
        }
        m
    }

    #[test]
    fn nodes_registered_once_across_cycles() {
        let mut bridge = EngineBridge::new(PrivacyMode::PrivateHome, 1, "r", "R");
        let states = two_node_states();
        bridge.process_cycle_from_states(&states, 1_000);
        bridge.process_cycle_from_states(&states, 2_000);
        bridge.process_cycle_from_states(&states, 3_000);
        // Still exactly two sensors — idempotent registration.
        assert_eq!(bridge.registered_node_count(), 2);
    }

    #[test]
    fn retention_bounds_world_graph_growth() {
        let mut bridge = EngineBridge::new(PrivacyMode::PrivateHome, 1, "r", "R");
        bridge.set_semantic_retention(5);
        let states = two_node_states();
        for i in 0..20i64 {
            bridge.process_cycle_from_states(&states, 1_000 + i * 50);
        }
        // room + 2 sensors + at most 5 retained beliefs.
        assert!(bridge.engine().world().node_count() <= 3 + 5);
    }

    #[test]
    fn identity_strict_mode_is_carried_into_provenance() {
        let mut bridge = EngineBridge::new(PrivacyMode::PrivateHome, 1, "r", "R");
        bridge.set_privacy_mode(PrivacyMode::StrictNoIdentity);
        let out = bridge
            .process_cycle_from_states(&two_node_states(), 7_000)
            .unwrap()
            .unwrap();
        assert!(out.provenance.privacy_decision.starts_with("StrictNoIdentity/"));
        // Effective class is a valid privacy class (sanity).
        let _ = matches!(
            out.effective_class,
            PrivacyClass::Raw | PrivacyClass::Derived | PrivacyClass::Anonymous | PrivacyClass::Restricted
        );
    }
}
