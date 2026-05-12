"""Shared fixtures for the data-layer test suite.

The fixture helpers (``SyntheticTick``, ``make_synthetic_bi5``, ...) live in
``tests/data/_synthetic.py`` so individual test modules can import them by
name. Conftest only wires up the @pytest.fixture wrappers.
"""

from __future__ import annotations

import pytest

from ._synthetic import SyntheticTick, make_synthetic_bi5


@pytest.fixture
def four_eurusd_records() -> list[SyntheticTick]:
    """Four EURUSD ticks at +0ms/+250ms/+500ms/+750ms from the hour.

    All ticks have ``ask_int > bid_int`` so the parser's invariant holds.
    Prices around 1.09510 (EURUSD digits=5).
    """
    return [
        SyntheticTick(ms_from_hour=0, ask_int=109512, bid_int=109510, ask_vol=1.0, bid_vol=1.0),
        SyntheticTick(ms_from_hour=250, ask_int=109513, bid_int=109511, ask_vol=2.0, bid_vol=1.5),
        SyntheticTick(ms_from_hour=500, ask_int=109514, bid_int=109512, ask_vol=0.5, bid_vol=2.0),
        SyntheticTick(ms_from_hour=750, ask_int=109515, bid_int=109513, ask_vol=1.0, bid_vol=1.0),
    ]


@pytest.fixture
def four_eurusd_bi5(four_eurusd_records: list[SyntheticTick]) -> bytes:
    """LZMA-compressed bytes of the four-tick EURUSD fixture."""
    return make_synthetic_bi5(four_eurusd_records)
