"""Shadow compare — runs primary AND candidate models for the same prompt.

Both calls go through the same `llm_kernel.call` surface and land in
the `llm_calls` ledger with the `comparison_lane` tag in metadata.
Returns both responses so the operator (or an automated grader) can
score them side by side.

Authority: ADVISORY_ONLY. Neither response is auto-routed anywhere.
"""
from shared.llm import llm_kernel


async def shadow_compare(role: str, task: str, prompt: str, metadata=None):
    """Run the operator's primary commercial provider AND a local
    candidate model on the same prompt. Both responses logged in
    `llm_calls`. Caller scores them.

    Note: the underlying kernel uses `provider_override` (not
    `force_provider`) to bypass routing. Both calls are still ledgered
    with full audit fields.
    """
    metadata = metadata or {}

    primary = await llm_kernel.call(
        role=role,
        task=task,
        prompt=prompt,
        metadata={**metadata, "comparison_lane": "primary"},
        provider_override="anthropic",
    )

    candidate = await llm_kernel.call(
        role=role,
        task=task,
        prompt=prompt,
        metadata={**metadata, "comparison_lane": "candidate"},
        provider_override="local",
    )

    return {
        "role": role,
        "task": task,
        "primary_call_id": primary.get("call_id"),
        "candidate_call_id": candidate.get("call_id"),
        "primary_response": primary.get("response"),
        "candidate_response": candidate.get("response"),
        "authority": "ADVISORY_ONLY",
    }
