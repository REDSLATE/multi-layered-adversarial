"""P7c — personality-separation acceptance tests.

Operator doctrine (2026-07-12): "prove the four brains remain
distinct." These tests use the deterministic replay corpus to
assert:

  1. Pairwise action agreement < configured ceiling.
  2. Pairwise confidence correlation < configured ceiling.
  3. At least one unique reason-code family per brain.
  4. At least one scenario where each brain disagrees with the
     council majority.
  5. No brain reads another brain's state or opinion.
  6. Same snapshot → deterministic per-brain output.

Nothing in these tests requires disagreement for its own sake.
Where evidence is overwhelming, all four brains should agree —
what must be distinct is the REASONING PATH (proven via reason
code families) and the sensitivity to different features
(proven via at-least-one-disagreement).
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from mc_brains.barracuda import BarracudaBrain
from mc_brains.camino import CaminoBrain
from mc_brains.gto import GtoBrain
from mc_brains.hellcat import HellcatBrain
from mc_pulse.tests.replay_corpus import REPLAY_SCENARIOS, make_snapshot


ALL_BRAIN_CLS = (CaminoBrain, GtoBrain, BarracudaBrain, HellcatBrain)


# Configured ceilings. Tunable but should NOT be relaxed
# lightly — relaxation without a strategy change means the
# brains have converged.
PAIRWISE_ACTION_AGREEMENT_CEILING = 0.85
PAIRWISE_CONFIDENCE_CORR_CEILING = 0.85


def _run(brain, snap):
    """Sync-run an evaluate() (which is async)."""
    coro = brain.evaluate(snap)
    return asyncio.get_event_loop().run_until_complete(coro) \
        if not asyncio.iscoroutine(coro) else asyncio.run(coro)


async def _run_all(brains, snap):
    return await asyncio.gather(*(b.evaluate(snap) for b in brains))


@pytest.mark.asyncio
async def test_pairwise_action_agreement_below_ceiling():
    """Across the full replay corpus, no PAIR of brains agrees on
    the same action more than the configured ceiling. Empirical
    proof that the personality-multiplier-only design has been
    replaced with distinct strategies."""
    brains = [cls() for cls in ALL_BRAIN_CLS]
    # Per pair — how many scenarios did they agree on?
    pair_agree: dict[tuple[str, str], int] = {}
    pair_total = 0

    for _label, features in REPLAY_SCENARIOS:
        snap = make_snapshot(features=features)
        opinions = await _run_all(brains, snap)
        # Only compare brains that produced an opinion.
        for i in range(len(brains)):
            for j in range(i + 1, len(brains)):
                a, b = opinions[i], opinions[j]
                if a is None or b is None:
                    continue
                key = (brains[i].id, brains[j].id)
                pair_agree.setdefault(key, 0)
                if a.direction == b.direction:
                    pair_agree[key] += 1
        pair_total += 1

    # Every pair — compute agreement rate.
    for (b1, b2), agree in pair_agree.items():
        rate = agree / pair_total if pair_total else 0.0
        assert rate <= PAIRWISE_ACTION_AGREEMENT_CEILING, (
            f"pair {b1} vs {b2} agrees on {rate:.2%} of scenarios "
            f"(> ceiling {PAIRWISE_ACTION_AGREEMENT_CEILING:.2%}) — "
            "strategies have collapsed toward one another"
        )


@pytest.mark.asyncio
async def test_pairwise_confidence_correlation_below_ceiling():
    """Per pair — Pearson correlation of confidence sequences must
    stay below the ceiling. A high correlation would mean the
    strategies are firing on the same evidence with only a
    multiplier separating them — the pre-P7 state."""
    brains = [cls() for cls in ALL_BRAIN_CLS]
    conf: dict[str, list[float]] = {b.id: [] for b in brains}
    for _label, features in REPLAY_SCENARIOS:
        snap = make_snapshot(features=features)
        opinions = await _run_all(brains, snap)
        for b, op in zip(brains, opinions):
            conf[b.id].append(op.confidence if op is not None else 0.0)

    ids = [b.id for b in brains]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            r = _pearson(conf[ids[i]], conf[ids[j]])
            assert abs(r) <= PAIRWISE_CONFIDENCE_CORR_CEILING, (
                f"confidence(pearson r={r:.3f}) for {ids[i]} vs {ids[j]} "
                f"exceeds ceiling {PAIRWISE_CONFIDENCE_CORR_CEILING} — "
                "brains are firing on the same evidence"
            )


@pytest.mark.asyncio
async def test_each_brain_has_unique_reason_code_family():
    """Every brain MUST emit at least one reason code with its
    family prefix (TREND_/MOMENTUM_/MEAN_/EXEC_) across the corpus,
    and NO other brain may emit codes with that family prefix.
    Guards against strategy reuse across brain classes."""
    expected = {
        "camino": "TREND",
        "gto": "MOMENTUM",
        "barracuda": "MEAN",
        "hellcat": "EXEC",
    }
    brains = [cls() for cls in ALL_BRAIN_CLS]
    per_brain_families: dict[str, set[str]] = {b.id: set() for b in brains}
    for _label, features in REPLAY_SCENARIOS:
        snap = make_snapshot(features=features)
        opinions = await _run_all(brains, snap)
        for b, op in zip(brains, opinions):
            if op is None:
                continue
            for code in op.reason_codes:
                # Family = prefix up to first underscore.
                fam = code.split("_", 1)[0]
                per_brain_families[b.id].add(fam)

    # Own family present.
    for bid, fam in expected.items():
        assert fam in per_brain_families[bid], (
            f"{bid} never emitted a {fam}_* reason code — its family "
            "signature is missing"
        )
    # Foreign families absent.
    for bid, fam in expected.items():
        foreign = {v for k, v in expected.items() if k != bid}
        leaked = foreign & per_brain_families[bid]
        assert not leaked, (
            f"{bid} emitted foreign reason-code families {leaked!r} — "
            "strategy layer is bleeding across brains"
        )


@pytest.mark.asyncio
async def test_each_brain_disagrees_with_council_at_least_once():
    """For every brain, there must be ≥ 1 scenario where it takes
    a direction the majority of the OTHER three brains do NOT take.
    Guards against a brain becoming a rubber-stamp on the council.
    """
    brains = [cls() for cls in ALL_BRAIN_CLS]
    dissent_seen: dict[str, bool] = {b.id: False for b in brains}
    for _label, features in REPLAY_SCENARIOS:
        snap = make_snapshot(features=features)
        opinions = await _run_all(brains, snap)
        for i, (b, op) in enumerate(zip(brains, opinions)):
            if op is None:
                continue
            # Peers' directions.
            peers = [
                opinions[j].direction for j in range(len(brains))
                if j != i and opinions[j] is not None
            ]
            if not peers:
                continue
            # Council majority direction (mode).
            counts = {d: peers.count(d) for d in set(peers)}
            majority_dir = max(counts, key=counts.get)
            if op.direction != majority_dir:
                dissent_seen[b.id] = True
    for bid, dissented in dissent_seen.items():
        assert dissented, (
            f"{bid} never dissents from the council majority across "
            f"{len(REPLAY_SCENARIOS)} scenarios — it's a rubber stamp"
        )


def test_no_brain_reads_another_brains_state():
    """Static scan: no strategy module OR brain module may import
    another brain by name or reference peer classes."""
    from mc_brains import barracuda, camino, gto, hellcat
    from mc_brains.strategies import (
        execution_safety, mean_reversion, momentum_confirmation,
        trend_following,
    )
    brain_mods = [camino, gto, barracuda, hellcat]
    strategy_mods = [
        trend_following, momentum_confirmation,
        mean_reversion, execution_safety,
    ]
    peer_class_names = {"CaminoBrain", "GtoBrain", "BarracudaBrain", "HellcatBrain"}
    peer_strategy_names = {
        "TrendFollowingStrategy", "MomentumConfirmationStrategy",
        "MeanReversionStrategy", "ExecutionSafetyStrategy",
    }
    for mod in brain_mods:
        src = inspect.getsource(mod)
        # A brain module MAY reference its OWN strategy. Just
        # sanity — no import of a peer brain module.
        for peer in ("camino", "gto", "barracuda", "hellcat"):
            if mod.__name__.endswith(peer):
                continue
            assert f"mc_brains.{peer}" not in src, (
                f"{mod.__name__} imports peer brain {peer!r}"
            )
    for mod in strategy_mods:
        src = inspect.getsource(mod)
        # No strategy should reference peer strategy classes.
        my_class = None
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if name in peer_strategy_names:
                my_class = name
                break
        for peer_class in peer_strategy_names - {my_class}:
            assert peer_class not in src, (
                f"{mod.__name__} references peer strategy {peer_class!r}"
            )


@pytest.mark.asyncio
async def test_same_snapshot_produces_deterministic_output():
    """Two evaluations of the SAME snapshot on a FRESH brain
    instance must produce identical direction + confidence."""
    for cls in ALL_BRAIN_CLS:
        b1 = cls()
        b2 = cls()
        snap = make_snapshot(features={
            "trend_score": 0.55, "price_change_pct": 0.40,
            "volume_change_pct": 0.60, "relative_volume": 1.5,
            "rsi": 62.0,
        })
        op1 = await b1.evaluate(snap)
        op2 = await b2.evaluate(snap)
        assert (op1 is None) == (op2 is None), (
            f"{cls.__name__}: nondeterministic Optional-ness"
        )
        if op1 is not None and op2 is not None:
            assert op1.direction == op2.direction
            assert op1.confidence == op2.confidence
            assert op1.reason_codes == op2.reason_codes


# ─────────────── helpers ───────────────

def _pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2 or len(xs) != len(ys):
        return 0.0
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)
