"""Factorized Thompson-sampling bandit: learns which pillars, formats and hours earn the most engagement."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ArmStats:
    n: int
    mean: float


def arm_stats(rewards: Iterable[tuple[str, float]]) -> dict[str, ArmStats]:
    acc: dict[str, list[float]] = defaultdict(list)
    for arm, r in rewards:
        acc[str(arm)].append(r)
    return {k: ArmStats(len(v), sum(v) / len(v)) for k, v in acc.items()}


def _global_prior(stats: dict[str, ArmStats]) -> tuple[float, float]:
    total_n = sum(s.n for s in stats.values())
    if total_n == 0:
        return 0.0, 1.0
    mean = sum(s.mean * s.n for s in stats.values()) / total_n
    # Spread of arm means is a cheap, robust stand-in for reward noise.
    var = sum(s.n * (s.mean - mean) ** 2 for s in stats.values()) / total_n
    return mean, max(math.sqrt(var), 0.5)


def thompson_scores(
    arms: Sequence[str],
    stats: dict[str, ArmStats],
    *,
    prior_strength: float = 2.0,
    rng: random.Random | None = None,
) -> dict[str, float]:
    """Sample a plausible mean reward for every arm. Unseen arms get the global prior (optimism via variance)."""
    rng = rng or random.Random()
    mu0, sigma = _global_prior(stats)
    out: dict[str, float] = {}
    for arm in arms:
        s = stats.get(str(arm), ArmStats(0, mu0))
        n_eff = prior_strength + s.n
        post_mean = (prior_strength * mu0 + s.n * s.mean) / n_eff
        post_sd = sigma / math.sqrt(n_eff)
        out[arm] = rng.gauss(post_mean, post_sd)
    return out


def choose(
    arms: Sequence[str],
    stats: dict[str, ArmStats],
    *,
    weights: dict[str, float] | None = None,
    exclude: Iterable[str] = (),
    epsilon: float = 0.1,
    rng: random.Random | None = None,
) -> str:
    rng = rng or random.Random()
    excluded = set(exclude)
    pool = [a for a in arms if a not in excluded] or list(arms)
    weights = weights or {}
    if rng.random() < epsilon or not stats:
        w = [max(weights.get(a, 1.0), 1e-6) for a in pool]
        return rng.choices(pool, weights=w, k=1)[0]
    scores = thompson_scores(pool, stats, rng=rng)
    # Configured weights act as a prior preference: log-weight bonus in reward units.
    bonus = {a: math.log(max(weights.get(a, 1.0), 1e-6)) * 0.25 for a in pool}
    return max(pool, key=lambda a: scores[a] + bonus[a])


def choose_hours(
    hours: Sequence[int],
    stats: dict[str, ArmStats],
    k: int,
    *,
    min_gap_minutes: int,
    rng: random.Random | None = None,
) -> list[int]:
    """Pick k distinct hours, best-sampled first, keeping them spread out."""
    rng = rng or random.Random()
    scores = thompson_scores([str(h) for h in hours], stats, rng=rng)
    ranked = sorted(hours, key=lambda h: scores[str(h)], reverse=True)
    gap_h = max(1, math.ceil(min_gap_minutes / 60))
    while gap_h >= 1:
        chosen: list[int] = []
        for h in ranked:
            if all(abs(h - c) >= gap_h for c in chosen):
                chosen.append(h)
            if len(chosen) == k:
                return sorted(chosen)
        gap_h -= 1
    return sorted(ranked[:k])
