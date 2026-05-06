"""Phase 19v27 (06.05.2026) — SX `orderSizeFillable` field removed.

Continuation of v26 SX fix. Live test on VPS revealed v26 alone wasn't
enough — SX also renamed/removed the order size field:

  Old: `orderSizeFillable: <int>`
  New: `totalBetSize: <int>` + `fillAmount: <int>` → fillable = diff

v26 parser was reading `o.get('orderSizeFillable', '0')` → always '0' on
new responses → all orders filtered out by `size <= 0` → 0 maker depth
on every market → `_sum_sx_market` always None → 0 SX in NEAR.

Also added gate on new `orderStatus` field: skip orders not 'ACTIVE'
(cancelled / expired / fully matched).
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def test_sync_fetch_sx_handles_new_size_fields():
    """`_fetch_sx_orders` parser handles `totalBetSize - fillAmount`."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server._fetch_sx_orders)
    assert 'totalBetSize' in src
    assert 'fillAmount' in src
    assert 'orderStatus' in src or 'ACTIVE' in src


def test_async_fetch_sx_handles_new_size_fields():
    import inspect
    from async_fetchers import fetch_sx_orders_async
    src = inspect.getsource(fetch_sx_orders_async)
    assert 'totalBetSize' in src
    assert 'fillAmount' in src
    assert 'orderStatus' in src or 'ACTIVE' in src


def test_sync_fetch_sx_back_compat_old_field():
    """If `orderSizeFillable` is present, parser uses it (forward-compat)."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server._fetch_sx_orders)
    assert "'orderSizeFillable' in o" in src or '"orderSizeFillable" in o' in src
