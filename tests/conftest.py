"""Synthetic-only test configuration; no production secret is provided."""
import os

# Pure in-memory/temporary-DB tests require an explicit dummy HMAC key.
# Runtime code never reads a built-in fallback signing key.
os.environ.setdefault(
    "ABUTRON_ACTIVATION_SECRET",
    "ABUTRON_TEST_ONLY_DO_NOT_USE_ON_REAL_TERMINALS_2026",
)
