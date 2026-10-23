"""Statistics for time series: block averaging and drift (design 8.1)."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

Z_95 = 1.96  # normal approximation for the 95% interval


def block_average(values: Sequence[float], n_blocks: int = 5) -> tuple[float, float]:
    """Mean and block standard error (sample std of the block means over sqrt(n_blocks))."""
    arr = np.asarray(values, dtype=float)
    if len(arr) < n_blocks or n_blocks < 2:
        raise ValueError(f"need at least {n_blocks} values for {n_blocks} blocks")
    size = len(arr) // n_blocks
    blocks = arr[: size * n_blocks].reshape(n_blocks, size).mean(axis=1)
    return float(blocks.mean()), float(blocks.std(ddof=1) / math.sqrt(n_blocks))


def linear_drift(times: Sequence[float], values: Sequence[float]) -> tuple[float, float]:
    """Least-squares slope per time unit and the half-width of its 95% interval."""
    x = np.asarray(times, dtype=float)
    y = np.asarray(values, dtype=float)
    if len(x) < 3:
        raise ValueError("need at least 3 points for a drift estimate")
    sxx = float(((x - x.mean()) ** 2).sum())
    slope = float(((x - x.mean()) * (y - y.mean())).sum() / sxx)
    intercept = float(y.mean() - slope * x.mean())
    residual = y - (slope * x + intercept)
    sigma = math.sqrt(float((residual ** 2).sum()) / (len(x) - 2))
    return slope, Z_95 * sigma / math.sqrt(sxx)
