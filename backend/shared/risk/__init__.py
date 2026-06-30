"""Cross-cutting risk guards.

This subpackage holds:
    * Pre-trade gate (`check.py`)  — the ONE hard-limit check between
      Seat and Broker. Re-exported as `risk.check` and `risk.RiskCheck`
      at the package level so callers write `from shared import risk`.
    * Post-fill guards (`stop_loss_guard`, `take_profit_guard`,
      `trailing_stop_guard`, `max_hold_time_guard`, `position_monitor`)
      — lane-neutral position management.

Pre-trade vs post-fill is a real distinction:
    * Pre-trade decides WHETHER an order goes to the broker.
    * Post-fill manages POSITIONS that already exist.

Both live here because both are "risk." Each module is pure
deterministic math except `check.py` which is async (reads broker
freeze, lane toggle, executions for daily spend).
"""
from shared.risk.check import RiskCheck, check  # noqa: F401

__all__ = ["RiskCheck", "check"]
