# Issue #12 repair status

This source repair connects the repository's calibrated NexusBrain inference to
APEX candidate direction, restores all 13 configured Deriv synthetic symbols,
uses account-configured risk percentages in the candidate review, and removes
synthetic manager ACKs. CRM percentage values are converted from percentage
points to fractions before sizing and review. The configured risk and portfolio
limits remain bounded by the V9 review contract.

The runtime loads only a calibrated `nexus_brain.json` from
`ABUTRON_BRAIN_DIR` (default: `data/brain`). If the artifact is absent,
uncalibrated, or still warming up, the result is HOLD. A `LOSS_PATTERN_REPEAT`
blocker is also enforced as HOLD. The separately deployed UnifiedBrain artifact
and its checksum are not present in this checkout, so this branch cannot claim
that exact external artifact has been loaded or verified here.

The manager IPC now uses the same candidate-bound signature shape as the
single-writer gateway, but the manager intentionally rejects order handoffs
with `NATIVE_ORDER_TRANSPORT_UNAVAILABLE`. This repository still has no
verified account-bound MT5 `OrderSend` / broker receipt adapter, and no
broker-confirmed protected DEMO order was attempted or established. Therefore
this is not a production execution-ready release. LIVE account linking defaults
off and remains separately opt-in; do not enable LIVE or infer execution
readiness from a READY candidate.
