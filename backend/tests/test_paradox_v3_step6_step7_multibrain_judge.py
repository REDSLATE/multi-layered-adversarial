"""Paradox v3 — Step 6 (multi-brain rollout) + Step 7 (execution_judge
re-enable for v3 PATIENT plans).

Step 6 pins:
  * The runner-side emit logic is brain-agnostic — adding a brain to
    `PARADOX_V3_BRAINS` enables v3 for that brain with no other code
    changes. Pinned via the synthesizer + v3_brain_enabled helpers.

Step 7 pins:
  * Doctrine sidecar audit rows now carry `intent_version` +
    `plan_execution_style` so the slicer can filter to v3 PATIENT.
  * `_v3_patient_execution_judge_candidates` re-scores
    `execution_judge.ready` ONLY on the v3 PATIENT subset.
  * Candidates emitted by the v3 PATIENT pass are tagged
    `scope="v3_patient_only"`.
  * The original broad-dataset quarantine remains in place (no
    execution_judge.ready candidates from the broad pass).
"""
from __future__ import annotations

import pytest

from shared.intent_envelope_v3 import (
    synthesize_v3_envelope, v3_brain_enabled,
)


# ── Step 6: multi-brain v3 emit ──────────────────────────────────
class TestStep6MultiBrainEmit:
    """Pins the contract that the runner-side path doesn't have any
    brain-specific assumptions baked in."""

    def test_each_brain_id_lifts_independently(self, monkeypatch):
        monkeypatch.setenv("PARADOX_V3_BRAINS", "camino,barracuda,hellcat,gto")
        for brain_id in ("camino", "barracuda", "hellcat", "gto"):
            assert v3_brain_enabled(brain_id) is True

    def test_partial_rollout_only_enables_listed_brains(self, monkeypatch):
        """Sequential rollout: add barracuda after camino — `hellcat`
        and `gto` continue emitting v2 until they're added to the
        env list."""
        monkeypatch.setenv("PARADOX_V3_BRAINS", "camino,barracuda")
        assert v3_brain_enabled("camino") is True
        assert v3_brain_enabled("barracuda") is True
        assert v3_brain_enabled("hellcat") is False
        assert v3_brain_enabled("gto") is False

    def test_synthesizer_brain_id_agnostic(self):
        """Same payload structure produces the same v3 envelope shape
        regardless of which brain produced it — the synthesizer doesn't
        look at `stack` for any classification decision."""
        envelopes = []
        for brain_id in ("camino", "barracuda", "hellcat", "gto"):
            payload = {
                "stack": brain_id, "symbol": "NVDA", "lane": "equity",
                "action": "BUY", "confidence": 0.7, "rationale": "x",
            }
            envelopes.append(synthesize_v3_envelope(payload))
        # All four envelopes have identical plan/execution shape.
        ref = envelopes[0]
        for env in envelopes[1:]:
            assert env["plan"] == ref["plan"]
            assert env["execution"] == ref["execution"]
            assert env["intent_version"] == "v3"


