"""Look-ahead bias linter (Task 5.4) — AST walker for ``@strategy`` functions.

Look-ahead bias is the #1 cause of false-positive backtest results. A strategy
that does ``df["signal"] = df["close"].shift(-1) > df["close"]`` uses
tomorrow's price to "predict" today's close — the backtest looks great, the
live deployment is random.

This module scans Python source files at commit time and flags a curated set
of known look-ahead patterns inside any function wearing the ``@strategy``
marker decorator. The scan is purely static — it never executes user code —
which means:

* The linter cannot follow data through variables: ``df.shift(periods)`` where
  ``periods`` is a runtime value is **deliberately not flagged**. The
  alternative is a false-positive avalanche. Use a runtime assertion in your
  strategy if ``periods`` must be non-negative.
* Indirection through function calls (e.g. ``df.iloc[some_helper(i)]``)
  evades detection. Document any such helper and audit it manually.

Rules
-----
The linter currently knows about four families of look-ahead:

================================  ============================================
Rule                              Pattern
================================  ============================================
``negative_shift``                ``df.shift(-N)`` / ``df.shift(periods=-N)``
``negative_roll``                 ``np.roll(arr, -N)``
``iloc_forward_index``            ``df.iloc[i + N]`` inside a ``for`` loop
``loc_forward_timedelta_review``  ``df.loc[ts + timedelta(...)]`` (heuristic)
================================  ============================================

The ``loc_forward_timedelta_review`` rule is a heuristic flag for manual
review — a positive ``timedelta`` added to a timestamp inside ``.loc[]``
is *usually* look-ahead but can be legitimate (e.g. labelling a horizon).
It is intentionally lower-confidence than the other three.

CLI
---
::

    python -m propfarm.data.lookahead_linter path/to/strategy.py [...]

Exit codes:

* ``0`` — no findings.
* ``1`` — one or more findings (prints them in ``path:line:col: ...`` form).
* ``2`` — input error (file does not exist, syntax error, etc.).
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import overload

__all__ = [
    "LookaheadFinding",
    "scan_file",
    "scan_files",
    "strategy",
]


# ---------------------------------------------------------------------------
# Marker decorator
# ---------------------------------------------------------------------------


@overload
def strategy[F: Callable[..., object]](func: F) -> F: ...


@overload
def strategy[F: Callable[..., object]](func: None = ...) -> Callable[[F], F]: ...


def strategy[F: Callable[..., object]](func: F | None = None) -> F | Callable[[F], F]:
    """Marker decorator. Tags a function as a strategy entrypoint.

    Identity at runtime — the decorator returns the wrapped function unchanged.
    Its only purpose is to be visible to :func:`scan_file`, which scans
    functions wearing this marker. Recognized spellings: bare ``@strategy``,
    call form ``@strategy()``, dotted ``@module.strategy``, and ``as``-aliased
    imports from our canonical module (``from propfarm.data.lookahead_linter
    import strategy as st`` → ``@st``). An imported ``strategy`` from a
    different module is NOT recognized as a marker.

    Supports both bare and call-form usage::

        @strategy
        def s(df): ...

        @strategy()
        def s(df): ...
    """
    if func is None:
        # Called as ``@strategy()`` — return the decorator itself.
        def _wrap(inner: F) -> F:
            return inner

        return _wrap
    # Called as ``@strategy`` — return the function unchanged.
    return func


# ---------------------------------------------------------------------------
# Finding record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LookaheadFinding:
    """One look-ahead pattern detected in a file.

    Attributes
    ----------
    path
        Absolute path to the source file containing the finding.
    lineno
        1-based line number of the offending AST node.
    col
        0-based column offset (matches ``ast.AST.col_offset``).
    rule
        Short identifier — see the module docstring's rule table.
    snippet
        A short string excerpt of the offending source (e.g.
        ``"df.shift(-1)"``) for display in pre-commit output.
    why
        Human-readable explanation of why the pattern is dangerous.
    """

    path: Path
    lineno: int
    col: int
    rule: str
    snippet: str
    why: str

    def format(self) -> str:
        """Compiler-style ``path:line:col: lookahead/<rule>: <why>`` line."""
        return f"{self.path}:{self.lineno}:{self.col}: lookahead/{self.rule}: {self.why}"


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _is_negative_int_constant(node: ast.AST) -> bool:
    """True if ``node`` represents a negative integer literal.

    Handles both AST shapes:

    * ``Constant(value=-1)`` — what some parser paths emit for ``-1``.
    * ``UnaryOp(op=USub(), operand=Constant(value=1))`` — what the parser
      typically emits for ``-1`` and reliably for ``-(1)`` / ``- N``.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value < 0
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        operand = node.operand
        if isinstance(operand, ast.Constant) and isinstance(operand.value, int):
            return operand.value > 0
    return False


