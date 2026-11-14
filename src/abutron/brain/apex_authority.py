"""NexusBrain as the sole signal authority for the V9 -> APEX candidate path.

The APEX bridge validates native closed bars; its own BUY/SELL vote is ignored.
Randomly initialized/untrained model weights must never authorize orders.
"""
from __future__ import annotations

from collections import deque
from hashlib import sha256
from pathlib import Path
import json
import math
from typing import Any

from ..brain import make_brain, load_brain
from ..brain.features import FEATURE_VERSION, N_FEATURES
from ..config import BRAIN_MODEL_DIR, BRAIN_MC_SAMPLES, BRAIN_SIZE_CAP


class NexusSignalAuthority:
    MIN_FEATURE_BARS = 30
    HISTORY_CAP = 128

    def __init__(self, model_dir: str | None = None) -> None:
        self.model_dir = Path(model_dir or BRAIN_MODEL_DIR)
        self.brain = None
        self.model_digest = ""
        self.load_reason = "MODEL_MISSING"
        self.history: dict[tuple[str, str], deque] = {}
        path = self.model_dir / "nexus_brain.json"
        if not path.is_file() or path.is_symlink():
            return
        try:
            raw = path.read_bytes()
            data = json.loads(raw)
            report = data.get("report")
            if (data.get("feature_version") != FEATURE_VERSION
                    or data.get("n_features") != N_FEATURES):
                raise ValueError("FEATURE_SCHEMA_MISMATCH")
            if (not isinstance(report, dict)
                    or report.get("calibrated") is not True
                    or int(report.get("n_train", 0)) < 40
                    or int(report.get("n_val", 0)) < 8
                    or data.get("calibration", {}).get("fitted") is not True):
                raise ValueError("TRAINING_EVIDENCE_MISSING")
            accuracy = float(report.get("val_accuracy", -1))
            if not math.isfinite(accuracy) or not 0.0 <= accuracy <= 1.0:
                raise ValueError("TRAINING_METRICS_INVALID")
            ensemble = load_brain(str(self.model_dir), mc_samples=BRAIN_MC_SAMPLES)
            if not ensemble.members:
                raise ValueError("EMPTY_ENSEMBLE")
            self.brain = make_brain(ensemble=ensemble, size_cap=BRAIN_SIZE_CAP)
            self.model_digest = sha256(raw).hexdigest()
            self.load_reason = "VALIDATED_MODEL_LOADED"
        except Exception as exc:
            self.brain = None
            self.load_reason = f"MODEL_REJECTED:{type(exc).__name__}:{exc}"

    def decide(self, *, connection_id: str, candle: dict[str, Any],
               orderflow: dict | None = None,
               footprint: dict | None = None) -> dict[str, Any]:
        """Called only after the APEX bridge authenticates a closed M5 bar."""
        symbol = str(candle["symbol"])
        ts = int(candle["close_time_epoch"])
        bar = (ts, float(candle["close"]), float(candle["high"]),
               float(candle["low"]))
        if not all(math.isfinite(v) and v > 0 for v in bar[1:]):
            return {"status": "BRAIN_INVALID_BAR", "reason": "NONFINITE_BAR"}
        key = (str(connection_id), symbol)
        history = self.history.setdefault(key, deque(maxlen=self.HISTORY_CAP))
        if history and bar[0] <= history[-1][0]:
            return {"status": "BRAIN_INVALID_BAR", "reason": "DUPLICATE_OR_STALE"}
        if history and bar[0] - history[-1][0] != 300:
            history.clear()
        history.append(bar)
        if self.brain is None:
            return {"status": "BRAIN_MODEL_UNAVAILABLE", "reason": self.load_reason}
        if len(history) < self.MIN_FEATURE_BARS:
            return {"status": "BRAIN_FEATURE_WARMUP", "reason": "FEATURE_HISTORY_INCOMPLETE"}
        proposal = self.brain.decide(
            symbol=symbol,
            closes=[x[1] for x in history],
            highs=[x[2] for x in history],
            lows=[x[3] for x in history],
            orderflow=orderflow or {},
            footprint=footprint or {},
        )
        if proposal.direction not in ("BUY", "SELL", "HOLD"):
            return {"status": "BRAIN_INVALID_PROPOSAL", "reason": "BAD_DIRECTION"}
        values = (proposal.confidence, proposal.conviction, proposal.uncertainty)
        if not all(math.isfinite(float(v)) and 0 <= float(v) <= 1 for v in values):
            return {"status": "BRAIN_INVALID_PROPOSAL", "reason": "BAD_METRICS"}
        return {"status": proposal.direction, "reason": proposal.rationale,
                "proposal": proposal.to_dict(), "model_sha256": self.model_digest}
