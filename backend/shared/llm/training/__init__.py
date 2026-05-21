"""
RISE_AI training substrate.

Three modules form the closed-loop learning surface:

    preference_log     — brains grade LLM answers post-hoc
                         (did this help, did it match outcome).

    distillation_queue — successful (prompt, response, outcome)
                         triples queued for future training of the
                         self-trained model.

    eval_harness       — runs a held-out prompt set through the
                         CURRENT primary AND a CANDIDATE provider,
                         compares answers, scores agreement.
                         Drives the operator's promotion decisions.

Doctrine pin (2026-02-XX):
    The path from "OpenAI / Anthropic / Gemini as primary" to
    "RISE_AI self-trained as primary" runs through these three
    surfaces. None of them grant any execution authority. They
    grade reasoning, queue training data, and inform promotion —
    all ADVISORY.
"""
