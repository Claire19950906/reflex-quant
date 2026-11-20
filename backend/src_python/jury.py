"""Three-track jury - heterogeneous review (v231+).

Kills the "LLM-as-judge circularity" weakness in the reflection loop.

The biggest problem with the current reflection system is that the
same model that produced a decision is also asked to evaluate it.
That's not a real critic; it's the model talking to itself.

This module provides a `Jury` that takes verdicts from N independent
"tracks" (different framings, different models, different evidence)
and aggregates them into a final verdict.  Default = 3 tracks:

  Track A: meta_reflection classifier  (current 7-layer finding_kind)
  Track B: pure-statistic heuristic     (win_rate, luck, sample_n)
  Track C: Bayesian posterior           (P(good|evidence) vs baseline)

The jury:
  - Discards verdicts with confidence < MIN_CONFIDENCE
  - Requires a quorum of >= 2 valid verdicts
  - If track kinds disagree: returns "contested" + the breakdown
  - If track kinds agree: returns the agreed kind + avg confidence

Pure-stdlib: no LLM calls inside the jury.  Tracks are injected.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple

# v231+: 阈值从 config 读，缺省值保持向后兼容。
# 注意：仅在模块加载时读一次（与原硬编码行为一致）。
try:
    import config as _cfg
    MIN_CONFIDENCE: float = float(getattr(_cfg, "JURY_MIN_CONFIDENCE", 0.20))
    MIN_QUORUM: int = int(getattr(_cfg, "JURY_MIN_QUORUM", 2))
except Exception:
    MIN_CONFIDENCE: float = 0.20
    MIN_QUORUM: int = 2

VALID_KINDS = frozenset({
    "confirmed", "refined", "invalidated",
    "stable_skill", "unstable_luck", "contested",
})


@dataclass
class TrackVerdict:
    track_name: str
    kind: str
    confidence: float
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "track_name": self.track_name,
            "kind":       self.kind,
            "confidence": round(self.confidence, 4),
            "evidence":   self.evidence,
        }


@dataclass
class JuryResult:
    kind: str
    confidence: float
    votes: List[TrackVerdict] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind":       self.kind,
            "confidence": round(self.confidence, 4),
            "votes":      [v.to_dict() for v in self.votes],
            "reason":     self.reason,
        }


Track = Callable[[Dict[str, Any]], TrackVerdict]


# ── built-in tracks ─────────────────────────────────────────────────


def track_stat_heuristic(evidence):
    """Track B: pure-statistic heuristic based on win_rate + luck."""
    n = int(evidence.get("n", 0))
    wr = float(evidence.get("win_rate", 0.5))
    luck = float(evidence.get("avg_luck", 0.0))
    base_wr = evidence.get("baseline_wr")
    if n < 5:
        return TrackVerdict("stat_heur", "confirmed", 0.0,
                            {"reason": "n<5 skip"})
    if luck > 0.30 and wr > 0.55:
        return TrackVerdict("stat_heur", "unstable_luck", 0.7,
                            {"wr": wr, "luck": luck})
    if luck < 0.30 and wr > 0.55:
        return TrackVerdict("stat_heur", "stable_skill", 0.7,
                            {"wr": wr, "luck": luck})
    if base_wr is not None:
        delta = wr - float(base_wr)
        if delta < -0.15:
            return TrackVerdict("stat_heur", "invalidated",
                                min(0.9, 0.4 + abs(delta)),
                                {"delta": delta})
        if delta > 0.15:
            return TrackVerdict("stat_heur", "refined",
                                min(0.9, 0.4 + delta),
                                {"delta": delta})
    return TrackVerdict("stat_heur", "confirmed", 0.5,
                        {"wr": wr, "luck": luck})


def track_bayes(evidence):
    """Track C: Bayesian posterior P(good | evidence) vs uniform prior."""
    n = int(evidence.get("n", 0))
    wr = float(evidence.get("win_rate", 0.5))
    alpha = 1.0
    p_good = (wr * n + alpha) / (n + 2 * alpha)
    confidence = abs(p_good - 0.5) * 2.0
    if n < 5:
        return TrackVerdict("bayes_post", "confirmed", 0.0,
                            {"p_good": round(p_good, 4)})
    if p_good > 0.6:
        return TrackVerdict("bayes_post", "refined", confidence,
                            {"p_good": round(p_good, 4)})
    if p_good < 0.4:
        return TrackVerdict("bayes_post", "invalidated", confidence,
                            {"p_good": round(p_good, 4)})
    return TrackVerdict("bayes_post", "confirmed", confidence,
                        {"p_good": round(p_good, 4)})


def track_meta_loop(evidence):
    """Track A: thin wrapper over meta_reflection_loop._classify_finding."""
    cur = evidence.get("cur")
    base = evidence.get("base")
    if not cur:
        return TrackVerdict("meta_loop", "confirmed", 0.0,
                            {"reason": "no cur dict"})
    try:
        from meta_reflection_loop import _classify_finding  # type: ignore
        kind, _detail, ev = _classify_finding(cur, base)
        jury_kind = "contested" if kind == "contradictory" else kind
        return TrackVerdict("meta_loop", jury_kind, 0.6, ev or {})
    except Exception as e:
        return TrackVerdict("meta_loop", "confirmed", 0.0,
                            {"error": str(e)[:200]})



# ── jury aggregator ─────────────────────────────────────────────────


class Jury:
    """A configurable collection of tracks that vote on a verdict."""

    def __init__(self, min_confidence=MIN_CONFIDENCE, min_quorum=MIN_QUORUM):
        self.min_confidence = min_confidence
        self.min_quorum = min_quorum
        self._tracks = []  # list of (name, fn)

    def register(self, name, track):
        if name in (n for n, _ in self._tracks):
            raise ValueError("track name already registered: " + name)
        self._tracks.append((name, track))

    def tracks(self):
        return [n for n, _ in self._tracks]

    def deliberate(self, evidence):
        if not self._tracks:
            return JuryResult(kind="confirmed", confidence=0.0,
                              reason="no tracks registered")

        raw = []
        for name, fn in self._tracks:
            try:
                v = fn(evidence)
                if v.track_name != name:
                    v.track_name = name
                raw.append(v)
            except Exception as e:
                raw.append(TrackVerdict(
                    track_name=name, kind="confirmed",
                    confidence=0.0,
                    evidence={"error": str(e)[:200]}))

        valid = [v for v in raw if v.confidence >= self.min_confidence]
        if len(valid) < self.min_quorum:
            return JuryResult(
                kind="confirmed", confidence=0.0, votes=raw,
                reason=("quorum failed: {} of {} tracks confident".format(
                    len(valid), len(raw))),
            )

        kind_counts = Counter(v.kind for v in valid)
        top_kind, top_count = kind_counts.most_common(1)[0]
        total_valid = len(valid)
        agreement_frac = top_count / total_valid
        avg_conf = sum(v.confidence for v in valid) / total_valid

        if agreement_frac >= 0.66:
            return JuryResult(
                kind=top_kind,
                confidence=avg_conf * agreement_frac,
                votes=valid,
                reason=("strong consensus: {}/{} tracks -> {}".format(
                    top_count, total_valid, top_kind)),
            )
        if agreement_frac >= 0.5:
            return JuryResult(
                kind=top_kind,
                confidence=avg_conf * agreement_frac * 0.8,
                votes=valid,
                reason=("plurality: {}/{} tracks -> {}".format(
                    top_count, total_valid, top_kind)),
            )
        return JuryResult(
            kind="contested",
            confidence=avg_conf * (1.0 - agreement_frac),
            votes=valid,
            reason=("contested: {} distinct kinds among {} tracks".format(
                len(kind_counts), total_valid)),
        )


def default_jury():
    """A 3-track jury: meta_loop + stat_heuristic + bayes."""
    j = Jury()
    j.register("meta_loop",  track_meta_loop)
    j.register("stat_heur",  track_stat_heuristic)
    j.register("bayes_post", track_bayes)
    return j

