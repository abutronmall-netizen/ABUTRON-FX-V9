# Deriv DEMO recovery verification checklist

Related issue: #65.

This checklist describes the existing system behavior to verify after a supported administrative release. It does not add an approval gate to the trading engine.

- [ ] Confirm the reviewed release matches its approved source and rollback copy.
- [ ] Confirm all three existing Deriv DEMO workers recover after a normal service restart and after a reboot test.
- [ ] Confirm encrypted credential handling, per-account isolation, and native broker identity validation remain intact.
- [ ] Confirm the CRM reflects fresh authenticated connections and current worker bindings.
- [ ] Confirm the existing owner-level exposure and risk calculation receives real broker data from every linked DEMO account.
- [ ] Confirm existing authorized DEMO risk settings and customer trading controls are unchanged.
- [ ] Confirm live configuration is unchanged.
- [ ] Confirm signal-to-execution behavior with real broker acknowledgement and protective stop/target reconciliation when a valid DEMO signal executes. Do not count a generated signal or local dispatch as a fill.

Status as of 2026-11-24: recovery implementation outstanding; broker execution not yet verified.
