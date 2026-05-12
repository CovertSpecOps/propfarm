"""Synthetic Dukascopy ``.bi5`` byte generators for offline unit tests.

Centralized here (not in conftest.py) so they can be imported by name from
test modules. Production code MUST NOT import from this module.

Wire-format reference (Dukascopy public datafeed):

* Each tick is a 20-byte big-endian record packed as ``>IIIff``:
  ``(ms_from_hour: uint32, ask_int: uint32, bid_int: uint32,
     ask_vol: float32, bid_vol: float32)``.
* The whole hour's records are concatenated and then LZMA-compressed with the
  raw (no-header) ``FORMAT_ALONE`` codec — that is what
  ``lzma.compress(data, format=lzma.FORMAT_ALONE)`` produces and what
  ``parse_bi5`` decompresses.
"""

from __future__ import annotations

import lzma
import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class SyntheticTick:
    """One Dukascopy tick in *raw integer* form (pre-digit-scaling)."""

    ms_from_hour: int
    ask_int: int
    bid_int: int
    ask_vol: float
    bid_vol: float


def _pack_records(records: list[SyntheticTick]) -> bytes:
    """Concatenate ``records`` into raw, uncompressed Dukascopy tick bytes."""
    return b"".join(
        struct.pack(">IIIff", r.ms_from_hour, r.ask_int, r.bid_int, r.ask_vol, r.bid_vol)
        for r in records
    )


def make_synthetic_bi5(records: list[SyntheticTick]) -> bytes:
    """Return LZMA-compressed bytes that the production parser can decode.

    Uses ``lzma.FORMAT_ALONE`` to match Dukascopy's actual wire format
    (legacy LZMA1, no xz header).
    """
    return lzma.compress(_pack_records(records), format=lzma.FORMAT_ALONE)


def make_raw_records(records: list[SyntheticTick]) -> bytes:
    """Return *uncompressed* tick bytes — useful for parser-only unit tests."""
    return _pack_records(records)
