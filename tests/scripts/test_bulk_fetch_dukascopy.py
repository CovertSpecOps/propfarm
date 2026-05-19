"""Offline-only unit tests for ``scripts/bulk_fetch_dukascopy.py``.

Loads the script via ``importlib.util`` (same pattern as
``tests/scripts/test_record_fills.py``) because ``scripts/`` is not a package.

The HTTP boundary is faked with a small stub satisfying the
:class:`propfarm.data.vendors.dukascopy.HttpClient` protocol — every test in
this module runs without touching the network. The single live integration
test (``@pytest.mark.integration``) is skipped by default; it executes only
under ``pytest -m integration``.

Performance note
----------------
A naive single-year run walks 8760+ hours. The autouse ``_no_sleep``
fixture replaces ``time.sleep`` inside the bulk-fetch module so a unit
test completes in <1s instead of 8+ minutes at the minimum sleep-ms.
"""

from __future__ import annotations

import importlib.util
import lzma
import struct
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from typer.testing import CliRunner

from propfarm.data.vendors.dukascopy import (
    DukascopyError,
    _build_url,
    _decompress_bi5,
    parse_bi5,
)


# Inline copy of `tests/data/_synthetic.py` helpers. ``tests/`` is not a
# package so we can't ``from tests.data._synthetic import ...``; duplicating
# the trivial 20-byte-record packer here is cheaper than wrestling with
# sys.path manipulation in every test invocation.
@dataclass(frozen=True)
class SyntheticTick:
    ms_from_hour: int
    ask_int: int
    bid_int: int
    ask_vol: float
    bid_vol: float


def make_synthetic_bi5(records: list[SyntheticTick]) -> bytes:
    """Return LZMA-FORMAT_ALONE compressed bytes (Dukascopy's wire format)."""
    packed = b"".join(
        struct.pack(">IIIff", r.ms_from_hour, r.ask_int, r.bid_int, r.ask_vol, r.bid_vol)
        for r in records
    )
    return lzma.compress(packed, format=lzma.FORMAT_ALONE)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "bulk_fetch_dukascopy.py"


