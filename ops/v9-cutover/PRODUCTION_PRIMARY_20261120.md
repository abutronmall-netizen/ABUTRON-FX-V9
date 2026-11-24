# V9 primary production cutover — 20 November 2026

## Verified implementation

The authorized Ubuntu host now runs V9 executable paths for the main **Deriv DEMO 6207** trading decision, CRM activation, MT5 session manager, native three-Deriv broker reconciliation and owner-wide native risk reporting. Existing systemd unit names are intentionally kept as compatibility slots so downstream dependencies and the customer portal do not break.

- `abutron-v9-production-6207.service` is the broker-native V9 trained-brain execution worker. Production source began from server-local `8a37ed2` and includes preserved native fill-price and ticket reconciliation fixes.
- `abutron-6207-native-activation-ack.service` now executes `/opt/abutron-v9-native-runtime/activation_6207.py --ack`. CRM acknowledged `V9_NATIVE_DEMO_READY_ACK`, not APEX11 supervisor approval.
- `abutron-client-mt5-manager.service` now executes `/opt/abutron-v9-native-runtime/mt5_session_manager.py`. All eight authenticated MT5 worker processes kept the **same PIDs**, and their heartbeats remained CONNECTED on the immediate post-cutover check. The authenticated Unix-socket probe and public portal HTTPS returned HTTP 200.
- `abutron-customer-wide-risk-readonly.service` now executes `/opt/abutron-v9-native-runtime/native_owner_risk_publisher.py`. That source is currently **byte-identical** to the legacy publisher and still uses existing CRM owner mappings. Its report has verified eight connected broker accounts, eight position sets, and zero unknown sets on healthy passes. This is path/ownership migration, **not a risk-algorithm rewrite**.
- `abutron-deriv-three-native-reconcile-ro.service` executes the V9 signed-native source matching implementation through `90-v9-native-source.conf`. On healthy passes all three Deriv DEMO accounts reconcile.

## Removed an obsolete timing veto

Legacy `abutron-issue38-deriv6207-exposureqa.timer` runs only every **300 seconds**. The old V9 activation, CRM binding and execution session code required a QA health report less than **70–75 seconds** old. That rejected current real broker evidence for parts of the five-minute reporting cycle.

Three production-source locations now validate the **actual current HMAC-signed MT5 native exposure** directly against real account binding and the installed signing secret. The pinned native EA binary, execution-mode preset, client ON/OFF, broker contract, owner current exposure and risk limits continue to apply. **A truly stale or missing signed broker snapshot still blocks execution.**

The isolated PR37 writer has shown restart gaps, including an exit `6/NOTCONFIGURED`; this can still interrupt native signed snapshots. No false successful fills or trailing amendments have been claimed.

## Evidence and regression

- Staged isolated Git worktree from production `8a37ed2`: **665 passed, 4 skipped** with normal pytest defaults.
- New focused regression `tests/test_v9_native_qa_timing.py`: **3 passed**. Verifies old QA health is not consulted; stale signed native exposure is still rejected in V9 execution and CRM binding.
- Server production files replaced atomically after tests, with root-private prior versions under `/var/backups/v9-native-qa-freshness-20261120T121208Z`.
- Service compatibility overrides are included in `ops/v9-cutover/systemd/*-compat.conf`. Use existing service/timer names; do not enable redundant alternative managers/activation timers simultaneously.

## Explicit limits

**V9 is the primary automatic decision and native order route on Deriv DEMO 6207, not yet a complete V9 execution replacement across all accounts.** Other Deriv DEMO/FP accounts still use legacy terminal worker/feed and account-specific execution paths until independently migrated. Client/admin portal compatibility services still use APEX paths. The user-requested 20% drawdown ceiling remains distinct from 2% aggregate-open stop-risk policy; the 6207 configured per-trade risk remains 0.15%. No LIVE trading settings were altered, no retired accounts restored, and no broker order was forced by this cutover.

When a broker-side publisher stops issuing fresh signed snapshots, V9 may still return `PRODUCTION_INPUT_OR_EXECUTION_ERROR` until current native evidence returns. This is a real broker-source availability issue, not the removed slow QA health timer dependency.
