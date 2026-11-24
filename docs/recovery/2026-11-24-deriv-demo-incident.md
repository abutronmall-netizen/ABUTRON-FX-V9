# Deriv DEMO session recovery incident

Verified 2026-11-24 (UTC). Related issue: #65.

## Findings
- Following reboot, three Deriv DEMO CRM-managed terminal workers failed to start because their service identity could not open the worker script. Separate broker-native exposure observers continued reporting fresh authenticated data.
- The existing session manager is running, but its automatic recovery did not restore these DEMO sessions.
- FP Markets read-only market-data collection had an unrelated exporter release mismatch. The collector was aligned to the installed known release; all four DEMO accounts subsequently reported 13/13 symbols and both existing neural inference services completed successfully.
- Execution for accounts sharing incomplete owner-level broker exposure remains unavailable. This is not evidence of a neural inference failure.

## Affected components
- `mt5_session_manager.py` (CRM session management)
- `worker_feed_v4_usd50_canary.py` (Deriv DEMO terminal worker)
- Existing native exposure and CRM reconciliation services

## Current state
The session-manager source change has not been deployed. This incident note is documentation, not a patch or deployment script. It deliberately excludes production account identifiers, secrets, private broker data, and machine-specific access configuration.
