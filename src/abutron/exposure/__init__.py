"""Native MT5 exposure publisher (Issue #36).

Binds exposure certification to authenticated broker-native evidence.
See :mod:`abutron.exposure.publisher`.
"""
from .publisher import publish_exposure

__all__ = ["publish_exposure"]
