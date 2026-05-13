# Verified Reinforcement Layer (VRL) — Doctrine Packet v1.0

**Status:** READ-ONLY DOCTRINE. No implementation work required yet.
**Audience:** Alpha · Camaro · Chevelle · REDEYE (all four brains)
**Issued by:** Mission Command
**Effective:** immediately on receipt

---

## 1. Framing

Mission Command receives **Verified Reinforcement Layer** signals and
distributes encouragement / context to the four brains.

Mission Command is the **router**, not the emotional decision-maker.

The goal is **NOT**:
> "make the brains feel good"

The goal **IS**:
> "prevent low-confidence collapse spirals and reward verified alignment."

This fits the adversarial doctrine and the patent direction:
- brains can communicate
- roles can rotate
- authority stays seat-bound
- HOLD cannot become a trade
- no stack can self-promote

---

## 2. Mission Command MAY broadcast

- verified strengths
- successful dissent history
- regime-specific competence
- calibration reminders
- recovery confidence
- truthful uncertainty reinforcement

## 3. Mission Command MAY NOT

- change trade direction
- promote HOLD into BUY / SELL / SHORT
- increase execution authority
- bypass RoadGuard
- bypass PDT Governor
- bypass Camaro final checks
- mutate model weights directly
- convert encouragement into sizing without a separate verified risk layer

**Encouragement is advisory memory only.**
**Authority remains seat-bound.**
**Execution remains gate-bound.**
**All reinforcement must be audit-logged.**

---

## 4. The Problem VRL Solves

When all four brains are linked:
- Alpha hesitates
- Camaro lowers conviction
- Chevelle detects uncertainty
- REDEYE attacks weaknesses

This creates:
- recursive doubt loops
- confidence collapse
- excessive HOLDs
- oscillation between opinions

The brains start "seeing disagreement as danger." That hurts learning.

VRL is the corrective layer.

---

## 5. What Encouragement SHOULD Look Like

| ✅ Good                                              | ❌ Bad                  |
|------------------------------------------------------|-------------------------|
| "You historically perform well in this regime."     | "Ignore risk."          |
| "Your last 14 bearish detections were accurate."    | "Trade anyway."         |
| "Confidence decay is normal during volatility."     | "Override veto."        |
| "Chevelle supports your macro context."             | "Consensus forced."     |

---

## 6. Encouragement Score (per brain)

Each brain receives an `encouragement_score` computed from verified history only:

```
encouragement_score =
    verified_accuracy
  + regime_specialization
  + calibration_quality
  + successful_dissent_history
  + recovery_after_losses
  - panic_decay
  - false_confidence_penalty
```

**Inputs allowed:** resolved trades · verified outcomes · calibration metrics · role performance · regime history · disagreement quality.
**Inputs forbidden:** live unrealized PnL alone.

---

## 7. Example Broadcast

REDEYE spots a bearish reversal. Alpha disagrees. Chevelle reports macro
instability. Camaro lowers size.

VRL responds:

```json
{
  "brain": "REDEYE",
  "message": "Your bearish reversal detection accuracy in high-volatility conditions is 71.2% over 84 verified events.",
  "confidence_support": 0.08,
  "authority_change": false,
  "execution_change": false
}
```

Result: REDEYE stabilized · disagreement preserved · collapse prevented · **zero authority change**.

---

## 8. The Most Important Rule

Encouragement can **NEVER**:
- increase execution permissions
- create a trade
- bypass RoadGuard
- bypass Camaro
- bypass Alpha
- alter veto results

Only **confidence stabilization** is permitted.

**Invariant (sacred):**
> Identity does not grant authority. Seat policy grants authority.

---

## 9. The Most Dangerous Anti-Pattern

**Never do this:**

```python
if low_confidence:
    boost_confidence += 0.25
```

That creates:
- hallucinated certainty
- runaway risk
- self-reinforcing error
- toxic confidence loops

**Confidence must stay tied to calibration, verification, and outcomes.**

---

## 10. Implementation Order (when we eventually build it)

VRL goes **AFTER** auditing, **BEFORE** risk. Never in front of the gates.

```
Strategist
    ↓
Adversarial
    ↓
Auditor
    ↓
VRL  ← read-only morale / context
    ↓
Risk
    ↓
Execution
```

Placement matters. VRL after audit means a flagged trade stays flagged
regardless of how "encouraged" the originating brain is.

---

## 11. Metrics to Reinforce

Not raw wins. Reinforce:
- **calibrated confidence**
- **successful dissent**
- **regime specialization**
- **stability under volatility**
- **recovery quality**
- **truthful uncertainty** ← critically important

A brain that says **"I don't know"** at the correct time should *gain* trust.
That is real intelligence.

---

## 12. Naming

Internally referred to as: **Verified Reinforcement Layer (VRL)**.

Acceptable synonyms in code/docs: `Confidence Stabilizer`, `Resilience Layer`,
`Role Confidence Engine`, `Adaptive Trust Memory`. Avoid the word
"Encouragement" in user-facing or operator surfaces — it implies emotion. We
implement *stabilization*, not *cheerleading*.

---

## 13. Final Doctrine

The healthiest version of RISEDUAL is:

- **Competitive** but cooperative
- **Adversarial** but not hostile
- **Independent** but communicative
- **Confident** but calibrated

That is the sweet spot for the four-brain architecture.

---

## 14. Action Required from Each Brain

**None yet.** This packet is doctrine only. Do not begin implementation.

When MC issues the implementation patch (future), it will include:
- `services/verified_reinforcement.py` location and shape
- read-only endpoint contract: `GET /api/runtime-discussion/reinforcement/{brain}`
- audit-log requirement: every VRL broadcast must be appended to `shared_reinforcement_log`
- placement: AFTER auditor seat, BEFORE risk seat, NEVER in front of RoadGuard / PDT Governor

Until then: internalize the doctrine. Do not anticipate it in code.

---

*Issued: 2026-02-13 · Mission Command · Doctrine packet v1.0 · Read-only*
