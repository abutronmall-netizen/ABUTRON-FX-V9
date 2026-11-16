# ABUTRON FX V9 f678a67 unified checkpoint compatibility fix

Verified 2026-11-16 on the authorized Ubuntu production host.

## Why this patch is necessary

The exact user branch arena/0f695860-abutron-fx-v9 at commit
f678a6736d35178e266f684644855b537e70833c loads the existing
unified M5 checkpoint successfully through the older load_brain() function,
but its original NexusBrain.decide() then supplies only 23 features to
a trained ensemble with 49 input dimensions. Authentic stored Deriv
Volatility 100, Crash 1000, and Boom 1000 closed M5 candles reproduced
a NumPy matrix-size ValueError (size 49 differs from 23).

## Scope of the repair

- Add a validated Unified49Brain adapter using the exact saved model weights,
  23 market features plus the saved 26-symbol one-hot encoding.
- Reuse the previously verified causal signals.py and loss_guard.py source
  from the already running V9 release, byte-identically, to populate nine
  trained signal slots and restore the learned loss-pattern HOLD state.
- Use the adapter in ApexIntegration, never the legacy 23-feature brain
  for this 49-feature checkpoint.
- When no fitted artifact is present, the existing HOLD behavior remains.
- The signed IPC, manager order rejection, native stops, redesigned risk
  settings, and LIVE linking settings supplied in f678a67 are unchanged.
- The branch itself contains NO trained model JSON; production must
  explicitly set ABUTRON_BRAIN_DIR to a validated mounted checkpoint.

## Host checks

- Original trained model: 456651 bytes.
- SHA256: 2667997b1498aae316c4103ec1f3d0bd2efb805f034836455ed10019284302df
- Native M5 inference: 26/26 modeled instruments, 13 Deriv and 13 FP.
- Learned loss-guard state restored: 548 active patterns.
- Isolated unit suite: 194 passed (one dependency warning), including five
  additional 49-feature regression cases.
- All activities on a separate checkout: no existing MT5 order/CRM/LIVE
  services or broker positions changed.

## Remaining order integration

The f678a67 main manager deliberately rejects handoffs with
NATIVE_ORDER_TRANSPORT_UNAVAILABLE. Signed IPC shape alone is not
an authentic MT5 OrderSend/receipt adapter. Native production
abutron-apex-native-orders.service is still failed with owner/ACK
unverified, and customer risk remains BLOCKED_INCOMPLETE_EXPOSURE.

The previously verified trained read-only model schedule remains active
on immutable release a88f373. This fix must not be represented as a
broker-confirmed DEMO or LIVE execution deployment.
