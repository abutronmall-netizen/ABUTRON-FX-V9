# FP Markets terminal startup failure — 24 November 2026

## Observed on Ubuntu
The deployed worker is derived from commit `02bbf13`; installed SHA256: `bdc219f3212c378d44eb62391f68b132a60e962c96132735a76e349aabd89bd2`. The accompanying worker source is anonymized for public review, not a deployment artifact.

The client-initiated LIVE read-only connection at 19:14 UTC started MT5 three times. Each launch completed startup config initialization, reported build 6295, began full recompilation, and shut down before a broker-native authenticated heartbeat. Launcher exit 69; terminal journal stopped/shutdown 10053. The retry patch is working, but does not solve the repeated compilation interruption.

| UTC | Journal |
|---|---|
| 19:14:35.859 | Compiler: full recompilation has been started |
| 19:14:36.732 | Terminal: exit with code 0 |
| 19:14:36.799 | Terminal: stopped with 10053 |
| 19:14:43.516 | Compiler: full recompilation has been started |
| 19:14:44.601 | Terminal: stopped with 10053 |
| 19:14:52.225 | Compiler: full recompilation has been started |
| 19:14:53.054 | Terminal: stopped with 10053 |

MetaEditor reported ExpertMACD.mq5 compilation with 0 errors, 0 warnings on each attempt. The corresponding EX5 was absent after shutdown. MQL5.community authorization failure is distinct from broker authentication; do not treat it as proof of an invalid broker password.

## Inventory comparison
All five inspected FP terminal installations report build 6295 and identical terminal64.exe bytes. The failed installation contains 146 MQ5 files, 105 EX5 files and 47 MQ5 files lacking same-path EX5. Two working DEMO installations contain 140–141 EX5 and 15 MQ5 without counterpart. Stock ExpertMACD.ex5 is present on all four inspected DEMO installations but missing in the failed one. These observations establish incomplete compiled assets, not the precise underlying cause of the compiler shutdown.

Template `/opt/abutron/client-mt5-template-v3/MQL5`: 0 MQ5, 5 EX5. Warm cache `/opt/abutron/fpmarkets-mql-bootstrap-v1`: 99 MQ5, 102 EX5; no MQ5 lacking counterpart. Worker copies only selected warm-cache subdirectories. Inspect `main()` stock bootstrap, `stage_fp6364_stock_compiler_assets`, the named read-only chart profile preparation and the pre-authentication compilation lifecycle.

## Review scope
Investigate stock compiler preparation with isolated offline non-broker fixtures. No account credentials, terminal profiles, broker journals, private data or proprietary executable files are included. Preserve account isolation, authentication, existing risk settings and LIVE OFF. Do not deploy this anonymized source. Do not copy another client's terminal profile or cached broker data. No increase to retry count is justified by the observed results. Actual broker connectivity and exposure remain unverified.
