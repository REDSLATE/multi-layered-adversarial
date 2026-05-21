"""PARADOX in-process coordinator.

Replaces the proposed Celery/Redis layer with an asyncio-based scheduler
that calls MC's gated endpoints over HTTP. Three guarantees:

  1. Every execute call goes through `/api/execution/submit` — the
     full 11-gate chain plus paradox-record writer.
  2. Each agent has its own enable flag. There is no global kill switch.
  3. Default state: every agent disabled. Operator must explicitly
     enable each one.

The coordinator is itself an emergent function — not a seat, not a
brain. It schedules. The kernel (PARADOX) still decides.
"""
