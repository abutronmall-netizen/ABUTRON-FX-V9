"""Online skill learner — learns each signal agent's trust from outcomes.

For every (agent, symbol, regime) cell it maintains an online estimate of
the agent's predictive skill (the realised information coefficient of its
vote), updated one observation at a time via an EWMA. The learned skill
is converted to a bounded trust multiplier that the portfolio uses to
self-adjust how much it relies on each agent — no manual tuning.

This is self-learning: the trust values change based on the model's own
realised performance, and the lessons persist across restarts.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Tuple


# (agent, symbol, regime) -> skill EWMA
SkillKey = Tuple[str, str, str]


@dataclass
class SkillCell:
    skill: float = 0.02          # EWMA of realised IC (prior ~ 0)
    n: int = 0
    # rolling statistics for IC significance
    sum_score_ret: float = 0.0   # sum(score * realised_ret)
    sum_score2: float = 1e-9
    sum_ret2: float = 1e-9

    def update(self, score: float, ret: float, ema_decay: float) -> None:
        self.n += 1
        self.sum_score_ret += score * ret
        self.sum_score2 += score * score
        self.sum_ret2 += ret * ret
        # IC estimate (correlation proxy) for this observation
        denom = math.sqrt(self.sum_score2 * self.sum_ret2)
        ic = self.sum_score_ret / denom if denom > 1e-12 else 0.0
        # EWMA blend into the skill
        self.skill = ema_decay * self.skill + (1 - ema_decay) * ic

    def tstat(self) -> float:
        denom = math.sqrt(self.sum_score2 * self.sum_ret2)
        if denom <= 1e-12 or self.n < 2:
            return 0.0
        ic = self.sum_score_ret / denom
        se = math.sqrt(max(1e-12, (1 - ic * ic) / self.n))
        return ic / se


class SkillLearner:
    """Online, regime-conditional agent-skill learner."""

    def __init__(self, ema_decay: float = 0.97,
                 trust_min: float = 0.5, trust_max: float = 1.6,
                 min_obs: int = 8):
        self.ema_decay = ema_decay
        self.trust_min = trust_min
        self.trust_max = trust_max
        self.min_obs = min_obs
        self.cells: Dict[SkillKey, SkillCell] = {}
        self._global: Dict[str, float] = {}  # agent:* global trust

    # ------------------------------------------------------------------
    def observe(self, *, agent: str, symbol: str, regime: str,
                score: float, realised_ret: float) -> None:
        key = (agent, symbol, regime)
        cell = self.cells.get(key)
        if cell is None:
            cell = SkillCell()
            self.cells[key] = cell
        cell.update(score, realised_ret, self.ema_decay)
        # global agent trust (across symbols/regimes)
        g = self._global.get(agent, 0.02)
        ic = cell.sum_score_ret / math.sqrt(
            max(1e-12, cell.sum_score2 * cell.sum_ret2))
        self._global[agent] = self.ema_decay * g + (1 - self.ema_decay) * ic

    # ------------------------------------------------------------------
    def trust(self, *, agent: str, symbol: str, regime: str) -> float:
        """Bounded trust multiplier in [trust_min, trust_max].

        Uses the regime+symbol cell if it has enough observations, else the
        global agent trust, else a neutral prior.
        """
        key = (agent, symbol, regime)
        cell = self.cells.get(key)
        if cell is not None and cell.n >= self.min_obs:
            base = cell.skill
        else:
            base = self._global.get(agent, 0.02)
        # map skill (roughly -1..1) to a trust multiplier
        trust = 1.0 + 2.0 * base
        return max(self.trust_min, min(self.trust_max, trust))

    # ------------------------------------------------------------------
    def skill_report(self) -> Dict[str, Dict[str, float]]:
        """Per-agent summary for the command center / diagnostics."""
        out: Dict[str, Dict[str, float]] = {}
        for (agent, symbol, regime), cell in self.cells.items():
            out.setdefault(agent, {})[f"{symbol}|{regime}"] = {
                "skill": round(cell.skill, 4), "n": cell.n,
                "tstat": round(cell.tstat(), 3),
                "global_trust": round(self.trust(agent=agent, symbol=symbol,
                                                 regime=regime), 3)}
        return out

    def to_dict(self) -> Dict[str, object]:
        return {
            "ema_decay": self.ema_decay,
            "trust_min": self.trust_min,
            "trust_max": self.trust_max,
            "global_trust": {k: round(v, 4) for k, v in self._global.items()},
            "cells": {
                f"{a}|{s}|{r}": {"skill": round(c.skill, 4), "n": c.n}
                for (a, s, r), c in self.cells.items()},
        }

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "SkillLearner":
        sl = cls(ema_decay=float(d.get("ema_decay", 0.97)),
                 trust_min=float(d.get("trust_min", 0.5)),
                 trust_max=float(d.get("trust_max", 1.6)))
        sl._global = {k: float(v) for k, v in
                      dict(d.get("global_trust", {})).items()}
        for k, v in dict(d.get("cells", {})).items():
            a, s, r = k.split("|")
            sl.cells[(a, s, r)] = SkillCell(skill=float(v.get("skill", 0.0)),
                                            n=int(v.get("n", 0)))
        return sl
