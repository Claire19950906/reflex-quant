"""Meta-loop adaptive stopping rule (v231+).

Replaces the hard-coded `max_level=2` with an information-gain-based rule.

Idea:
  At each level L, we measure the fraction of signals where level L's
  finding_kind differs from level (L-1)'s. This is the "new information
  rate". If it falls below `meta_stopping_eps`, deeper levels add no
  signal — stop.

  This is more principled than a fixed max_level:
    - On new/chaotic data: keeps digging (up to `meta_max_level`)
    - On mature/stable data: stops at level 1 or 2, saving compute
    - On data where the loop oscillates: detects it (kind flips each
      level → high entropy → also stop)

This module is importable independently of meta_reflection_loop; the
loop is a thin caller.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional, Tuple


def _safe_div(a: int, b: int) -> float:
    return a / b if b > 0 else 0.0


def new_info_rate(current_kinds: List[str], prior_kinds: List[str]) -> float:
    """Fraction of items where current kind differs from prior kind.

    Args:
        current_kinds: list of finding_kind at level L (in some signal order)
        prior_kinds:   list of finding_kind at level L-1 (same signal order)

    Returns:
        rate in [0, 1]. 0 = current level never disagrees with prior;
                         1 = always disagrees.

    Notes:
        If lengths differ, we zip by position up to min(len_a, len_b).
    """
    if not current_kinds or not prior_kinds:
        return 0.0
    n = min(len(current_kinds), len(prior_kinds))
    if n == 0:
        return 0.0
    diffs = sum(1 for c, p in zip(current_kinds, prior_kinds) if c != p)
    return diffs / n


def level_entropy(kinds: List[str]) -> float:
    """Shannon entropy of a level's finding_kind distribution (normalized to [0, 1]).

    Used to detect oscillation: if entropy is high but new_info_rate is
    also high, the loop is flipping kinds (suggests data is noisy → stop).
    """
    if not kinds:
        return 0.0
    counts = Counter(kinds)
    total = sum(counts.values())
    import math
    H = 0.0
    for c in counts.values():
        p = c / total
        if p > 0:
            H -= p * math.log2(p)
    # Normalize by max entropy (uniform over k distinct kinds)
    k = len(counts)
    H_max = math.log2(k) if k > 1 else 1.0
    return H / H_max if H_max > 0 else 0.0


def should_continue(prior_kinds: List[str],
                    current_kinds: List[str],
                    eps: float = 0.05,
                    max_entropy: float = 0.85) -> bool:
    """Decide whether to dig one more level.

    Returns True (continue) only if BOTH:
      - new_info_rate > eps  (current level disagrees with prior at least eps fraction)
      - current entropy < max_entropy  (current level isn't chaotic)

    Args:
        prior_kinds:    level L-1 finding_kinds (per signal)
        current_kinds:  level L finding_kinds (per signal, same order)
        eps:            min disagreement rate to consider level L worth having
        max_entropy:    if level L's kind entropy exceeds this, level is chaotic — stop
    """
    if not current_kinds:
        return False
    nir = new_info_rate(current_kinds, prior_kinds)
    ent = level_entropy(current_kinds)
    if nir <= eps:
        return False   # no new info
    if ent > max_entropy:
        return False   # chaotic
    return True


def plan_levels(prior_kinds: List[str],
                level_runner: Any,
                max_level: int = 4,
                eps: float = 0.05,
                max_entropy: float = 0.85) -> List[List[Tuple[str, str]]]:
    """Run the meta-loop adaptively, stopping when should_continue says so.

    Args:
        prior_kinds:    list of "kinds" for level 0 (typically the parent reflections
                        are unreflected; pass empty list to start at level 1)
        level_runner:   callable(level_int, prev_kinds) -> List[Tuple[signal_key, kind)]
                        where signal_key is any hashable; kind is the finding_kind string
        max_level:      hard upper bound on loop depth
        eps:            stopping rule: min new_info_rate
        max_entropy:    stopping rule: max allowed level entropy

    Returns:
        list of levels, each is a list of (signal_key, kind) tuples for that level
    """
    levels: List[List[Tuple[str, str]]] = []
    prev = list(prior_kinds)
    for L in range(1, max_level + 1):
        pairs: List[Tuple[str, str]] = list(level_runner(L, prev))
        if not pairs:
            break   # nothing to reflect on
        kinds = [k for _, k in pairs]
        levels.append(pairs)
        # Stop conditions:
        #   1. Hard upper bound
        #   2. Empty prior: always run L=2 once to give the rule data
        #   3. Otherwise: apply the new_info_rate + entropy rule
        if L >= max_level:
            break
        if not prev:
            # L=1 → L=2: always continue so the rule has prior data
            prev = kinds
            continue
        if not should_continue(prev, kinds, eps=eps, max_entropy=max_entropy):
            break
        prev = kinds
    return levels
