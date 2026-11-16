"""SelfLearningEngine — the self-learning AI orchestrator.

Wires together:
  * OnlineLinearModel  — predicts next-bar edge from features; updates
                         its own weights from realised returns (SGD)
  * SkillLearner       — learns each signal agent's trust from realised
                         P&L, per regime/symbol; self-adjusts trust
  * SelfLearnGuardrails — clamps every overlay to hard bounds so the
                         learner only adjusts weights, never execution

Per closed-bar cycle the engine:
  1. produce_overlay(): predicts edge + skill trust -> bounded weight
     multipliers (applied to APEX target weights, advisory only).
  2. observe_outcome(): after the bar settles, feeds realised return +
     agent scores into the online model + skill learner to update them.

State persists to JSON so the learned weights survive restarts and keep
improving across sessions.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from .online_model import OnlineLinearModel
from .skill_learner import SkillLearner
from .guardrails import SelfLearnGuardrails, OverlayDecision
from .features import N_FEATURES, build_feature_vector


@dataclass
class LearnerState:
    model: Optional[OnlineLinearModel] = None
    skill: Optional[SkillLearner] = None
    observations: int = 0
    last_overlay: Optional[Dict[str, Any]] = None


class SelfLearningEngine:
    """The self-learning AI: updates its own weights from live outcomes."""

    def __init__(self, event_log=None, *,
                 model_lr: float = 0.05,
                 model_l2: float = 1e-5,
                 skill_decay: float = 0.97,
                 global_bounds=(0.5, 1.5),
                 symbol_bounds=(0.5, 1.5),
                 state_path: Optional[str] = None):
        self.event_log = event_log
        self.model = OnlineLinearModel(n_features=N_FEATURES, lr=model_lr,
                                       l2=model_l2)
        self.skill = SkillLearner(ema_decay=skill_decay)
        self.guardrails = SelfLearnGuardrails(
            global_min=global_bounds[0], global_max=global_bounds[1],
            symbol_min=symbol_bounds[0], symbol_max=symbol_bounds[1])
        self.observations = 0
        self.state_path = state_path
        self.last_overlay: Optional[OverlayDecision] = None

    # ------------------------------------------------------------------
    # Phase 1 — produce a bounded overlay from the current model
    # ------------------------------------------------------------------
    def produce_overlay(self, *, symbol: str,
                        orderflow: Optional[Dict[str, float]] = None,
                        footprint: Optional[Dict[str, float]] = None,
                        regime: str = "",
                        agent_scores: Optional[Dict[str, float]] = None,
                        momentum_z: float = 0.0,
                        realised_vol_z: float = 0.0
                        ) -> OverlayDecision:
        """Predict edge + skill, return a guardrail-clamped overlay.

        The global multiplier is derived from the model's edge prediction
        mapped to a confidence scalar; the per-symbol multiplier blends
        the skill learner's trust for the dominant agent.
        """
        feats = build_feature_vector(
            orderflow=orderflow, footprint=footprint, regime=regime,
            agent_scores=agent_scores, momentum_z=momentum_z,
            realised_vol_z=realised_vol_z)
        edge_pred = self.model.predict(np.asarray(feats, dtype=float))

        # map edge prediction (normalised return units) to a confidence
        # multiplier around 1.0. Strong predicted edge -> modest boost;
        # strong negative edge -> reduce. Bounded by guardrails.
        learned_multiplier = float(1.0 + np.clip(edge_pred * 4.0, -0.5, 0.5))

        # per-symbol trust from the skill learner (if any agent scored)
        per_sym = {}
        if agent_scores:
            # trust = skill-learned trust of the strongest-signal agent
            best_agent = max(agent_scores, key=lambda a: abs(agent_scores[a]))
            trust = self.skill.trust(agent=best_agent, symbol=symbol,
                                     regime=regime)
            per_sym[symbol] = float(trust)
        else:
            per_sym[symbol] = 1.0

        overlay = self.guardrails.build_overlay(
            learned_multiplier=learned_multiplier,
            per_symbol_multiplier=per_sym)
        self.last_overlay = overlay
        return overlay

    def apply_overlay(self, weights: Dict[str, float],
                      overlay: OverlayDecision) -> Dict[str, float]:
        """Apply the bounded overlay to a target-weight book (weights only)."""
        return self.guardrails.apply_to_weights(weights, overlay)

    # ------------------------------------------------------------------
    # Phase 2 — observe the outcome and update the learner
    # ------------------------------------------------------------------
    def observe_outcome(self, *, symbol: str, features: List[float],
                        realised_ret: float, regime: str = "",
                        agent_scores: Optional[Dict[str, float]] = None,
                        target_weight: float = 0.0) -> Dict[str, float]:
        """Feed a settled bar's realised return into the learner.

        Updates the online model (SGD on the edge prediction) and the
        skill learner (agent trust from realised P&L). Returns a small
        summary of the update for the decision journal.
        """
        feats = np.asarray(features, dtype=float)
        error = self.model.update(feats, float(realised_ret))
        self.observations += 1

        summary: Dict[str, float] = {"error": float(error)}
        if agent_scores:
            for agent, score in agent_scores.items():
                self.skill.observe(agent=agent, symbol=symbol, regime=regime,
                                   score=float(score),
                                   realised_ret=float(realised_ret))
            summary["agents_updated"] = float(len(agent_scores))
        return summary

    # ------------------------------------------------------------------
    # Persistence — the learned weights survive restarts
    # ------------------------------------------------------------------
    def save(self) -> None:
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            payload = {
                "model": self.model.to_dict(),
                "skill": self.skill.to_dict(),
                "observations": self.observations,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            with open(self.state_path, "w") as fh:
                json.dump(payload, fh, indent=2)
        except OSError:
            pass

    def load(self) -> bool:
        if not self.state_path or not os.path.exists(self.state_path):
            return False
        try:
            with open(self.state_path) as fh:
                payload = json.load(fh)
            self.model = OnlineLinearModel.from_dict(payload["model"])
            self.skill = SkillLearner.from_dict(payload["skill"])
            self.observations = int(payload.get("observations", 0))
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    # ------------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        """Diagnostics for the command center."""
        return {
            "model_updates": self.model.updates,
            "observations": self.observations,
            "mse_ema": (None if not self.model._mse_ema_valid
                        else round(self.model.mse_ema, 6)),
            "skill_cells": len(self.skill.cells),
            "last_overlay": (self.last_overlay.to_dict()
                             if self.last_overlay else None),
            "state_path": self.state_path,
            "execution_authority": False,  # invariant: learner never executes
        }
