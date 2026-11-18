"""APEX — LEGACY subsystem.

The ``abutron.apex`` decision stack (engine, agents, debate, gemma,
sniper, trailing, ranker, journal) is **replaced by the agentic system**
in ``abutron.agent``.

The agentic AI + brains + V9 now form the authoritative production
decision path via ``abutron.agent.production.AgenticDecisionEngine``,
which implements the same ``run_decision_pass`` -> ``List[Candidate]``
contract but routes every symbol through the hybrid multi-agent
pipeline (sense -> think -> gate -> act -> monitor -> recover) with the
V9 brains as signal authority and the unbypassable RiskGate.

This package is retained as a **legacy rollback reference**. Do not
build new decision paths on it. See ``docs/AGENTIC_SYSTEM_DESIGN.md``.
"""