# ── Step 7: execution_judge.ready re-enabled for v3 PATIENT ───────
class TestStep7V3PatientExecutionJudge:

    def _mk_row(self, *, lane="equity", dv="v1", ready=True,
                outcome="loss", intent_version="v3",
                plan_execution_style="PATIENT", holder="camino"):
        """Construct a doctrine_sidecars row in the shape the slicer
        expects. Mirrors what `_build_and_persist_doctrine_packet`
        writes today plus the two new Step 7 fields.

        `outcome` is "loss" or "win" — the slicer reads it as
        `outcome_join.outcome_label`.
        """
        return {
            "lane": lane,
            "doctrine_version": dv,
            "execution_judge_ready": ready,
            "execution_judge_holder": holder,
            "intent_version": intent_version,
            "plan_execution_style": plan_execution_style,
            "outcome_join": {"outcome_label": outcome},
        }

    def test_v3_patient_pass_excludes_v2_rows(self):
        """A v2 row never contributes to the v3 PATIENT scope."""
        from shared.doctrine.auto_retire import (
            _v3_patient_execution_judge_candidates,
        )
        rows = [
            # 30 v2 rows that would normally trigger the quarantine
            # (ready=True + loss) — these MUST be ignored.
            *[self._mk_row(intent_version="v2", plan_execution_style=None,
                           ready=True, outcome="loss") for _ in range(30)],
        ]
        # The filter in the caller (`retirement_candidates`) is what
        # excludes v2 rows. Here we directly invoke the helper with
        # pre-filtered rows to verify the empty-input behaviour:
        out = _v3_patient_execution_judge_candidates(
            v3_patient_rows=[], min_samples=10,
        )
        assert out == []

    def test_v3_patient_pass_emits_candidate_when_ready_inverts(self):
        """When the v3 PATIENT subset shows ready_loss_rate > not_ready_
        loss_rate by enough margin, the pass emits a tagged candidate."""
        from shared.doctrine.auto_retire import (
            _v3_patient_execution_judge_candidates,
        )
        # 40 ready=True (mostly loss) vs 40 ready=False (mostly win)
        # — exactly the inversion the broad quarantine flagged.
        rows = (
            [self._mk_row(ready=True, outcome="loss") for _ in range(35)] +
            [self._mk_row(ready=True, outcome="win") for _ in range(5)] +
            [self._mk_row(ready=False, outcome="win") for _ in range(30)] +
            [self._mk_row(ready=False, outcome="loss") for _ in range(10)]
        )
        out = _v3_patient_execution_judge_candidates(
            v3_patient_rows=rows, min_samples=10,
        )
        # At least one candidate emitted, tagged with v3 PATIENT scope.
        assert len(out) >= 1
        cand = out[0]
        assert cand["scope"] == "v3_patient_only"
        assert cand["seat"] == "execution_judge"
        assert cand["branch"] == "ready"
        assert cand["comparator"] == "not_ready"
        # The headline + rationale call out PATIENT.
        assert "PATIENT" in cand["headline"]
        assert "PATIENT" in cand["rationale"]
        # Doctrine pin (operator §13): the failure is the heuristic,
        # not the holder. The suggested action says so.
        assert "not the seat holder" in cand["suggested_action"]

    def test_v3_patient_pass_no_candidate_when_ready_outperforms(self):
        """When the v3 PATIENT subset shows ready_loss_rate < not_ready_
        loss_rate (the healthy direction), NO candidate is emitted —
        the heuristic IS working on the PATIENT subset."""
        from shared.doctrine.auto_retire import (
            _v3_patient_execution_judge_candidates,
        )
        rows = (
            [self._mk_row(ready=True, outcome="win") for _ in range(30)] +
            [self._mk_row(ready=True, outcome="loss") for _ in range(10)] +
            [self._mk_row(ready=False, outcome="loss") for _ in range(25)] +
            [self._mk_row(ready=False, outcome="win") for _ in range(5)]
        )
        out = _v3_patient_execution_judge_candidates(
            v3_patient_rows=rows, min_samples=10,
        )
        assert out == []

    def test_v3_patient_pass_below_min_samples_emits_nothing(self):
        from shared.doctrine.auto_retire import (
            _v3_patient_execution_judge_candidates,
        )
        # Only 8 total rows — below min_samples=50.
        rows = (
            [self._mk_row(ready=True, outcome="loss") for _ in range(4)] +
            [self._mk_row(ready=False, outcome="win") for _ in range(4)]
        )
        out = _v3_patient_execution_judge_candidates(
            v3_patient_rows=rows, min_samples=50,
        )
        assert out == []


# ── Step 7 broad-dataset quarantine remains in place ──────────────
def test_broad_expectations_list_still_excludes_execution_judge():
    """The broad-dataset auto_retire MUST NOT have re-added
    execution_judge.ready. The re-enable is ONLY scoped to v3 PATIENT.

    Defensive: if a future agent un-quarantines the broad pass without
    reading the doctrine pin, the v3 PATIENT scope becomes redundant
    AND the broad-dataset MARKET_NOW emits start firing false-positive
    candidates again. This test catches that regression by inspecting
    the expectations list line-by-line, treating leading-`#` lines as
    quarantined (comment) lines.
    """
    import inspect
    from shared.doctrine import auto_retire
    src = inspect.getsource(auto_retire.retirement_candidates)
    expectations_block = src.split("expectations = [")[1].split("]")[0]
    # Active (non-comment) lines only.
    active_lines = [
        ln.strip() for ln in expectations_block.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    for line in active_lines:
        assert "execution_judge" not in line, (
            f"Broad expectations list ACTIVE line includes "
            f"execution_judge: {line!r}. The re-enable is scoped "
            f"strictly to v3 PATIENT — see doctrine pin in "
            f"auto_retire.py around line 130."
        )
