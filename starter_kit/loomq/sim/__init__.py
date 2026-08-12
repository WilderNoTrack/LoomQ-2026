"""LoomQ's own noise-free reference simulator.

It exists for three reasons, not one:

1. it is the executor of last resort when no vendor SDK is installed;
2. it is the oracle the backend layer cross-checks vendor results against;
3. it is what lets the L2 agent verify its own generated QASM before answering.

Because (3) runs inside a 120 s budget it has to be dependency-free and fast on
small circuits, which is why it is a plain statevector rather than a density
matrix.
"""

from .sampler import sample_counts, stratified_counts
from .statevector import (
    ideal_distribution,
    measurement_width,
    simulate_statevector,
)

__all__ = [
    "ideal_distribution",
    "measurement_width",
    "sample_counts",
    "simulate_statevector",
    "stratified_counts",
]
