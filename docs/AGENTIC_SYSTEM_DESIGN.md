# ABUTRON FX V9 — Agentic AI Hybrid Multi-Agent System

This document describes the agentic orchestration layer that unifies the
ABUTRON FX V9 platform into a single **hybrid multi-agent system**, and
records the standing directive about V9's role.

## Standing directive: V9 is the main system

**V9 is the main system for ABUTRON FX.** The agentic AI — once built,
tested, and deployed — is the **primary operating layer** on top of V9,
not a replacement for it and not a permanently constrained add-on.

Two distinct things must not be conflated:

| | Fail-closed / evidence-only | Risk invariants |
|---|---|---|
| **What** | The *development & deployment safety posture*: frozen brains, read-only CRM, no live trading, shadow/record-only operation. | The *money-safety invariants*: 0.15% per-trade cap, 20% portfolio ceiling, R:R ≥ 1.50, tick-value verification. |
| **When it applies** | The current baseline and the build/test/deploy phase. | **Always** — during build/test/deploy **and** post-deploy. |
| **End state** | **Lifted** after the agentic AI is built, tested, and deployed. | **Never lifted.** V9's risk engine is the backbone the agentic AI runs on top of. |

The agentic AI may become the primary *operating* layer, but it never
bypasses the risk invariants. **V9 is the main system because its risk
engine is the backbone; the agentic AI is the orchestrating layer on top.**

## Architecture

The system composes the existing V9 subsystems rather than replacing
them:

```
                      ┌──────────────────────────────────────────┐
                      │           AgentOrchestrator               │
                      │   sense -> think -> gate -> act ->        │
                      │   monitor -> recover                      │
                      └───────┬─────────┬──────────┬──────────────┘
                              │         │          │
        signal substrate      │         │          │  evidence
   (abutron.brain)           ▼         ▼          ▼
  NexusBrain / UnifiedBrain  Reasoning  RiskGate  DecisionLedger
  (NexusBrain.predict)        backend  (RiskGuard +
                                   LLM (Gemma/Debate)   hard invariants)
                                            │
                                            ▼
                                  ExecutionAgent
                                  (native dispatcher)
```

### Pipeline stages

| Stage | Agent | Owns |
|-------|-------|------|
| **sense** | `SignalAgent` | Produce the brain `Signal` from the substrate. |
| **think** | `OrchestratorAgent` | Call the reasoning backend; synthesise intent; build a gate-able `AgentAction`. |
| **gate** | `RiskAgent` | Evaluate the action through the **unbypassable** `RiskGate`. |
| **act** | `ExecutionAgent` | Dispatch only if the gate approved; shadow-record by default. |
| **monitor** | `MonitoringAgent` | Append the decision to the evidence `DecisionLedger`. |
| **recover** | `RecoveryAgent` | Fail-soft handling; quarantine failed cycles. |

Each stage runs through `Agent._safe_act`, so a single agent's failure
degrades to a recorded error rather than aborting the cycle.

### The pluggable reasoning layer

`ReasoningBackend` is an abstract contract with two implementations:

* **`LLMReasoningBackend`** — wraps the existing V9 LLM advisors
  (`abutron.apex.gemma` single advisor + `abutron.apex.debate`
  multi-agent bull/bear debate). Advisory-only. On any outage it
  degrades to a rule synthesis — the decision cycle is never blocked by
  a missing model gateway.
* **`RuleReasoningBackend`** — a deterministic, fully self-contained
  reasoner. Applies a confidence floor, an uncertainty veto, and
  order-flow/spread adjustments. Always available; used in CI, offline,
  and sandbox. Every rule is recorded in `considerations` for audit.

`LLMReasoningBackend.from_config().or_fallback()` returns the LLM
backend when a gateway is configured, else the rule backend — so
`build_agent_system()` works everywhere with no configuration.

### The unbypassable RiskGate

`RiskGate` is the backbone and the embodiment of "V9 is the main
system." Every action that can affect money or broker state passes
through it. It:

1. Checks stop/target geometry.
2. Enforces the R:R ≥ 1.50 floor.
3. Delegates to the V9 `RiskGuard` (per-trade cap, position size,
   concentration, exposure) when available.
4. **Independently re-enforces** the hard invariants from the config
   constants — so even a misconfigured or absent guard cannot let a
   violation through. The gate can never be talked out of a rejection.

Read-only / record actions (`requires_gate=False`) skip money checks.

### The brain signal substrate

`BrainSubstrate` wraps `NexusBrain` / `UnifiedBrain` and exposes a
single `predict` call returning a `Signal`. If no trained checkpoint is
present, it produces a transparent **rule-based** signal marked
`brain_source = "rule_fallback"`. It never fabricates a trained-brain
signal it does not have — provenance is always explicit so the ledger
can audit whether a signal came from a real model or a rule.

### Execution

`ExecutionAgent` is the only component that can touch broker state. In
**shadow mode** (default) it records a simulated receipt and never
contacts a broker. In **live mode** it dispatches through the V9 native
single-writer dispatcher — and only after the gate approved. It is
hard-wired to refuse any action the gate did not approve.

## Build / test / deploy trajectory

1. **Build** — the agentic layer is developed under the fail-closed,
   evidence-only posture. Shadow mode only.
2. **Test** — the 21-test suite (`tests/test_agent_system.py`) covers
   the full pipeline, both reasoning backends, the gate invariants, and
   fail-soft recovery, all offline.
3. **Deploy** — deploy under shadow/evidence-only. Validate against
   authenticated DEMO staging.
4. **Promote to primary** — after the agentic AI is built, tested, and
   deployed, it becomes the primary operating layer. Fail-closed /
   evidence-only is lifted as the operating mode; the risk invariants
   remain deterministic and unbypassable.

## Key invariants (never lifted)

- Per-trade risk cap: 0.15% of equity.
- Owner portfolio ceiling: 20%.
- Reward:risk ratio floor: 1.50.
- `RiskGate` is always in the path.
- Every decision is journalled in the `DecisionLedger`.

## Module map

```
src/abutron/agent/
  __init__.py         public API + build_agent_system()
  interfaces.py       AgentContext, Signal, AgentAction, AgentDecision, ...
  base.py             Agent base (lifecycle, capabilities, _safe_act)
  reasoning.py        ReasoningBackend, LLMReasoningBackend, RuleReasoningBackend
  substrate.py        BrainSubstrate (wires abutron.brain)
  risk_gate.py        RiskGate (wires abutron.risk, unbypassable)
  memory.py           DecisionLedger (evidence)
  orchestrator.py     AgentOrchestrator (the runtime)
  agents/
    signal_agent.py       sense
    orchestrator_agent.py think
    risk_agent.py         gate
    execution_agent.py    act
    monitoring_agent.py   monitor
    recovery_agent.py     recover
```
