# ABUTRON V9 Deriv DEMO 7593/9244 — production cutover 2026-11-20

Authorization: DEMO-only. Leave 6207 and LIVE unchanged. V4 check-only guards retained.

Source baseline: GitHub commit 4121745. Execution now uses the same broker-native sizing, signed writer arming, and receipt path proven in 6207, alongside the existing generic runtime for unrelated callers.

Production V9 source checkout: /opt/abutron-v9-migration-qa-20261120/v9-deriv-7593-9244-4121745

Owner-risk source (shared):
/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json

Cost sources:
/var/lib/abutron-deriv-v4-7593/v4-native-execution-cost-7593.json
/var/lib/abutron-deriv-v4-9244/v4-native-execution-cost-9244.json

For each suffix 7593 and 9244, the isolated QA root is:
/var/lib/private/abutron-pr37-direct-writer-demo{suffix}-qa

Per-account state: /var/lib/abutron-v9-production-{suffix}

Systemd units per suffix:
- abutron-pr37-direct-writer-demo{suffix}-qa.service (signed native writer, DEMO execution and trailing preset)
- abutron-v9-deriv-demo-{suffix}-activation.service and .timer (CRM native ACK)
- abutron-v9-deriv-demo-{suffix}.service and .timer (V9 decision/dispatch cycle)
All QA units and both timer types enabled for reboot persistence.

Checks: full 676 passed, 4 skipped. Both V9 preflights and execute cycles succeeded; first execute cycles sent zero orders with zero unknown or unresolved receipts. No broker DEMO fill or trailing modification from these runners yet demonstrated. Native Deriv reconciliation all 3 DEMO verified; 8/8 owner exposure sets reconciled. FP LIVE 9392 connected and unchanged.

Rollback scope: stop/disable the two new V9 decision timers to halt their new orders, then stop/disable their respective activation timers and signed QA writers if reversing the whole DEMO cutover. Do not stop 6207, LIVE, shared owner-risk publisher, or existing V4 check-only services.

No secrets or account connection IDs are documented here.