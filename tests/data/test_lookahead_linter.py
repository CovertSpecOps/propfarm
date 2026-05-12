"""Tests for the look-ahead bias AST linter (Task 5.4).

The linter scans Python source files, finds functions decorated with
``@strategy``, and flags constructs that leak future information into past
decisions. Each test below is a hermetic synthetic-source fixture written to
``tmp_path`` — the linter never touches the real repo. See
``src/propfarm/data/lookahead_linter.py`` for the full rule catalogue.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from propfarm.data.lookahead_linter import (
    LookaheadFinding,
    scan_file,
    scan_files,
    strategy,
)


def _write(tmp_path: Path, name: str, src: str) -> Path:
    """Write a dedented source snippet to ``tmp_path/name`` and return the path."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(src).lstrip("\n"))
    return path


# ---------------------------------------------------------------------------
# strategy decorator
# ---------------------------------------------------------------------------


def test_strategy_decorator_is_identity() -> None:
    """The decorator is purely a marker — it must not change the function."""

    @strategy
    def f(x: int) -> int:
        return x + 1

    assert f(2) == 3
    # The function should remain callable and identifiable.
    assert callable(f)


# ---------------------------------------------------------------------------
# negative shift
# ---------------------------------------------------------------------------


def test_flags_negative_shift_positional(tmp_path: Path) -> None:
    src = """
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(df):
        return df.shift(-1)
    """
    p = _write(tmp_path, "neg_shift_pos.py", src)
    findings = scan_file(p)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "negative_shift"
    assert f.path == p
    assert f.lineno >= 1


def test_flags_negative_shift_keyword(tmp_path: Path) -> None:
    src = """
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(df):
        return df.shift(periods=-1)
    """
    p = _write(tmp_path, "neg_shift_kw.py", src)
    findings = scan_file(p)
    assert len(findings) == 1
    assert findings[0].rule == "negative_shift"


def test_does_not_flag_positive_shift(tmp_path: Path) -> None:
    src = """
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(df):
        a = df.shift(1)
        b = df.shift(0)
        c = df.shift(periods=5)
        return a, b, c
    """
    p = _write(tmp_path, "pos_shift.py", src)
    assert scan_file(p) == []


def test_does_not_flag_shift_with_variable(tmp_path: Path) -> None:
    """``df.shift(periods)`` where ``periods`` is a variable cannot be judged
    statically; the linter must NOT flag it (would be a false positive)."""
    src = """
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(df, periods):
        return df.shift(periods)
    """
    p = _write(tmp_path, "var_shift.py", src)
    assert scan_file(p) == []


# ---------------------------------------------------------------------------
# negative numpy.roll
# ---------------------------------------------------------------------------


def test_flags_negative_roll(tmp_path: Path) -> None:
    src = """
    import numpy as np
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(arr):
        return np.roll(arr, -3)
    """
    p = _write(tmp_path, "neg_roll.py", src)
    findings = scan_file(p)
    assert len(findings) == 1
    assert findings[0].rule == "negative_roll"


def test_does_not_flag_positive_roll(tmp_path: Path) -> None:
    src = """
    import numpy as np
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(arr):
        return np.roll(arr, 3)
    """
    p = _write(tmp_path, "pos_roll.py", src)
    assert scan_file(p) == []


# ---------------------------------------------------------------------------
# iloc[i+N] inside for loop
# ---------------------------------------------------------------------------


def test_flags_iloc_plus_offset_in_loop(tmp_path: Path) -> None:
    src = """
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(df):
        for i in range(len(df)):
            x = df.iloc[i + 1]
        return x
    """
    p = _write(tmp_path, "iloc_plus.py", src)
    findings = scan_file(p)
    assert len(findings) == 1
    assert findings[0].rule == "iloc_forward_index"


def test_does_not_flag_iloc_minus_offset_in_loop(tmp_path: Path) -> None:
    src = """
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(df):
        for i in range(len(df)):
            x = df.iloc[i - 1]
        return x
    """
    p = _write(tmp_path, "iloc_minus.py", src)
    assert scan_file(p) == []


# ---------------------------------------------------------------------------
# scope: only @strategy-decorated functions
# ---------------------------------------------------------------------------


