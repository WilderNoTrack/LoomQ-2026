"""Turning an exact distribution into a ``counts`` dictionary.

Two modes, because "sample 8192 shots" means different things to a noise-free
simulator and to a real device:

``stratified`` (default)
    Largest-remainder allocation of the exact probabilities.  A noise-free
    simulator knows the distribution in closed form, so injecting pseudo-random
    shot noise would only add error to a number that is already exact.  This is
    the honest reading of "run this circuit on an ideal simulator", and it is
    what LoomQ reports unless asked otherwise.

``multinomial``
    Genuine i.i.d. sampling with a seeded RNG, for when you want to see the shot
    noise a real device would show.  Hardware and vendor-SDK runs are always
    naturally in this regime — this mode just reproduces it locally.

Set ``LOOMQ_SAMPLING=multinomial`` (and optionally ``LOOMQ_SEED``) to switch.
Whichever mode ran is recorded in ``meta.sampling`` of every result.
"""

import bisect
import os
import random
from typing import Dict, Optional

STRATIFIED = "stratified"
MULTINOMIAL = "multinomial"

DEFAULT_MODE = STRATIFIED


def resolve_mode(mode: Optional[str] = None) -> str:
    """Pick the sampling mode: explicit argument > environment > default."""
    candidate = (mode or os.environ.get("LOOMQ_SAMPLING") or DEFAULT_MODE).strip().lower()
    if candidate not in (STRATIFIED, MULTINOMIAL):
        return DEFAULT_MODE
    return candidate


def resolve_seed(seed: Optional[int] = None) -> Optional[int]:
    if seed is not None:
        return seed
    raw = os.environ.get("LOOMQ_SEED")
    if raw is None:
        return None
    try:
        return int(raw, 0)
    except ValueError:
        return None


def stratified_counts(distribution: Dict[str, float], shots: int) -> Dict[str, int]:
    """Largest-remainder allocation: exact expectation, no sampling noise.

    Every key gets ``floor(p * shots)``; the leftover shots go to the largest
    fractional parts, ties broken by bit string so the result is reproducible.
    """
    if shots <= 0:
        raise ValueError("shots must be a positive integer")
    ordered = sorted(distribution.items())
    allocated = {}  # type: Dict[str, int]
    remainders = []
    assigned = 0
    for key, probability in ordered:
        exact = probability * shots
        floor = int(exact)
        allocated[key] = floor
        assigned += floor
        remainders.append((exact - floor, key))

    leftover = shots - assigned
    if leftover > 0:
        remainders.sort(key=lambda item: (-item[0], item[1]))
        for _, key in remainders[:leftover]:
            allocated[key] += 1
        # More leftover than keys can absorb one each (possible only with a
        # pathological distribution): top up the most likely outcome.
        still = shots - sum(allocated.values())
        if still > 0:
            best = max(ordered, key=lambda item: (item[1], item[0]))[0]
            allocated[best] += still

    return {key: count for key, count in allocated.items() if count > 0}


def multinomial_counts(
    distribution: Dict[str, float], shots: int, seed: Optional[int] = None
) -> Dict[str, int]:
    """Draw ``shots`` i.i.d. samples from ``distribution``."""
    if shots <= 0:
        raise ValueError("shots must be a positive integer")
    keys = sorted(distribution)
    cumulative = []
    running = 0.0
    for key in keys:
        running += distribution[key]
        cumulative.append(running)
    if running <= 0.0:
        raise ValueError("distribution has zero total probability")
    cumulative[-1] = 1.0

    rng = random.Random(seed) if seed is not None else random.Random()
    counts = {}  # type: Dict[str, int]
    for _ in range(shots):
        key = keys[min(bisect.bisect_left(cumulative, rng.random()), len(keys) - 1)]
        counts[key] = counts.get(key, 0) + 1
    return counts


def sample_counts(
    distribution: Dict[str, float],
    shots: int,
    mode: Optional[str] = None,
    seed: Optional[int] = None,
) -> Dict[str, int]:
    """Produce a ``counts`` dictionary summing to exactly ``shots``."""
    if resolve_mode(mode) == MULTINOMIAL:
        return multinomial_counts(distribution, shots, resolve_seed(seed))
    return stratified_counts(distribution, shots)


__all__ = [
    "DEFAULT_MODE",
    "MULTINOMIAL",
    "STRATIFIED",
    "multinomial_counts",
    "resolve_mode",
    "resolve_seed",
    "sample_counts",
    "stratified_counts",
]