def _is_nonnegative_int_constant(node: ast.AST) -> bool:
    """True if ``node`` is a non-negative integer literal (``0``, ``1``, ...)."""
    return isinstance(node, ast.Constant) and isinstance(node.value, int) and node.value >= 0


def _attr_chain(node: ast.AST) -> list[str]:
    """Return the dotted attribute chain rooted at ``node``.

    Examples:
        * ``Name('x')`` → ``["x"]``
        * ``Attribute(Name('a'), 'b')`` → ``["a", "b"]``
        * ``Attribute(Attribute(Name('a'), 'b'), 'c')`` → ``["a", "b", "c"]``
        * other shapes → ``[]``
    """
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    else:
        return []
    return list(reversed(parts))


def _collect_strategy_aliases(tree: ast.AST) -> set[str]:
    """Return every local name that resolves to our ``@strategy`` marker.

    Always includes the literal ``"strategy"``. Also resolves ``from
    propfarm.data.lookahead_linter import strategy as st`` (the ``as``
    form) so that ``@st``-decorated functions are not silently skipped
    by the scanner. Without this resolution, an author could bypass the
    entire linter just by renaming the import.
    """
    aliases: set[str] = {"strategy"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # Only resolve imports whose module ends with ``lookahead_linter``;
            # this avoids a separate project's "strategy" symbol falsely
            # claiming our protection.
            mod = node.module or ""
            if not (mod == "lookahead_linter" or mod.endswith(".lookahead_linter")):
                continue
            for alias in node.names:
                if alias.name == "strategy":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _decorator_is_strategy(dec: ast.expr, aliases: set[str]) -> bool:
    """True if a decorator AST node names our ``@strategy`` marker.

    Accepts: bare ``@strategy``, dotted ``@a.b.strategy``, called form
    ``@strategy()`` or ``@a.b.strategy()``, and ``as``-aliased imports
    (e.g. ``from ...lookahead_linter import strategy as st`` → ``@st``).
    """
    target: ast.expr = dec
    if isinstance(dec, ast.Call):
        target = dec.func
    if isinstance(target, ast.Name):
        return target.id in aliases
    if isinstance(target, ast.Attribute):
        # Dotted form (e.g. @propfarm.data.lookahead_linter.strategy) — the
        # final attribute name is what we match. An author could still
        # bypass via `import propfarm.data.lookahead_linter as p; @p.strategy`
        # which works because the attr is still "strategy".
        return target.attr == "strategy"
    return False


# ---------------------------------------------------------------------------
# Visitor
# ---------------------------------------------------------------------------


class _LookaheadVisitor(ast.NodeVisitor):
    """Walk the body of one ``@strategy``-decorated function and collect findings."""

    def __init__(self, path: Path, source_lines: list[str]) -> None:
        self.path = path
        self.source_lines = source_lines
        self.findings: list[LookaheadFinding] = []
        # Names bound as ``for`` loop targets currently in scope. We use a list
        # of sets to allow shadowing in nested loops.
        self._loop_index_stack: list[set[str]] = []

    # -- loop tracking ------------------------------------------------------

    def visit_For(self, node: ast.For) -> None:
        indices: set[str] = set()
        if isinstance(node.target, ast.Name):
            indices.add(node.target.id)
        elif isinstance(node.target, ast.Tuple):
            for elt in node.target.elts:
                if isinstance(elt, ast.Name):
                    indices.add(elt.id)
        self._loop_index_stack.append(indices)
        try:
            self.generic_visit(node)
        finally:
            self._loop_index_stack.pop()

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        # ``async for`` shares the look-ahead semantics of ``for``: any
        # ``df.iloc[loop_var + N]`` inside the body reads a future row.
        indices: set[str] = set()
        if isinstance(node.target, ast.Name):
            indices.add(node.target.id)
        elif isinstance(node.target, ast.Tuple):
            for elt in node.target.elts:
                if isinstance(elt, ast.Name):
                    indices.add(elt.id)
        self._loop_index_stack.append(indices)
        try:
            self.generic_visit(node)
        finally:
            self._loop_index_stack.pop()

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    ) -> None:
        """Push every ``for`` clause's target onto the loop-index stack.

        ``[df.iloc[i + 1] for i in range(n)]`` exposes ``i`` as a loop
        index just like a ``for`` statement; without this hook the
        ``iloc_forward_index`` rule silently misses it.
        """
        indices: set[str] = set()
        for gen in node.generators:
            if isinstance(gen.target, ast.Name):
                indices.add(gen.target.id)
            elif isinstance(gen.target, ast.Tuple):
                for elt in gen.target.elts:
                    if isinstance(elt, ast.Name):
                        indices.add(elt.id)
        self._loop_index_stack.append(indices)
        try:
            self.generic_visit(node)
        finally:
            self._loop_index_stack.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node)

    # -- call-based rules ---------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        self._check_shift(node)
        self._check_roll(node)
        self.generic_visit(node)

    def _check_shift(self, node: ast.Call) -> None:
        """Flag ``<anything>.shift(-N)`` / ``.shift(periods=-N)``."""
        if not isinstance(node.func, ast.Attribute):
            return
        if node.func.attr != "shift":
            return
        # Determine the periods argument: first positional, or ``periods=`` kw.
        periods_node: ast.AST | None = None
        if node.args:
            periods_node = node.args[0]
        for kw in node.keywords:
            if kw.arg == "periods":
                periods_node = kw.value
                break
        if periods_node is None:
            return
        if _is_negative_int_constant(periods_node):
            self._emit(
                node,
                rule="negative_shift",
                why=(
                    "`.shift(<negative>)` pulls future rows into the present and is a "
                    "classic look-ahead bias. Use `.shift(positive)` to look back."
                ),
            )

    def _check_roll(self, node: ast.Call) -> None:
        """Flag ``np.roll(arr, -N)`` (positional shift only).

        Recognised forms: bare ``roll(...)``, ``np.roll(...)``, or any
        attribute chain ending in ``.roll`` (e.g. ``numpy.roll``).
        """
        chain = _attr_chain(node.func)
        if not chain or chain[-1] != "roll":
            return
        # ``np.roll(arr, shift, axis=...)`` — shift is positional arg 1, or
        # ``shift=`` keyword.
        shift_node: ast.AST | None = None
        if len(node.args) >= 2:
            shift_node = node.args[1]
        for kw in node.keywords:
            if kw.arg == "shift":
                shift_node = kw.value
                break
        if shift_node is None:
            return
        if _is_negative_int_constant(shift_node):
            self._emit(
                node,
                rule="negative_roll",
                why=(
                    "`np.roll(arr, <negative>)` rotates future values into the past "
                    "position and is a look-ahead bias when applied to a time series."
                ),
            )

    # -- subscript-based rules ---------------------------------------------

    def visit_Subscript(self, node: ast.Subscript) -> None:
        self._check_iloc(node)
        self._check_loc(node)
        self.generic_visit(node)

    def _check_iloc(self, node: ast.Subscript) -> None:
        """Flag ``df.iloc[i + N]`` inside a ``for`` loop when ``i`` is the loop var."""
        if not self._loop_index_stack:
            return
        if not isinstance(node.value, ast.Attribute) or node.value.attr != "iloc":
            return
        # The subscript may be a single expression or a Tuple (multi-dim).
        targets: list[ast.expr] = []
        if isinstance(node.slice, ast.Tuple):
            targets.extend(node.slice.elts)
        else:
            targets.append(node.slice)
        active_indices: set[str] = set().union(*self._loop_index_stack)
        for target in targets:
            if not isinstance(target, ast.BinOp) or not isinstance(target.op, ast.Add):
                continue
            # ``i + N`` where ``i`` is a loop index name and ``N`` is positive.
            left, right = target.left, target.right
            name_node, offset_node = None, None
            if isinstance(left, ast.Name) and left.id in active_indices:
                name_node, offset_node = left, right
            elif isinstance(right, ast.Name) and right.id in active_indices:
                name_node, offset_node = right, left
            if name_node is None or offset_node is None:
                continue
            if _is_nonnegative_int_constant(offset_node) and not (
                isinstance(offset_node, ast.Constant) and offset_node.value == 0
            ):
                self._emit(
                    target,
                    rule="iloc_forward_index",
                    why=(
                        f"`.iloc[{name_node.id}+N]` inside a `for` loop reads a row "
                        "ahead of the current iteration — look-ahead bias."
                    ),
                )

    def _check_loc(self, node: ast.Subscript) -> None:
        """Heuristic: flag ``df.loc[<anything> + timedelta(...)]`` for review."""
        if not isinstance(node.value, ast.Attribute) or node.value.attr != "loc":
            return
        targets: list[ast.expr] = []
        if isinstance(node.slice, ast.Tuple):
            targets.extend(node.slice.elts)
        else:
            targets.append(node.slice)
        for target in targets:
            if not isinstance(target, ast.BinOp) or not isinstance(target.op, ast.Add):
                continue
            # Look for a Call to ``timedelta`` on either side of the ``+``.
            for side in (target.left, target.right):
                if isinstance(side, ast.Call):
                    chain = _attr_chain(side.func)
                    if chain and chain[-1] == "timedelta":
                        self._emit(
                            target,
                            rule="loc_forward_timedelta_review",
                            why=(
                                "`.loc[ts + timedelta(...)]` reads a future timestamp. "
                                "If `timedelta` is positive this is look-ahead; review "
                                "and either invert the sign or annotate as intentional."
                            ),
                        )
                        return

    # -- emit ---------------------------------------------------------------

    def _emit(self, node: ast.AST, *, rule: str, why: str) -> None:
        lineno = getattr(node, "lineno", 0)
        col = getattr(node, "col_offset", 0)
        snippet = ""
        if 1 <= lineno <= len(self.source_lines):
            snippet = self.source_lines[lineno - 1].strip()
        self.findings.append(
            LookaheadFinding(
                path=self.path,
                lineno=lineno,
                col=col,
                rule=rule,
                snippet=snippet,
                why=why,
            )
        )


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


