"""Safe numeric coercion helper.

2026-07-11 doctrine step 5:
    Never call bare `float(x)` on values that came from a feeder,
    a snapshot dict, a broker response, or any external source.
    A single None / NaN / non-numeric string in one field cascades
    into a whole-tick abort — the exact failure mode that took out
    three brains on ETH/USD (see 2026-07-11 iter-27 log):

        intent_loop error brain=hellcat sym=ETH/USD:
        float() argument must be a string or a real number,
        not 'NoneType'

    Camino is unaffected because iter-27 already put it behind
    `build_camino_features` which uses this helper. Extending to
    GTO / Barracuda / Hellcat closes the same class of failure.

Contract:
    * Returns `float` on any finite numeric input (int / float /
      numeric string).
    * Returns `None` on None, NaN, ±inf, non-numeric strings, or
      unparseable values.
    * NEVER raises. Callers pattern-match on `is None` and gate
      their downstream logic explicitly (missing-field abstention
      instead of exception cascade).
"""
from __future__ import annotations

import math
from typing import Optional


def optional_float(value: object) -> Optional[float]:
    """Safe numeric coercion — returns `None` instead of raising.

    Use everywhere feeder / broker / snapshot values reach numeric
    code. Do NOT use `float(x)` on external data. See module
    docstring for rationale.
    """
    if value is None:
        return None
    try:
        number = float(value)          # int, float, or numeric str
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None                    # rejects NaN and ±inf
    return number