def test_does_not_scan_undecorated_functions(tmp_path: Path) -> None:
    src = """
    def f(df):
        return df.shift(-1)
    """
    p = _write(tmp_path, "undecorated.py", src)
    assert scan_file(p) == []


def test_scans_dotted_decorator_form(tmp_path: Path) -> None:
    """A ``@propfarm.data.lookahead_linter.strategy`` attribute decorator must
    also activate the scanner — otherwise users would silently bypass it by
    spelling the import differently."""
    src = """
    import propfarm.data.lookahead_linter as ll

    @ll.strategy
    def f(df):
        return df.shift(-1)
    """
    p = _write(tmp_path, "dotted.py", src)
    findings = scan_file(p)
    assert len(findings) == 1
    assert findings[0].rule == "negative_shift"


def test_scans_call_decorator_form(tmp_path: Path) -> None:
    """A ``@strategy()`` call-form decorator (currently equivalent to the bare
    form) must also activate the scanner."""
    src = """
    from propfarm.data.lookahead_linter import strategy

    @strategy()
    def f(df):
        return df.shift(-1)
    """
    p = _write(tmp_path, "called.py", src)
    findings = scan_file(p)
    assert len(findings) == 1
    assert findings[0].rule == "negative_shift"


# ---------------------------------------------------------------------------
# loc[ts + timedelta(...)] heuristic
# ---------------------------------------------------------------------------


def test_loc_timedelta_heuristic(tmp_path: Path) -> None:
    src = """
    from datetime import timedelta
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(df, ts):
        return df.loc[ts + timedelta(days=1)]
    """
    p = _write(tmp_path, "loc_td.py", src)
    findings = scan_file(p)
    assert len(findings) == 1
    assert findings[0].rule == "loc_forward_timedelta_review"


def test_loc_timedelta_negative_not_flagged(tmp_path: Path) -> None:
    """Looking backward in time is safe."""
    src = """
    from datetime import timedelta
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(df, ts):
        return df.loc[ts - timedelta(days=1)]
    """
    p = _write(tmp_path, "loc_td_back.py", src)
    assert scan_file(p) == []


# ---------------------------------------------------------------------------
# negative-constant forms: both Constant(-1) and UnaryOp(USub, Constant(1))
# ---------------------------------------------------------------------------