def _find_strategy_functions(
    tree: ast.AST,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return every function (sync or async) in ``tree`` decorated with ``@strategy``."""
    aliases = _collect_strategy_aliases(tree)
    out: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            _decorator_is_strategy(d, aliases) for d in node.decorator_list
        ):
            out.append(node)
    return out


def scan_file(path: Path) -> list[LookaheadFinding]:
    """Parse ``path`` and return every look-ahead finding in its ``@strategy`` functions.

    Non-strategy code is **not** scanned — that's intentional. The linter is a
    strategy-author safety net, not a global static checker.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist. Callers (CLI, pre-commit) should catch.
    SyntaxError
        If the file does not parse. Surfaced as exit code 2 by the CLI.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    source_lines = source.splitlines()
    findings: list[LookaheadFinding] = []
    for fn in _find_strategy_functions(tree):
        visitor = _LookaheadVisitor(path=path, source_lines=source_lines)
        # Walk only the body of the function, not its decorators / signature —
        # otherwise default arguments would be scanned for loop-context that
        # doesn't exist there.
        for stmt in fn.body:
            visitor.visit(stmt)
        findings.extend(visitor.findings)
    return findings


def scan_files(paths: Iterable[Path]) -> list[LookaheadFinding]:
    """Run :func:`scan_file` over many paths, returning the concatenated findings."""
    out: list[LookaheadFinding] = []
    for p in paths:
        out.extend(scan_file(p))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str]) -> int:
    """Entry point. Returns the process exit code."""
    if not argv:
        # Nothing to scan — treat as clean (pre-commit can call us with no
        # paths when the file filter excludes everything).
        return 0

    findings: list[LookaheadFinding] = []
    for raw in argv:
        path = Path(raw)
        if not path.exists():
            print(f"lookahead-linter: file not found: {path}", file=sys.stderr)
            return 2
        try:
            findings.extend(scan_file(path))
        except SyntaxError as exc:
            print(f"lookahead-linter: syntax error in {path}: {exc}", file=sys.stderr)
            return 2

    for f in findings:
        print(f.format())
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