def _load_module() -> ModuleType:
    """Load ``scripts/bulk_fetch_dukascopy.py`` as a fresh module."""
    spec = importlib.util.spec_from_file_location("bulk_fetch_dukascopy", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bulk_fetch_dukascopy"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``time.sleep`` inside the bulk-fetch module with a no-op."""
    bf = _load_module()
    monkeypatch.setattr(bf.time, "sleep", lambda *_a, **_k: None)


# --------------------------------------------------------------------------- #
# Stub HTTP client
# --------------------------------------------------------------------------- #
class _StubHttpClient:
    """A configurable :class:`HttpClient` stub for tests.

    ``responses`` maps a URL to either:

    * ``bytes`` — return these bytes on call.
    * a callable ``Callable[[], bytes]`` — invoked per request; useful for
      "fail twice then succeed" patterns. The callable may raise
      :class:`DukascopyError` to test the retry path.

    Unrecognized URLs return ``b""`` (matching Dukascopy's empty-hour
    convention) so tests can stub only the URLs they care about. This is
    what real Dukascopy does for weekend hours, holidays, pre-2010 sparse
    hours, etc.

    Tracked
    -------
    * ``calls`` — every URL fetched (one entry per network call).
    * ``non_empty_calls`` — URLs that returned non-empty bytes.
    * ``raise_counts`` — per-URL count of DukascopyError raises (so tests
      can assert exactly how many retries the script issued).
    """

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses: dict[str, Any] = responses or {}
        self.calls: list[str] = []
        self.non_empty_calls: list[str] = []
        self.raise_counts: dict[str, int] = {}

    def fetch_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self.responses:
            return b""
        resp = self.responses[url]
        try:
            result = resp() if callable(resp) else resp
        except DukascopyError:
            self.raise_counts[url] = self.raise_counts.get(url, 0) + 1
            raise
        assert isinstance(result, bytes)
        if len(result) > 0:
            self.non_empty_calls.append(url)
        return result


def _one_record_bi5() -> bytes:
    """A 1-record synthetic ``.bi5`` payload (parser-decodable)."""
    return make_synthetic_bi5(
        [SyntheticTick(0, ask_int=109512, bid_int=109510, ask_vol=1.0, bid_vol=1.0)]
    )


# --------------------------------------------------------------------------- #
# Cache-layout tests
# --------------------------------------------------------------------------- #
def test_bulk_fetch_writes_to_correct_cache_layout(tmp_path: Path) -> None:
    """Non-empty hour on 2020-02-15 10:00 UTC → file at the right path.

    Note Dukascopy's 0-indexed month: February = ``01`` in the on-disk path.
    Other hours of the year come back as ``b""`` from the stub (matching
    Dukascopy's empty-hour convention) and land on disk as 0-byte markers.
    """
    bf = _load_module()
    raw_root = tmp_path / "raw"
    hour = datetime(2020, 2, 15, 10, tzinfo=UTC)
    url = _build_url("EURUSD", hour)
    bi5 = _one_record_bi5()
    stub = _StubHttpClient({url: bi5})

    bf.run_bulk_fetch(
        symbols=["EURUSD"],
        year_min=2020,
        year_max=2020,
        raw_root=raw_root,
        sleep_ms=50,
        max_retries=1,
        http_client=stub,
    )

    expected = raw_root / "EURUSD" / "2020" / "01" / "15" / "10h_ticks.bi5"
    assert expected.is_file()
    assert expected.read_bytes() == bi5
    # Only the one URL returned non-empty bytes.
    assert stub.non_empty_calls == [url]


def test_bulk_fetch_empty_response_written_as_zero_byte_marker(tmp_path: Path) -> None:
    """Zero-byte response from the stub → 0-byte file on disk.

    The bulk fetcher MUST persist empty hours as 0-byte markers so a resume
    scan can distinguish "checked, legitimately empty" (skip) from "fetch
    failed" (retry).
    """
    bf = _load_module()
    raw_root = tmp_path / "raw"
    stub = _StubHttpClient({})  # all URLs → b""

    bf.run_bulk_fetch(
        symbols=["EURUSD"],
        year_min=2020,
        year_max=2020,
        raw_root=raw_root,
        sleep_ms=50,
        max_retries=1,
        http_client=stub,
    )

    target = raw_root / "EURUSD" / "2020" / "00" / "01" / "00h_ticks.bi5"
    assert target.is_file()
    assert target.stat().st_size == 0
    # 2020 is a leap year -> 366 * 24 = 8784 hours, all 0-byte markers.
    all_files = list((raw_root / "EURUSD").rglob("*.bi5"))
    assert len(all_files) == 8784
    assert all(p.stat().st_size == 0 for p in all_files)


def test_bulk_fetch_skips_already_cached_hours(tmp_path: Path) -> None:
    """Pre-populate a non-empty hour; second run's stub must not see it.

    The script's resume scan picks up any ``.bi5`` file (zero-byte or not).
    Putting a non-empty file in place before run 1 means the script must
    skip that one hour entirely. The stub answers ``b""`` for everything
    else (matching Dukascopy's empty-hour convention), so the only
    non-empty call recorded would be the target URL — and we assert that
    list is empty.
    """
    bf = _load_module()
    raw_root = tmp_path / "raw"

    # Pre-populate just one hour of 2020 with a non-empty payload. The
    # script's existing-scan picks it up; the stub never gets asked for it.
    target_hour = datetime(2020, 3, 15, 10, tzinfo=UTC)
    cached_path = raw_root / "EURUSD" / "2020" / "02" / "15" / "10h_ticks.bi5"
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    cached_path.write_bytes(b"PRE_EXISTING_FAKE_BI5")

    target_url = _build_url("EURUSD", target_hour)
    stub = _StubHttpClient({target_url: _one_record_bi5()})

    bf.run_bulk_fetch(
        symbols=["EURUSD"],
        year_min=2020,
        year_max=2020,
        raw_root=raw_root,
        sleep_ms=50,
        max_retries=1,
        http_client=stub,
    )

    # The pre-existing file is untouched (bytes preserved exactly).
    assert cached_path.read_bytes() == b"PRE_EXISTING_FAKE_BI5"
    # The stub was never asked for the cached hour's URL.
    assert target_url not in stub.calls
    # No non-empty downloads happened (the only non-empty URL in the
    # stub map was the cached one, which we skipped).
    assert stub.non_empty_calls == []


def test_bulk_fetch_retries_on_transient_error(tmp_path: Path) -> None:
    """Stub raises ``DukascopyError`` twice, then returns bytes — script must retry."""
    bf = _load_module()
    raw_root = tmp_path / "raw"

    raw_bytes = _one_record_bi5()
    target_hour = datetime(2020, 3, 15, 10, tzinfo=UTC)
    target_url = _build_url("EURUSD", target_hour)
    state = {"calls": 0}

    def flaky() -> bytes:
        state["calls"] += 1
        if state["calls"] <= 2:
            raise DukascopyError("transient")
        return raw_bytes

    stub = _StubHttpClient({target_url: flaky})

    bf.run_bulk_fetch(
        symbols=["EURUSD"],
        year_min=2020,
        year_max=2020,
        raw_root=raw_root,
        sleep_ms=50,
        max_retries=3,
        http_client=stub,
    )

    # 2 failures + 1 success = 3 attempts on the target URL.
    assert state["calls"] == 3
    final_path = raw_root / "EURUSD" / "2020" / "02" / "15" / "10h_ticks.bi5"
    assert final_path.is_file()
    assert final_path.read_bytes() == raw_bytes


def test_bulk_fetch_gives_up_after_max_retries(tmp_path: Path) -> None:
    """Stub always raises → no file written for that hour; fetch loop continues."""
    bf = _load_module()
    raw_root = tmp_path / "raw"

    target_hour = datetime(2020, 3, 15, 10, tzinfo=UTC)
    target_url = _build_url("EURUSD", target_hour)

    def always_fail() -> bytes:
        raise DukascopyError("always down")

    stub = _StubHttpClient({target_url: always_fail})

    bf.run_bulk_fetch(
        symbols=["EURUSD"],
        year_min=2020,
        year_max=2020,
        raw_root=raw_root,
        sleep_ms=50,
        max_retries=3,
        http_client=stub,
    )

    # All 3 attempts on the target URL recorded; no file written there.
    assert stub.raise_counts.get(target_url, 0) == 3
    target_path = raw_root / "EURUSD" / "2020" / "02" / "15" / "10h_ticks.bi5"
    assert not target_path.exists()
    # The rest of the year still landed (as 0-byte markers) — the failure
    # didn't halt the loop.
    other_hour_file = raw_root / "EURUSD" / "2020" / "00" / "01" / "00h_ticks.bi5"
    assert other_hour_file.is_file()
    assert other_hour_file.stat().st_size == 0


def test_bulk_fetch_eta_estimate_in_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run prints the ETA line and exits without any stub calls."""
    bf = _load_module()
    raw_root = tmp_path / "raw"

    stub = _StubHttpClient({})

    bf.run_bulk_fetch(
        symbols=["EURUSD"],
        year_min=2024,
        year_max=2024,
        raw_root=raw_root,
        sleep_ms=100,
        max_retries=3,
        dry_run=True,
        http_client=stub,
    )
    assert stub.calls == []
    captured = capsys.readouterr().out
    assert "ETA:" in captured
    assert "dry-run" in captured.lower()


def test_bulk_fetch_validates_symbol_against_supported_symbols(tmp_path: Path) -> None:
    """Invalid symbol → typer.BadParameter (non-zero exit via CliRunner)."""
    bf = _load_module()
    runner = CliRunner()
    result = runner.invoke(
        bf.app,
        [
            "--symbol",
            "DOGEUSD",
            "--year-min",
            "2024",
            "--year-max",
            "2024",
            "--raw-root",
            str(tmp_path / "raw"),
            "--dry-run",
        ],
    )
    assert result.exit_code != 0
    output = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "DOGEUSD" in output or "unknown" in output.lower()


def test_bulk_fetch_partial_failure_resume(tmp_path: Path) -> None:
    """The adversarial-reviewer's case: half-failed first run resumes cleanly.

    Setup on calendar day 2020-01-01 (Jan, ``00`` zero-indexed):

    * Hours 0..8 of Jan 1 are pre-populated as non-empty cached files
      (5 cached + 4 cached = 9 hours pretend-cached; we want 5 cached + 5
      pending and the simplest way is to pre-populate everything up to a
      cutoff). For clarity: pre-populate hours 0..4 (5 hours).
    * Hours 5..9 of Jan 1 are *pending* — the stub will answer 3 of them
      with bytes (5/6/7) and raise DukascopyError on 2 of them (8/9).
    * Every other hour of the year comes back ``b""`` from the stub
      (Dukascopy's empty-hour convention).

    First invocation:
    * Stub IS asked for hours 5/6/7/8/9 of Jan 1 (the pending non-empty
      ones). 5, 6, 7 succeed → files on disk. 8 and 9 raise repeatedly
      → no files. Stub also probes the 8779 other hours of the year and
      gets ``b""`` for each, so those become 0-byte markers on disk.
    * After run 1: hours 5/6/7 on disk as non-empty, hours 8/9 missing,
      rest of year as 0-byte markers.

    Second invocation:
    * Resume scan finds everything on disk except hours 8 and 9 of Jan 1.
    * Stub answers those two with bytes this time.
    * Exactly 2 fetches happen.
    """
    bf = _load_module()
    raw_root = tmp_path / "raw"
    year = 2020
    sym_dir = raw_root / "EURUSD"

    raw_bytes = _one_record_bi5()
    pre_cached_hours = (0, 1, 2, 3, 4)  # 5 hours pre-populated
    success_hours = (5, 6, 7)  # 3 hours that succeed on run 1
    fail_hours = (8, 9)  # 2 hours that fail on run 1, succeed on run 2

    # Pre-populate the 5 cached hours of Jan 1.
    for h in pre_cached_hours:
        p = sym_dir / f"{year:04d}" / "00" / "01" / f"{h:02d}h_ticks.bi5"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw_bytes)

    # Build URL map for the 5 pending hours.
    pending_urls = {
        h: _build_url("EURUSD", datetime(year, 1, 1, h, tzinfo=UTC))
        for h in success_hours + fail_hours
    }

    # Run-1 responses: success_hours return bytes; fail_hours raise.
    def _ok_bytes(_h: int) -> Any:
        return raw_bytes

    def _always_raise(_h: int) -> Any:
        def _raise() -> bytes:
            raise DukascopyError("transient")

        return _raise

    run1_responses: dict[str, Any] = {}
    for h in success_hours:
        run1_responses[pending_urls[h]] = raw_bytes
    for h in fail_hours:
        run1_responses[pending_urls[h]] = _always_raise(h)
    stub1 = _StubHttpClient(run1_responses)

    bf.run_bulk_fetch(
        symbols=["EURUSD"],
        year_min=year,
        year_max=year,
        raw_root=raw_root,
        sleep_ms=50,
        max_retries=2,
        http_client=stub1,
    )

    # Verify on-disk state after run 1.
    for h in pre_cached_hours + success_hours:
        path = sym_dir / f"{year:04d}" / "00" / "01" / f"{h:02d}h_ticks.bi5"
        assert path.is_file(), f"hour {h} (cached or run1-success) should exist"
        assert path.read_bytes() == raw_bytes
    for h in fail_hours:
        path = sym_dir / f"{year:04d}" / "00" / "01" / f"{h:02d}h_ticks.bi5"
        assert not path.exists(), f"hour {h} (run1-fail) should be missing"

    # Verify stub call accounting on run 1:
    # * Pre-cached hours: stub NEVER asked (resume-skip).
    # * Success hours: 3 calls, each non-empty.
    # * Fail hours: max_retries=2 attempts each = 4 raises total.
    # * All other 8780 hours of the year: empty response (b"").
    for h in pre_cached_hours:
        assert (
            pending_urls.get(h, _build_url("EURUSD", datetime(year, 1, 1, h, tzinfo=UTC)))
            not in stub1.calls
        )
    for h in success_hours:
        assert pending_urls[h] in stub1.non_empty_calls
    assert sum(stub1.raise_counts.values()) == 2 * 2  # 2 fail_hours * max_retries

    # Run 2: same fail hours now succeed.
    run2_responses = {pending_urls[h]: raw_bytes for h in fail_hours}
    stub2 = _StubHttpClient(run2_responses)

    bf.run_bulk_fetch(
        symbols=["EURUSD"],
        year_min=year,
        year_max=year,
        raw_root=raw_root,
        sleep_ms=50,
        max_retries=1,
        http_client=stub2,
    )

    # Run 2 should ask the stub for exactly the 2 missing hours (other
    # hours are on disk as zero-byte markers from run 1 and get skipped).
    # The stub will also see implicit `b""` lookups for any hour NOT on
    # disk — but every hour of 2020 except (8, 9) was written in run 1
    # (either as cached, non-empty, or empty-marker), so stub2 should
    # receive exactly 2 calls.
    assert len(stub2.calls) == 2
    assert set(stub2.calls) == {pending_urls[h] for h in fail_hours}
    for h in fail_hours:
        path = sym_dir / f"{year:04d}" / "00" / "01" / f"{h:02d}h_ticks.bi5"
        assert path.is_file()
        assert path.read_bytes() == raw_bytes


# --------------------------------------------------------------------------- #
# Help-text & CLI surface
# --------------------------------------------------------------------------- #
def test_cli_help_lists_all_flags() -> None:
    """``--help`` mentions every flag from the spec."""
    bf = _load_module()
    runner = CliRunner()
    result = runner.invoke(bf.app, ["--help"])
    assert result.exit_code == 0
    out = result.output
    for flag in (
        "--symbol",
        "--year-min",
        "--year-max",
        "--raw-root",
        "--sleep-ms",
        "--max-retries",
        "--dry-run",
    ):
        assert flag in out, f"--help missing {flag}"


# --------------------------------------------------------------------------- #
# Integration test (opt-in, network required)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_live_bulk_fetch_one_hour_round_trip(tmp_path: Path) -> None:
    """Fetch one real hour of EURUSD; verify the file lands at the right
    path and round-trips through ``_decompress_bi5`` + :func:`parse_bi5`.

    Tuesday 2024-03-15 10:00 UTC: mid-London session, guaranteed non-empty.
    """
    bf = _load_module()
    raw_root = tmp_path / "raw"
    target_hour = datetime(2024, 3, 15, 10, tzinfo=UTC)
    sym_dir = raw_root / "EURUSD"

    # Pre-populate the entire year 2024 EXCEPT the one target hour, so the
    # live fetcher only hits the real network for that single hour.
    from calendar import monthrange

    placeholder = b"\x00" * 8  # non-zero so the resume scan treats it as cached.
    for month in range(1, 13):
        _, days_in_month = monthrange(2024, month)
        month_zero = month - 1
        for day in range(1, days_in_month + 1):
            for hour in range(24):
                if (
                    month == target_hour.month
                    and day == target_hour.day
                    and hour == target_hour.hour
                ):
                    continue
                p = sym_dir / "2024" / f"{month_zero:02d}" / f"{day:02d}" / f"{hour:02d}h_ticks.bi5"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(placeholder)

    bf.run_bulk_fetch(
        symbols=["EURUSD"],
        year_min=2024,
        year_max=2024,
        raw_root=raw_root,
        sleep_ms=100,
        max_retries=2,
    )

    out_path = sym_dir / "2024" / "02" / "15" / "10h_ticks.bi5"
    assert out_path.is_file()
    assert out_path.stat().st_size > 0, "live hour should be non-empty"

    raw = out_path.read_bytes()
    payload = _decompress_bi5(raw)
    assert len(payload) % 20 == 0
    df = parse_bi5(raw, hour_ts=target_hour, symbol="EURUSD")
    assert df.height > 50
    assert (df["bid"] < df["ask"]).all()