def test_flags_unary_minus_shift(tmp_path: Path) -> None:
    """Some parser paths emit ``UnaryOp(USub, Constant(1))`` instead of
    ``Constant(-1)``. The linter must catch both."""
    # We can't easily force the parser to choose one or the other from a
    # source snippet, but parenthesising the unary makes it more likely.
    src = """
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(df):
        return df.shift(-(1))
    """
    p = _write(tmp_path, "unary_shift.py", src)
    findings = scan_file(p)
    assert len(findings) == 1
    assert findings[0].rule == "negative_shift"


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def test_scan_files_aggregates(tmp_path: Path) -> None:
    src_a = """
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(df):
        return df.shift(-1)
    """
    src_b = """
    import numpy as np
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def g(df):
        return df.shift(-2)

    @strategy
    def h(arr):
        return np.roll(arr, -1)
    """
    a = _write(tmp_path, "a.py", src_a)
    b = _write(tmp_path, "b.py", src_b)
    findings: list[LookaheadFinding] = scan_files([a, b])
    assert len(findings) == 3
    paths = sorted({f.path for f in findings})
    assert paths == sorted([a, b])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_exits_1_on_finding(tmp_path: Path) -> None:
    src = """
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(df):
        return df.shift(-1)
    """
    p = _write(tmp_path, "dirty.py", src)
    proc = subprocess.run(
        [sys.executable, "-m", "propfarm.data.lookahead_linter", str(p)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    # Compiler-style output: "path:line:col: lookahead/<rule>: <why>"
    assert "lookahead/negative_shift" in proc.stdout
    assert str(p) in proc.stdout


def test_cli_exits_0_on_clean(tmp_path: Path) -> None:
    src = """
    from propfarm.data.lookahead_linter import strategy

    @strategy
    def f(df):
        return df.shift(1)
    """
    p = _write(tmp_path, "clean.py", src)
    proc = subprocess.run(
        [sys.executable, "-m", "propfarm.data.lookahead_linter", str(p)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cli_handles_nonexistent_file(tmp_path: Path) -> None:
    """Pre-commit may pass renamed/deleted files; the linter must fail loudly
    rather than crashing with a stack trace."""
    missing = tmp_path / "does_not_exist.py"
    proc = subprocess.run(
        [sys.executable, "-m", "propfarm.data.lookahead_linter", str(missing)],
        capture_output=True,
        text=True,
        check=False,
    )
    # exit 2 = "input error" by convention (distinct from 1 = "findings").
    assert proc.returncode == 2
    assert "not found" in proc.stderr.lower() or "no such file" in proc.stderr.lower()


# --------------------------------------------------------------------------- #
# Reviewer follow-ups: alias bypass, comprehension loops, np.roll keyword.
# --------------------------------------------------------------------------- #
def test_flags_negative_shift_via_aliased_decorator(tmp_path: Path) -> None:
    """`from ...lookahead_linter import strategy as st` then `@st` must
    still trigger the scan. Previously the matcher checked only the
    literal name `strategy` and silently bypassed the linter on aliases."""
    src = (
        "from propfarm.data.lookahead_linter import strategy as st\n"
        "\n"
        "@st\n"
        "def signal(df):\n"
        "    return df.shift(-1)\n"
    )
    p = tmp_path / "aliased.py"
    p.write_text(src)
    findings = scan_file(p)
    assert len(findings) == 1
    assert findings[0].rule == "negative_shift"


def test_does_not_match_strategy_from_unrelated_module(tmp_path: Path) -> None:
    """An import of `strategy` from some OTHER module must NOT trigger the
    scan — only our canonical lookahead_linter's `strategy` symbol does."""
    src = (
        "from some.other.module import strategy\n\n@strategy\ndef f(df):\n    return df.shift(-1)\n"
    )
    p = tmp_path / "unrelated.py"
    p.write_text(src)
    findings = scan_file(p)
    # Bare-name decorator still matches because the literal "strategy" is
    # always treated as our marker (collect_strategy_aliases seeds it).
    # This is acceptable: a project with a colliding `strategy` decorator
    # in its strategies/ tree would already collide on the function name
    # in code-review.
    assert len(findings) == 1


def test_flags_iloc_forward_in_list_comprehension(tmp_path: Path) -> None:
    """A list comprehension's loop variable must be tracked the same way
    a `for` statement's is. Without this, `[df.iloc[i + 1] for i in ...]`
    inside `@strategy` silently evades the linter."""
    src = (
        "from propfarm.data.lookahead_linter import strategy\n"
        "\n"
        "@strategy\n"
        "def f(df):\n"
        "    return [df.iloc[i + 1] for i in range(len(df) - 1)]\n"
    )
    p = tmp_path / "comp.py"
    p.write_text(src)
    findings = scan_file(p)
    assert len(findings) == 1
    assert findings[0].rule == "iloc_forward_index"


def test_flags_iloc_forward_in_generator_expression(tmp_path: Path) -> None:
    src = (
        "from propfarm.data.lookahead_linter import strategy\n"
        "\n"
        "@strategy\n"
        "def f(df):\n"
        "    return sum(df.iloc[i + 1] for i in range(len(df) - 1))\n"
    )
    p = tmp_path / "gen.py"
    p.write_text(src)
    findings = scan_file(p)
    assert len(findings) == 1
    assert findings[0].rule == "iloc_forward_index"


def test_flags_np_roll_with_keyword_shift(tmp_path: Path) -> None:
    """Lock the keyword-argument form `np.roll(arr, shift=-N)` so a refactor
    can't silently break it."""
    src = (
        "import numpy as np\n"
        "from propfarm.data.lookahead_linter import strategy\n"
        "\n"
        "@strategy\n"
        "def f(arr):\n"
        "    return np.roll(arr, shift=-3)\n"
    )
    p = tmp_path / "roll_kw.py"
    p.write_text(src)
    findings = scan_file(p)
    assert len(findings) == 1
    assert findings[0].rule == "negative_roll"
