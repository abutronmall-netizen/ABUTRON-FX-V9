# Disk-pressure companion to historical evidence incident (2026-11-18)

Related [issue #24](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/issues/24) and [historical evidence issue #22](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/issues/22).

## Verified capacity

- Ubuntu root partition: approximately 323 GB total; ~290 GB used; ~17 GB free (95%).
- Largest evidence-bearing storage class: isolated MT5 state in `/var/lib/private` (~138 GB). These directories may contain current terminal workers, broker-native history, source-original ACK records, authenticated proof and session-bound credentials.
- Historical terminal recovery and local backup trees have **not** been deleted.
- High-volume syslog contributed materially: ~1.5 GB previous rotated syslog plus ~1 GB current syslog before the maintenance.
- We rotated/compressed logs using the existing rsyslog recipe and its global `su root adm` context; service `rsyslog` and `logrotate.timer` remained active. Free space increased from ~16 GB to ~17 GB.
- Backed up `/etc/logrotate.d/rsyslog` as a host-local recovery copy, then adjusted it from `rotate 4` to `rotate 28` and added `maxsize 250M` while retaining `weekly`, `compress`, and `delaycompress`. Dry-run confirmed rotations will run earlier when logs exceed 250 MB without dropping the configured 28-cycle archive retention. No trading service was changed.

## Remaining storage blockers

A further reclamation plan needs a verified *offsite restic snapshot* and **isolated restore drill** before any retired terminal or recovery tree is destroyed or pruned. The ~11 GB retired FP LIVE terminal is a candidate for **investigation**, not automatic deletion. PR engineering worktrees may contain unsaved patches; inspect repository status/commits first. Do not delete original broker receipts or EA evidence to create free space.

## Evidence to attach privately

- Per-folder pre/post disk and inode reports and service state.
- Exact account-worker active/retired service inventory.
- Encrypted snapshot ID, snapshot verification and restore-check manifest for selected retired files (no credentials in public issues).
- Preservation and retention rules for native broker history, source-original ACKs and audit logs.

No new certifying broker facts were created by the storage maintenance. `BLOCKED_INCOMPLETE_EXPOSURE` stays in force. FP LIVE x9392 execution remains OFF, as requested.
