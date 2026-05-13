"""Cross-firm predicate registry (Task 11.2 — W4b).

This module is the **canonical home** for the cross-firm predicate registry
that the loader, kill switch, daily auto-report, and Phase-4 funded-deploy
certification iterate. Moved out of :mod:`propfarm.rules.ftmo` during W4b
because importing fundednext/fundingpips predicates **into** ftmo.py would
have been a layering inversion: ftmo.py should not know about the other
firms, but the registry must reference all firms.

Why a dedicated module (and not the package ``__init__.py``)?
-----------------------------------------------------------
Three reasons:

1. **Cycle hygiene.** Putting per-firm imports in ``rules/__init__.py``
   would force every consumer of any predicate to load every firm's
   module — fine for now, but couples the loader cost to the surface
   size, and creates a lazy-import smell that resists static analysis.
2. **Single source of truth.** The W3 cost-table registry lives in its
   own module (``propfarm.sim.commission.ALL_TABLES``), not in the
   package ``__init__``. Symmetric structure across W3 / W4.
3. **Loader ergonomics.** A consumer that only wants the registry imports
   ``from propfarm.rules.registry import ALL_FIRM_PREDICATES`` — one
   short path, no package-level side effects.

The package ``__init__.py`` re-exports ``ALL_FIRM_PREDICATES`` and
``ALL_MODEL_PREDICATES`` for ergonomic access; the registry module is
the canonical definition site.
"""

from __future__ import annotations

from typing import Final

from propfarm.rules.ftmo import FTMO_PREDICATES
from propfarm.rules.fundednext import (
    FUNDEDNEXT_PREDICATES,
    FUNDEDNEXT_PREDICATES_BY_MODEL,
)
from propfarm.rules.fundingpips import (
    FUNDINGPIPS_PREDICATES,
    FUNDINGPIPS_PREDICATES_BY_MODEL,
)
from propfarm.rules.predicates import Predicate

__all__ = [
    "ALL_FIRM_PREDICATES",
    "ALL_MODEL_PREDICATES",
]


#: Firm-level predicate registry. Each firm's tuple is the predicate set
#: selected by the firm's **default model**:
#:
#: * ``ftmo``: the single FTMO model (one-step Challenge + two-step combined).
#: * ``fundednext``: Stellar 2-Step (project default per the brief).
#: * ``fundingpips``: 2-Step (matches FTMO's 5% / 10% shape).
#:
#: Loader pattern mirrors :data:`propfarm.sim.commission.ALL_TABLES`: a
#: consumer iterates the tuple/dict and reads ``.confidence`` per element.
ALL_FIRM_PREDICATES: Final[dict[str, tuple[Predicate, ...]]] = {
    "ftmo": FTMO_PREDICATES,
    "fundednext": FUNDEDNEXT_PREDICATES,
    "fundingpips": FUNDINGPIPS_PREDICATES,
}


#: Per-(firm, model) predicate registry. The Phase-4 funded-deploy
#: certification check iterates this when a single firm's account is being
#: certified for a specific model — the certification gate must know
#: exactly which predicate set the live account will run against.
#:
#: For FTMO the model key is ``"default"`` because FTMO ships a single
#: rule set (one-step / two-step share predicates with different
#: ``threshold_fraction`` instances, but they all belong to the same
#: ``FTMO_PREDICATES`` tuple — see ``src/propfarm/rules/ftmo.py``).
ALL_MODEL_PREDICATES: Final[dict[tuple[str, str], tuple[Predicate, ...]]] = {
    ("ftmo", "default"): FTMO_PREDICATES,
    **{("fundednext", model): preds for model, preds in FUNDEDNEXT_PREDICATES_BY_MODEL.items()},
    **{("fundingpips", model): preds for model, preds in FUNDINGPIPS_PREDICATES_BY_MODEL.items()},
}
