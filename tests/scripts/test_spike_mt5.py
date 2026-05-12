"""Regression test for scripts/spike_mt5.py's _build_close_req helper.

History: the original spike used ``{**open_req, "action": ..., "type": ...,
"position": ..., "price": ...}`` to build the close-leg trade request,
which silently inherited the open leg's ``sl`` and ``tp`` fields. On a
live FTMO demo run that produced retcode 10016 (INVALID_STOPS) and
failed the close. The helper now builds the close request from scratch
with ``sl=0.0`` / ``tp=0.0`` explicit, and this test pins that
behavior so a future refactor cannot regress it.

The helper is imported via ``importlib.util`` because ``scripts/`` is
not a package — the script lives at the repo root for ergonomics, not
as part of the ``propfarm`` package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPIKE_PATH = _REPO_ROOT / "scripts" / "spike_mt5.py"


def _load_spike() -> ModuleType:
    """Load scripts/spike_mt5.py as a module without executing main()."""
    spec = importlib.util.spec_from_file_location("spike_mt5", _SPIKE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample_open_req() -> dict[str, Any]:
    """A realistic open-leg request mirroring what spike_mt5.main() builds."""
    return {
        "action": 1,  # mt5.TRADE_ACTION_DEAL
        "symbol": "EURUSD",
        "volume": 0.01,
        "type": 0,  # mt5.ORDER_TYPE_BUY
        "price": 1.10000,
        "sl": 1.09800,  # 20 pips below — the value that caused INVALID_STOPS
        "tp": 1.10400,  # 40 pips above
        "deviation": 10,
        "type_filling": 2,  # mt5.ORDER_FILLING_IOC
    }


def test_close_req_zeros_sl_and_tp() -> None:
    """The bug that produced retcode 10016 on the first live run."""
    spike = _load_spike()
    open_req = _sample_open_req()
    close = spike._build_close_req(
        open_req,
        opposite_type=1,  # mt5.ORDER_TYPE_SELL
        position_ticket=447052140,  # real ticket from the failed run
        close_price=1.09950,
    )
    assert close["sl"] == 0.0, f"close_req inherited sl={close['sl']} from open; bug regressed"
    assert close["tp"] == 0.0, f"close_req inherited tp={close['tp']} from open; bug regressed"


def test_close_req_preserves_immutable_open_fields() -> None:
    """symbol, volume, action, deviation, type_filling must match the open leg."""
    spike = _load_spike()
    open_req = _sample_open_req()
    close = spike._build_close_req(
        open_req, opposite_type=1, position_ticket=12345, close_price=1.09950
    )
    assert close["symbol"] == open_req["symbol"]
    assert close["volume"] == open_req["volume"]
    assert close["action"] == open_req["action"]
    assert close["deviation"] == open_req["deviation"]
    assert close["type_filling"] == open_req["type_filling"]


def test_close_req_sets_close_specific_fields() -> None:
    """type, position, price must be the close-leg values, not the open's."""
    spike = _load_spike()
    open_req = _sample_open_req()
    close = spike._build_close_req(
        open_req, opposite_type=1, position_ticket=12345, close_price=1.09950
    )
    assert close["type"] == 1  # SELL — opposite of the BUY open
    assert close["position"] == 12345
    assert close["price"] == 1.09950
    # The open's price (1.10000) must NOT appear in the close request.
    assert close["price"] != open_req["price"]


def test_close_req_does_not_mutate_open() -> None:
    """The helper must build a new dict, not modify open_req in place."""
    spike = _load_spike()
    open_req = _sample_open_req()
    original = dict(open_req)
    spike._build_close_req(open_req, opposite_type=1, position_ticket=12345, close_price=1.099)
    assert open_req == original


def test_close_req_does_not_have_extra_keys() -> None:
    """The close-leg dict should contain exactly the documented keys, no
    leftover from a partial spread. If the helper ever switches to a
    spread-and-override, this test catches an extra key sneaking in."""
    spike = _load_spike()
    open_req = _sample_open_req()
    close = spike._build_close_req(
        open_req, opposite_type=1, position_ticket=12345, close_price=1.099
    )
    expected_keys = {
        "action",
        "symbol",
        "volume",
        "type",
        "position",
        "price",
        "deviation",
        "type_filling",
        "sl",
        "tp",
    }
    assert set(close.keys()) == expected_keys
