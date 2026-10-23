"""Demo policy — pure class-based wrapper around demo_mode (v231+).

Why this exists:
  `demo_mode.py` is a 280-line module that mixes pure transformation
  functions (sanitize_number, trim_history) with global state
  (IS_DEMO, DEMO_DECIMALS, etc.).  This makes the transforms hard to
  test in isolation, and ties the policy to the runtime env vars.

`demo_policy.py` is a thin refactor that:
  1. Defines a `DemoPolicy` dataclass with the knobs as fields
  2. Provides a `sanitize` / `trim_history` instance method on it
  3. Adds `DemoPolicy.disabled()` and `DemoPolicy.from_env()` factories
  4. Keeps full backward compatibility: `demo_mode` keeps its module-level
     functions unchanged, and `DemoPolicy.from_env()` just wraps them.

This is a refactor, not a behavior change.  All `demo_mode.sanitize_number`
callers continue to work; new code can use `policy.sanitize(value)`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_SKIP_KEYS = frozenset({
    "id", "uuid", "ts", "timestamp", "created_at", "event_id",
    "title", "name", "symbol", "ticker", "track",
})


@dataclass(frozen=True)
class DemoPolicy:
    """Immutable policy describing what demo mode does to a response.

    Fields:
        enabled:        if False, sanitize is a no-op
        decimals:       number of decimal places to keep on floats
        history_days:   if > 0, trim rows older than N days
        skip_keys:      dict keys whose values are never sanitized
                        (ids, timestamps, names — anything structural)
    """
    enabled: bool = False
    decimals: int = 2
    history_days: int = 0
    skip_keys: frozenset = field(default_factory=lambda: DEFAULT_SKIP_KEYS)

    # ────────── factories ──────────

    @classmethod
    def disabled(cls) -> "DemoPolicy":
        return cls(enabled=False)

    @classmethod
    def from_env(cls) -> "DemoPolicy":
        """Build a policy from TRIDENT_MODE-style env vars (lazy import to avoid cycles)."""
        try:
            import demo_mode
            return cls(
                enabled=demo_mode.IS_DEMO,
                decimals=demo_mode.DEMO_DECIMALS,
                history_days=demo_mode.DEMO_HISTORY_DAYS,
                skip_keys=frozenset(demo_mode._SKIP_KEYS)
                          if hasattr(demo_mode, "_SKIP_KEYS")
                          else DEFAULT_SKIP_KEYS,
            )
        except Exception:
            return cls.disabled()

    # ────────── transform: numbers ──────────

    def sanitize(self, x: Any) -> Any:
        """Recursive sanitize: floats → round, dicts/list → recurse.

        Disabled policy: returns x unchanged.  Cheap O(1) check.
        """
        if not self.enabled:
            return x
        return self._sanitize(x)

    def _sanitize(self, x: Any) -> Any:
        if isinstance(x, bool):
            return x
        if isinstance(x, float):
            if math.isnan(x) or math.isinf(x):
                return x
            return round(x, self.decimals)
        if isinstance(x, dict):
            return {
                k: (x[k] if k in self.skip_keys else self._sanitize(v))
                for k, v in x.items()
            }
        if isinstance(x, (list, tuple)):
            cleaned = [self._sanitize(v) for v in x]
            return type(x)(cleaned) if isinstance(x, tuple) else cleaned
        return x

    # ────────── transform: history ──────────

    def trim_history(self, rows: List[dict],
                     ts_field: str = "ts") -> List[dict]:
        """Trim rows older than history_days.  No-op if disabled or days<=0."""
        if not self.enabled or self.history_days <= 0:
            return rows
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.history_days)
        kept: List[dict] = []
        for r in rows:
            ts = r.get(ts_field)
            if ts is None:
                kept.append(r)
                continue
            try:
                if isinstance(ts, (int, float)):
                    dt = datetime.fromtimestamp(
                        ts / 1000.0 if ts > 1e12 else float(ts),
                        tz=timezone.utc,
                    )
                else:
                    ts_s = str(ts).replace("Z", "+00:00")
                    dt = datetime.fromisoformat(ts_s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                if dt >= cutoff:
                    kept.append(r)
            except Exception:
                kept.append(r)
        return kept

    # ────────── introspection ──────────

    def describe(self) -> Dict[str, Any]:
        """For /api/health/security and config dumps."""
        return {
            "enabled":       self.enabled,
            "decimals":      self.decimals,
            "history_days":  self.history_days,
            "skip_keys_n":   len(self.skip_keys),
        }


# ────────── module-level convenience ──────────

def current_policy() -> DemoPolicy:
    """Returns the policy reflecting the live env.  Cheap (1 import)."""
    return DemoPolicy.from_env()


def sanitize(x: Any) -> Any:
    """Module-level shortcut for `current_policy().sanitize(x)`.

    Convenient for one-off calls; tests should build a DemoPolicy
    directly to avoid env coupling.
    """
    return current_policy().sanitize(x)


def trim_history(rows: List[dict], ts_field: str = "ts") -> List[dict]:
    """Module-level shortcut for `current_policy().trim_history(rows, ts_field)`."""
    return current_policy().trim_history(rows, ts_field=ts_field)
