"""Adapter package — one file per provider.

Each adapter exposes the SAME function signature:

    async def call_<provider>(*, model, prompt, system, session_id)
        -> tuple[str, dict | None]

It returns `(response_text, usage_dict)`. The kernel is the only
caller. New providers drop in by adding a file + a route mapping in
`routing_policy.py`.
"""
