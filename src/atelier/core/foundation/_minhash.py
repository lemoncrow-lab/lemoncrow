"""Pure-Python MinHash — replaces datasketch.MinHash with zero quality loss.

Algorithm
---------
For each token (bytes), compute a 32-bit SHA-1 digest, then apply ``num_perm``
independent linear permutations modulo the Mersenne prime M61.  The minimum
per permutation is the MinHash signature.

Jaccard estimate accuracy is identical to datasketch: error ≈ 1/√num_perm.
No external dependencies — stdlib only.
"""

from __future__ import annotations

import hashlib
import random
import struct

_PRIME: int = (1 << 61) - 1  # Mersenne prime M61
_MAX: int = (1 << 32) - 1

_PARAMS_CACHE: dict[tuple[int, int], tuple[list[int], list[int]]] = {}


def _get_params(num_perm: int, seed: int) -> tuple[list[int], list[int]]:
    key = (num_perm, seed)
    if key not in _PARAMS_CACHE:
        rng = random.Random(seed)
        a = [rng.randint(1, _PRIME) for _ in range(num_perm)]
        b = [rng.randint(0, _PRIME) for _ in range(num_perm)]
        _PARAMS_CACHE[key] = (a, b)
    return _PARAMS_CACHE[key]


class MinHash:
    """MinHash signature for Jaccard similarity estimation.

    Drop-in replacement for ``datasketch.MinHash`` covering the API used in
    Atelier: ``update``, ``jaccard``, ``hashvalues``, ``num_perm``.
    """

    __slots__ = ("_a", "_b", "hashvalues", "num_perm")

    def __init__(self, num_perm: int = 128, seed: int = 1) -> None:
        self.num_perm = num_perm
        self._a, self._b = _get_params(num_perm, seed)
        self.hashvalues: list[int] = [_MAX] * num_perm

    def update(self, b: bytes) -> None:
        """Incorporate one token (bytes) into the signature."""
        h = struct.unpack("<I", hashlib.sha1(b).digest()[:4])[0]
        prime = _PRIME
        mx = _MAX
        a, bv, hv = self._a, self._b, self.hashvalues
        for i in range(self.num_perm):
            v = (a[i] * h + bv[i]) % prime & mx
            if v < hv[i]:
                hv[i] = v

    def jaccard(self, other: MinHash) -> float:
        """Estimate Jaccard similarity with *other*."""
        if self.num_perm != other.num_perm:
            raise ValueError(f"num_perm mismatch: {self.num_perm} vs {other.num_perm}")
        return sum(a == b for a, b in zip(self.hashvalues, other.hashvalues, strict=True)) / self.num_perm


__all__ = ["MinHash"]
