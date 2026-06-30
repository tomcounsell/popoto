"""Shared, deterministic subset sampling for benchmark adapters.

Both the LongMemEval-S and LoCoMo adapters parse their full corpus into a
``list[BenchmarkItem]`` and then call :func:`sample_items` to select the
subset that a limited run should evaluate. Centralising the logic here means
representative sampling is identical across datasets and is unit-testable in
isolation (pure function, no I/O).

Why this exists (issue #435):
    LongMemEval-S is grouped by ``question_type`` on disk — the first ~70
    records are all the easiest single-evidence category. A naive
    ``items[:limit]`` ("head") therefore benchmarks only the easiest slice and
    reports near-perfect, unrepresentative numbers. ``stride`` and
    ``stratified`` span the whole corpus so small runs reflect real
    performance.

Modes:
    - ``head``       — contiguous prefix ``items[:limit]``. Legacy behaviour;
      retained only to reproduce historical runs. Not representative.
    - ``stride``     — deterministic even sampling across the full list using
      indices ``round(i * N / limit)`` for ``i in range(limit)``. Spans the
      whole dataset, needs no RNG, fully reproducible.
    - ``shuffle``    — ``random.Random(seed).sample(range(N), limit)``, then
      the selection is returned in original corpus order. Seeded.
    - ``stratified`` — group by ``metadata["question_type"]``, apportion
      ``limit`` across groups proportionally (largest-remainder so the counts
      sum to exactly ``limit``), and pick within each group by a seeded
      shuffle. Guarantees every category is represented in small runs. Items
      with an empty/``None`` ``question_type`` fall back to a single
      ``""`` bucket and are sampled gracefully.

Seeding:
    Modes that use randomness create a LOCAL ``random.Random(seed)`` instance
    and never touch the global RNG, so concurrent runs and unrelated code
    cannot perturb a selection. Same ``(items, limit, mode, seed)`` always
    yields the same subset.

Guards:
    - ``limit is None`` or ``limit >= len(items)`` → return all items unchanged.
    - ``limit <= 0`` → return ``[]``.
    - empty ``items`` → return ``[]``.
    - unknown ``mode`` → raise ``ValueError`` (fail loud, never silent).
"""

import random
from typing import List, Optional

from . import BenchmarkItem

VALID_MODES = ("head", "stride", "shuffle", "stratified")


def _question_type(item: BenchmarkItem) -> str:
    """Group key for stratified sampling.

    Empty string and ``None`` question types collapse to the same ``""``
    bucket so they are sampled together rather than crashing an apportionment.
    """
    qt = item.metadata.get("question_type")
    return qt if qt else ""


def _stratified(
    items: List[BenchmarkItem], limit: int, seed: int
) -> List[BenchmarkItem]:
    """Largest-remainder stratified sample over ``question_type`` groups."""
    rng = random.Random(seed)
    n = len(items)

    # Group indices by question_type, preserving first-seen group order.
    groups: dict = {}
    for idx, item in enumerate(items):
        groups.setdefault(_question_type(item), []).append(idx)

    # Apportion `limit` across groups: floor of the exact quota first, then
    # hand out the leftover seats to the largest fractional remainders.
    quotas: dict = {}
    remainders = []
    allocated = 0
    for qt, idxs in groups.items():
        exact = limit * len(idxs) / n
        base = int(exact)  # floor (exact >= 0)
        quotas[qt] = base
        allocated += base
        remainders.append((exact - base, qt))

    leftover = limit - allocated
    # Sort by remainder desc; deterministic tie-break on the group key.
    remainders.sort(key=lambda pair: (-pair[0], str(pair[1])))
    for i in range(leftover):
        quotas[remainders[i % len(remainders)][1]] += 1

    # Pick `quota` indices within each group via the seeded RNG.
    selected: List[int] = []
    for qt, idxs in groups.items():
        quota = quotas[qt]
        if quota <= 0:
            continue
        if quota >= len(idxs):
            chosen = list(idxs)
        else:
            chosen = rng.sample(idxs, quota)
        selected.extend(chosen)

    # Return in original corpus order for stable, readable output.
    selected.sort()
    return [items[i] for i in selected]


def sample_items(
    items: List[BenchmarkItem],
    limit: Optional[int],
    mode: str = "stride",
    seed: int = 0,
) -> List[BenchmarkItem]:
    """Select a representative subset of ``items``.

    Args:
        items: Fully parsed corpus (any iterable of BenchmarkItem).
        limit: Target subset size. ``None`` (or ``>= len(items)``) returns all
            items; ``<= 0`` returns an empty list.
        mode: One of ``head``, ``stride``, ``shuffle``, ``stratified``.
        seed: Seed for the local RNG used by ``shuffle``/``stratified``.

    Returns:
        A new list containing the selected items (original corpus order for
        every mode except ``head``, which is already a prefix).

    Raises:
        ValueError: If ``mode`` is not one of :data:`VALID_MODES`.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown sample mode {mode!r}; expected one of {VALID_MODES}")

    items = list(items)
    n = len(items)
    if n == 0:
        return []
    if limit is None or limit >= n:
        return items
    if limit <= 0:
        return []

    if mode == "head":
        return items[:limit]

    if mode == "stride":
        indices = [round(i * n / limit) for i in range(limit)]
        return [items[idx] for idx in indices]

    if mode == "shuffle":
        rng = random.Random(seed)
        chosen = sorted(rng.sample(range(n), limit))
        return [items[i] for i in chosen]

    # mode == "stratified"
    return _stratified(items, limit, seed)
