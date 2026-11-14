"""NexusBrain — the advanced, uncertainty-aware decision brain.

Replaces the legacy rule-based multi-agent voting as the signal authority.
Ensemble neural encoders + MC-dropout/ensemble uncertainty + calibrated
confidence + regime conditioning + abstention + gated online adaptation,
all behind the deterministic RiskGuard firewall.

Public API:
    NexusBrain, BrainProposal, make_brain, default_ensemble
    BrainEnsemble, EnsemblePrediction, build_ensemble
    prepare_samples, walk_forward_train, save_brain, load_brain
"""
from .brain import (NexusBrain, BrainProposal, make_brain, default_ensemble,
                    detect_regime)
from .ensemble import (BrainEnsemble, EnsemblePrediction, EnsembleMember,
                       build_ensemble)
from .features import (FEATURE_NAMES, N_FEATURES, FEATURE_VERSION,
                       build_feature_vector)
from .trainer import (TrainingSample, TrainReport, prepare_samples,
                      walk_forward_train, save_brain, load_brain)

__all__ = [
    "NexusBrain", "BrainProposal", "make_brain", "default_ensemble",
    "detect_regime", "BrainEnsemble", "EnsemblePrediction",
    "EnsembleMember", "build_ensemble", "FEATURE_NAMES", "N_FEATURES",
    "FEATURE_VERSION", "build_feature_vector", "TrainingSample",
    "TrainReport", "prepare_samples", "walk_forward_train",
    "save_brain", "load_brain",
]
