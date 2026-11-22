"""V9 49-feature broker-net entry reward research.

DEMO-only candidate, independent of the directional neural ensemble.
Every label comes from a broker-authenticated CLOSED executed position.
A losing BUY is evidence about BUY only; it is not a winning SELL label.

This module does not send orders, change risk rules, or promote a model.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Mapping, Sequence

import numpy as np

N_FEATURES = 49
SIDES = ("BUY", "SELL")


def _utc(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError("naive broker timestamp")
    return ts.astimezone(timezone.utc)


@dataclass(frozen=True)
class ClosedTrade:
    account: str
    trade_key: str
    symbol: str
    side: str
    entered: datetime
    closed: datetime
    features: tuple[float, ...]
    lots: float
    gross_usd: float
    commission_usd: float
    fee_usd: float
    swap_usd: float
    net_usd: float
    stop_loss_exit: bool
    take_profit_exit: bool

    @classmethod
    def from_native_row(cls, row: Mapping) -> "ClosedTrade":
        if int(row["native_deal_verified"]) != 1 or int(row["cost_breakdown_verified"]) != 1:
            raise ValueError("unverified native deal or cost breakdown")
        side = str(row["direction"]).upper()
        if side not in SIDES:
            raise ValueError("unknown executed trade direction")
        features = tuple(float(x) for x in json.loads(row["features_json"]))
        if len(features) != N_FEATURES or not np.isfinite(features).all():
            raise ValueError("invalid 49-feature input")
        entered, closed = _utc(str(row["entry_time"])), _utc(str(row["exit_time"]))
        if not entered < closed:
            raise ValueError("trade exit is not later than its entry")
        lots = float(row["lots"])
        gross, commission, fee, swap, net = (
            float(row[name]) for name in
            ("gross_usd", "commission_usd", "fee_usd", "swap_usd", "net_usd")
        )
        if lots <= 0 or not np.isfinite([lots, gross, commission, fee, swap, net]).all():
            raise ValueError("invalid broker economics")
        if abs(net - (gross + commission + fee + swap)) > 0.03:
            raise ValueError("broker-net P&L does not reconcile")
        sl, tp = bool(int(row["stop_loss_exit"])), bool(int(row["take_profit_exit"]))
        if sl and tp:
            raise ValueError("SL and TP cannot both be the exit reason")
        return cls(
            account=str(row["account"]), trade_key=str(row["trade_key"]),
            symbol=str(row["symbol"]), side=side,
            entered=entered, closed=closed, features=features, lots=lots,
            gross_usd=gross, commission_usd=commission, fee_usd=fee,
            swap_usd=swap, net_usd=net, stop_loss_exit=sl, take_profit_exit=tp,
        )

    @property
    def gross_per_lot(self) -> float:
        return self.gross_usd / self.lots

    @property
    def cost_per_lot(self) -> float:
        return -(self.commission_usd + self.fee_usd + self.swap_usd) / self.lots

    @property
    def net_per_lot(self) -> float:
        return self.net_usd / self.lots

    @property
    def closed_m5_bucket(self) -> int:
        return int(self.entered.timestamp()) // 300


def chronological_split(
    trades: Sequence[ClosedTrade],
    validation_start: datetime,
    test_start: datetime,
) -> tuple[list[ClosedTrade], list[ClosedTrade], list[ClosedTrade], int]:
    """Purge boundary-crossing outcomes; never use a later settlement label."""
    if validation_start.tzinfo is None or test_start.tzinfo is None:
        raise ValueError("split boundaries require timezone")
    if not validation_start < test_start:
        raise ValueError("test start must follow validation start")
    train, validation, test = [], [], []
    used_keys = set()
    excluded = 0
    for row in sorted(trades, key=lambda t: (t.entered, t.account, t.trade_key)):
        key = (row.account, row.trade_key)
        if key in used_keys:
            raise ValueError("duplicate native trade")
        used_keys.add(key)
        if row.entered < validation_start and row.closed < validation_start:
            train.append(row)
        elif validation_start <= row.entered < test_start and row.closed < test_start:
            validation.append(row)
        elif row.entered >= test_start:
            test.append(row)
        else:
            excluded += 1
    for lhs, rhs in ((train, validation), (validation, test)):
        if {r.closed_m5_bucket for r in lhs} & {r.closed_m5_bucket for r in rhs}:
            raise ValueError("same entry M5 candle in different splits")
    return train, validation, test, excluded


class NetReward49:
    """Separate observed-side heads: expected gross, cost, net and TP chance.

    No side is synthesized from the opposite side's result.
    Input is exactly 49 features; side selects a trained head, not a 50th feature.
    """

    def __init__(self, *, identity_slot: int | None = None, min_side_trades: int = 20):
        if identity_slot is not None and not 23 <= identity_slot < N_FEATURES:
            raise ValueError("symbol identity slot outside canonical one-hot")
        self.identity_slot = identity_slot
        self.min_side_trades = min_side_trades
        self.heads: dict[str, dict] = {}
        self.observed_ids: set[tuple[str, str]] = set()
        self.observed_fingerprints: dict[tuple[str, str], str] = {}
        self.account_scope: str | None = None
        self.online_updates = 0

    def _matrix(self, rows: Sequence[ClosedTrade]) -> np.ndarray:
        x = np.asarray([r.features for r in rows], dtype=float)
        if x.ndim != 2 or x.shape[1] != N_FEATURES or not np.isfinite(x).all():
            raise ValueError("expected finite 49-feature matrix")
        if self.identity_slot is not None:
            if (
                not np.allclose(x[:, self.identity_slot], 1.0, rtol=0, atol=1e-8)
                or not np.allclose(x[:, 23:].sum(axis=1), 1.0, rtol=0, atol=1e-8)
            ):
                raise ValueError("incorrect native 49-feature symbol identity mapping")
            x = x.copy()
            x[:, self.identity_slot] = 0.0
        return x

    @staticmethod
    def _fingerprint(trade: ClosedTrade) -> str:
        """Immutable native settlement fingerprint, including all learning inputs."""
        evidence = {
            "account": trade.account, "trade_key": trade.trade_key,
            "symbol": trade.symbol, "side": trade.side,
            "entered": trade.entered.isoformat(), "closed": trade.closed.isoformat(),
            "features": list(trade.features), "lots": trade.lots,
            "gross_usd": trade.gross_usd, "commission_usd": trade.commission_usd,
            "fee_usd": trade.fee_usd, "swap_usd": trade.swap_usd, "net_usd": trade.net_usd,
            "stop_loss_exit": trade.stop_loss_exit, "take_profit_exit": trade.take_profit_exit,
        }
        return hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _targets(rows: Sequence[ClosedTrade]) -> np.ndarray:
        return np.asarray([
            (r.gross_per_lot, r.cost_per_lot, float(r.take_profit_exit))
            for r in rows
        ], dtype=float)

    @staticmethod
    def _design(x: np.ndarray, mean: np.ndarray, scale: np.ndarray) -> np.ndarray:
        return np.column_stack([np.ones(len(x)), np.clip((x - mean) / scale, -4, 4)])

    @staticmethod
    def _ridge(z: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
        penalty = np.eye(z.shape[1]) * alpha
        penalty[0, 0] = 0.0
        return np.linalg.solve(z.T @ z + penalty, z.T @ y)

    def fit(
        self,
        training: Sequence[ClosedTrade],
        validation: Sequence[ClosedTrade],
        *,
        penalties: Sequence[float] = (3.0, 10.0, 30.0, 100.0, 300.0),
    ) -> dict:
        if not penalties or any(p <= 0 for p in penalties):
            raise ValueError("invalid ridge candidates")
        accounts = {r.account for r in (*training, *validation)}
        if len(accounts) != 1:
            raise ValueError("reward heads require one isolated DEMO account")
        self.account_scope = accounts.pop()
        new_fingerprints = {
            (r.account, r.trade_key): self._fingerprint(r) for r in training
        }
        if len(new_fingerprints) != len(training):
            raise ValueError("duplicate native training trade")
        self.heads.clear()
        self.observed_fingerprints = new_fingerprints
        self.observed_ids = set(new_fingerprints)
        self.online_updates = 0
        summary = {}
        for side in SIDES:
            tr = [r for r in training if r.side == side]
            va = [r for r in validation if r.side == side]
            if len(tr) < self.min_side_trades:
                summary[side] = {"status": "INSUFFICIENT_ACTUAL_SIDE_TRADES", "train": len(tr)}
                continue
            x = self._matrix(tr)
            mean = x.mean(axis=0)
            sd = np.where(x.std(axis=0) < 0.05, 1.0, x.std(axis=0))
            z = self._design(x, mean, sd)
            targets = self._targets(tr)
            # Cap outliers using training ONLY; never winsorize using holdout labels.
            lower = np.quantile(targets[:, :2], 0.02, axis=0)
            upper = np.quantile(targets[:, :2], 0.98, axis=0)
            clipped = targets.copy()
            clipped[:, :2] = np.clip(clipped[:, :2], lower, upper)
            xval = self._design(self._matrix(va), mean, sd) if va else None
            best = None
            for alpha in penalties:
                coef = self._ridge(z, clipped, float(alpha))
                if va:
                    yhat = xval @ coef
                    prediction = yhat[:, 0] - np.maximum(yhat[:, 1], 0)
                    realized = np.asarray([r.net_per_lot for r in va])
                    mae = float(np.mean(np.abs(prediction - realized)))
                else:
                    mae = None
                if best is None or (mae is not None and (best[0] is None or mae < best[0])):
                    best = (mae, float(alpha), coef)
            assert best is not None
            # The unvalidated SELL head may be evaluated in research,
            # but should never be considered proven or automatically tradable.
            self.heads[side] = {
                "mean": mean.tolist(), "scale": sd.tolist(),
                "coefficients": best[2].tolist(), "alpha": best[1],
                "train_trades": len(tr), "validation_trades": len(va),
                "validated_side": len(va) >= 15,
                "validation_net_mae_per_lot": best[0],
                "trained_first_entry": min(r.entered for r in tr).isoformat(),
                "trained_last_exit": max(r.closed for r in tr).isoformat(),
            }
            summary[side] = {
                "status": "RESEARCH_FITTED" if va else "NO_SIDE_VALIDATION",
                "train": len(tr), "validation": len(va),
                "ridge": best[1], "validation_mae_per_lot": best[0],
            }
        return summary

    def predict(
        self,
        features: Sequence[float],
        side: str,
        *,
        broker_current_cost_per_lot: float | None = None,
    ) -> dict:
        if side not in self.heads:
            return {"side": side, "status": "UNTRAINED_EXECUTED_SIDE"}
        info = self.heads[side]
        x = np.asarray(features, dtype=float)
        if x.shape != (N_FEATURES,) or not np.isfinite(x).all():
            raise ValueError("expected finite 49-feature signal")
        if self.identity_slot is not None:
            if (
                not np.isclose(x[self.identity_slot], 1.0, rtol=0, atol=1e-8)
                or not np.isclose(x[23:].sum(), 1.0, rtol=0, atol=1e-8)
            ):
                raise ValueError("incorrect native 49-feature symbol identity mapping")
            x = x.copy()
            x[self.identity_slot] = 0.0
        z = np.r_[1.0, np.clip(
            (x - np.asarray(info["mean"])) / np.asarray(info["scale"]), -4, 4
        )]
        gross, cost, tp_chance = z @ np.asarray(info["coefficients"])
        predicted_cost = max(0.0, float(cost))
        if broker_current_cost_per_lot is not None:
            if broker_current_cost_per_lot < 0 or not np.isfinite(broker_current_cost_per_lot):
                raise ValueError("invalid quoted broker cost")
            predicted_cost = max(predicted_cost, broker_current_cost_per_lot)
        return {
            "side": side, "status": "RESEARCH_PREDICTION",
            "validated_side": bool(info["validated_side"]),
            "gross_usd_per_lot": float(gross),
            "cost_usd_per_lot": predicted_cost,
            "broker_net_usd_per_lot": float(gross) - predicted_cost,
            "take_profit_exit_probability": float(np.clip(tp_chance, 0, 1)),
        }

    def choose(
        self,
        features: Sequence[float],
        *,
        broker_cost_by_side: Mapping[str, float] | None = None,
    ) -> dict:
        predictions = {
            side: self.predict(features, side, broker_current_cost_per_lot=(
                broker_cost_by_side or {}).get(side))
            for side in SIDES
        }
        opportunities = [
            p for p in predictions.values()
            if p.get("validated_side") and p.get("broker_net_usd_per_lot", 0) > 0
        ]
        selected = max(opportunities, key=lambda p: p["broker_net_usd_per_lot"]) if opportunities else None
        return {"direction": selected["side"] if selected else "HOLD",
                "predictions": predictions, "research_only": True,
                "broker_execution_enabled": False}

    def online_update(self, trade: ClosedTrade, *, learned_at: datetime, step: float = 0.001) -> bool:
        """One causal, owner-scoped, observed-side economic reward update."""
        if learned_at.tzinfo is None or trade.closed > learned_at:
            raise ValueError("closed trade must precede the learning event")
        key = (trade.account, trade.trade_key)
        if self.account_scope is None or trade.account != self.account_scope:
            raise ValueError("cross-account economic learning rejected")
        fingerprint = self._fingerprint(trade)
        if key in self.observed_ids:
            if self.observed_fingerprints[key] != fingerprint:
                raise ValueError("previously learned broker evidence changed")
            return False
        if trade.side not in self.heads:
            return False
        head = self.heads[trade.side]
        x = self._matrix([trade])
        z = self._design(x, np.asarray(head["mean"]), np.asarray(head["scale"]))[0]
        coef = np.asarray(head["coefficients"], dtype=float)
        observed = self._targets([trade])[0]
        err = np.clip(observed - z @ coef, -1.0, 1.0)
        coef += (step / (1.0 + float(z @ z))) * np.outer(z, err)
        head["coefficients"] = coef.tolist()
        self.observed_ids.add(key)
        self.observed_fingerprints[key] = fingerprint
        self.online_updates += 1
        return True

    @classmethod
    def from_artifact(cls, source: Mapping) -> "NetReward49":
        """Restore a SHA-pinned research checkpoint without repeating past trades."""
        payload = dict(source)
        digest = payload.pop("sha256", None)
        expected = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        if not isinstance(digest, str) or digest != expected:
            raise ValueError("research checkpoint digest mismatch")
        if (
            payload.get("schema") != "abutron.v9.broker_net_49.research.v1"
            or payload.get("market_feature_width") != N_FEATURES
            or payload.get("production_model_promoted") is not False
            or payload.get("orders_sent") != 0
            or not isinstance(payload.get("account_scope"), str)
            or not payload["account_scope"]
        ):
            raise ValueError("unsafe or incompatible reward checkpoint")
        model = cls(identity_slot=payload["identity_slot"],
                    min_side_trades=int(payload["min_side_trades"]))
        model.account_scope = payload["account_scope"]
        head_data = payload["heads"]
        if not isinstance(head_data, dict) or any(k not in SIDES for k in head_data):
            raise ValueError("invalid broker reward side heads")
        for head in head_data.values():
            coef = np.asarray(head["coefficients"], dtype=float)
            mean = np.asarray(head["mean"], dtype=float)
            scale = np.asarray(head["scale"], dtype=float)
            if (
                coef.shape != (N_FEATURES + 1, 3)
                or mean.shape != (N_FEATURES,)
                or scale.shape != (N_FEATURES,)
                or not np.isfinite(coef).all()
                or not np.isfinite(mean).all()
                or not np.isfinite(scale).all()
                or np.any(scale <= 0)
            ):
                raise ValueError("invalid neural reward checkpoint dimensions")
        model.heads = head_data
        learned = payload.get("observed_trade_fingerprints")
        if not isinstance(learned, list):
            raise ValueError("missing persistent broker evidence history")
        for item in learned:
            if not isinstance(item, list) or len(item) != 3:
                raise ValueError("invalid broker evidence history entry")
            account, trade_key, fingerprint = item
            if (
                account != model.account_scope
                or not isinstance(trade_key, str) or not trade_key
                or not isinstance(fingerprint, str) or len(fingerprint) != 64
            ):
                raise ValueError("invalid recorded broker evidence")
            key = (account, trade_key)
            if key in model.observed_fingerprints:
                raise ValueError("duplicate persisted evidence identity")
            model.observed_fingerprints[key] = fingerprint
        model.observed_ids = set(model.observed_fingerprints)
        model.online_updates = int(payload.get("online_updates", -1))
        if model.online_updates < 0 or model.online_updates > len(model.observed_ids):
            raise ValueError("invalid online learning update counter")
        return model

    def artifact(self) -> dict:
        payload = {
            "schema": "abutron.v9.broker_net_49.research.v1",
            "market_feature_width": N_FEATURES, "identity_slot": self.identity_slot,
            "min_side_trades": self.min_side_trades, "heads": self.heads,
            "account_scope": self.account_scope,
            "observed_trade_fingerprints": [
                [account, trade_key, fingerprint]
                for (account, trade_key), fingerprint
                in sorted(self.observed_fingerprints.items())
            ],
            "online_updates": self.online_updates,
            "status": "RESEARCH_ONLY_NOT_VALIDATED_FOR_BROKER_EXECUTION",
            "orders_sent": 0, "production_model_promoted": False,
        }
        payload["sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return payload


def group_results(rows: Sequence[ClosedTrade], predictions: Sequence[float]) -> dict:
    if len(rows) != len(predictions):
        raise ValueError("prediction/settlement length mismatch")
    result = {}
    for key, take in (("predicted_positive", True), ("predicted_nonpositive", False)):
        group = [r for r, p in zip(rows, predictions) if (p > 0) == take]
        result[key] = {
            "trades": len(group),
            "broker_net_usd": round(sum(r.net_usd for r in group), 4),
            "mean_net_usd": round(sum(r.net_usd for r in group) / len(group), 4)
                if group else None,
            "stop_loss_exits": sum(r.stop_loss_exit for r in group),
            "take_profit_exits": sum(r.take_profit_exit for r in group),
            "independent_m5_entry_candles": len({r.closed_m5_bucket for r in group}),
        }
    return result
