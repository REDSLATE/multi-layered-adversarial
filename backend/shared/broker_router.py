"""Broker router — single dispatch point.

Doctrine:
    * Lane → broker registry decides WHICH adapter handles an order.
    * Canonical asset key is the ONLY identity the brain ships.
    * Resolver translates canonical → broker-native at the last mile.
    * Every fail mode is NO_TRADE: missing lane, missing mapping, missing
      adapter, lane mismatch.
    * **MC receipt seal (2026-05-18)**: before ANY broker call, the
      router mints an HMAC-signed `MCExecutionReceipt` via
      `shared.runtime.platform_survival.mc_canonical_gate`. The broker
      adapter receives it and (when `RISEDUAL_BROKER_REQUIRE_MC_RECEIPT`
      is `true`) refuses to place the order if the signature is missing,
      tampered, or carries `accepted=false`. While the flag is `false`
      (the default during Alpha sidecar rollout), MC mints the receipt
      and *logs* mismatches but doesn't block — so PROD Alpha keeps
      trading until the sidecar kit lands. Flip to `true` once Alpha
      adopts `services.platform_survival.sidecar_build_intent(...)`.

Order-routing layers (execution.py manual submit, auto_router.py) call
exactly one function here: `route_order(intent, notional_usd)`.

The router NEVER decides identity. It only:
    1. Reads `intent.lane` and `intent.symbol` (or composes from them)
    2. Looks up the broker for the lane
    3. Asks the resolver for the broker-native symbol
    4. Fetches the adapter for that broker
    5. Mints + attaches an MC receipt (and optionally enforces it)
    6. Calls `adapter.submit_market_order(...)`
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from shared.broker.webull import get_webull_adapter
from shared.broker.webull_caps import WebullCapBlocked, evaluate_webull_order
from shared.broker_freeze import BrokerFrozen, assert_not_frozen
from shared.crypto.broker_adapter import get_kraken_adapter
from shared.broker_symbol_resolver import (
    AssetKey,
    BrokerSymbolUnresolved,
    CanonicalError,
    LaneRoutingError,
    broker_for_lane,
    compose,
    resolve_broker_symbol,
)
from shared.runtime.platform_survival import (
    broker_verify_receipt,
    mc_canonical_gate,
    policy_hash,
)


logger = logging.getLogger("risedual.broker_router")


# ─────────────────────── adapter registry ───────────────────────
#
# Function-based; each broker name maps to an async `get_<broker>_adapter`
# loader. Stubs return None — that's NO_TRADE territory by design.

async def _get_public_adapter():
    """Build a `PublicAdapter` from the operator-stored credentials.

    Returns None when:
      * no credentials stored (operator hasn't run /admin/public/connect)
      * no account_id pinned (operator must pick one when ≥2 accounts exist)
      * execution_enabled=False on the stored credential singleton
        (operator-level kill switch — distinct from MC's gate chain)

    All None-returns route equity orders to the Alpaca fallback —
    crypto is unaffected because it routes to Kraken via a different
    loader. Doctrine pin (operator, 2026-06-07): NEVER let Public's
    misconfiguration close equity trading; Alpaca remains the
    fallback path until Public is verified live for a week.
    """
    try:
        from shared.public import get_active, _stored_doc  # noqa: WPS433
        from shared.broker.public import PublicAdapter  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        logger.warning("PublicAdapter import failed: %s", e)
        return None
    try:
        doc = await _stored_doc()
        if not doc:
            return None
        if not doc.get("execution_enabled"):
            # Operator-side kill switch is OFF — fall through to Alpaca.
            return None
        active = await get_active()
        if not active or not active.get("account_id"):
            return None
        return PublicAdapter(
            base_url=active["base_url"],
            access_token=active["access_token"],
            account_id=active["account_id"],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("PublicAdapter load failed: %s", e)
        return None


async def _get_ibkr_adapter():
    return None  # not yet wired


async def _get_equity_adapter():
    """Equity-lane adapter resolver.

    2026-02-19 (operator directive): Public.com and Alpaca are
    deprecated. Webull is the SOLE equity broker. This resolver
    delegates to the Webull adapter so any legacy caller still
    landing on the `alpaca_paper` slot name routes correctly.

    Fail-closed: if Webull credentials aren't configured the
    adapter loader returns None and the router raises
    `BrokerRouteBlocked`; equity NO_TRADEs rather than silently
    routing through a deprecated path.
    """
    return await get_webull_adapter()


ADAPTER_LOADERS = {
    "kraken": get_kraken_adapter,
    "public": _get_public_adapter,
    "ibkr": _get_ibkr_adapter,
    "webull": get_webull_adapter,
    # Legacy slot alias kept so any DB row still pinned to
    # `alpaca_paper` (pre-2026-02-19 broker_selection rows) routes
    # to the current equity adapter (Webull) instead of NO_TRADE.
    # The constant is decorative — it does NOT load an Alpaca client.
    "alpaca_paper": _get_equity_adapter,
}


# Brokers that act as a per-intent operator override across BOTH lanes.
# Setting `intent.broker_override = "webull"` routes that single intent
# through Webull instead of the lane's default broker. Public / Kraken
# / Alpaca cannot be selected as overrides — they ARE the defaults for
# their lanes; the override exists precisely to opt INTO an alternative
# without erasing the lane-default keys.
ROUTE_OVERRIDE_BROKERS: set[str] = {"webull"}


class BrokerRouteBlocked(Exception):
    """Raised when routing cannot complete. Surfaced as a gate failure
    by the calling layer. ALWAYS NO_TRADE — fail-closed."""


# ─────────────────────── MC receipt seal ───────────────────────

def _broker_require_mc_receipt() -> bool:
    """Read at call-time so the operator can flip the flag in `.env`
    without restarting the server. Doctrine pin (2026-05-23): defaults
    to TRUE after the orphan audit — bypass is the bug we're closing.
    Set RISEDUAL_BROKER_REQUIRE_MC_RECEIPT=false in `.env` only during
    a deliberate rollback (logged + reviewed)."""
    return os.getenv("RISEDUAL_BROKER_REQUIRE_MC_RECEIPT", "true").strip().lower() in {
        "true", "1", "yes", "on",
    }


def _mint_and_verify_mc_receipt(
    *,
    intent: dict,
    asset: AssetKey,
    side: str,
    notional_usd: float,
) -> dict:
    """Build an envelope from the intent, run it through
    `mc_canonical_gate(...)`, verify the signature with
    `broker_verify_receipt(...)`, and return a status dict.

    Returns:
        {ok: bool, enforced: bool, reason: str, receipt: dict|None}

    When `RISEDUAL_BROKER_REQUIRE_MC_RECEIPT=true`, callers MUST refuse
    the order if `ok=False`. When the flag is unset/false (rollout
    mode), this function still mints the receipt and surfaces the
    status — the operator can observe pass/fail in PROD logs before
    flipping enforcement on.
    """
    # Build a survival-layer envelope from the existing intent's MC-stamped
    # fields. Sidecars that already shipped the kit may include a
    # full `runtime` stamp on the intent; if absent, we synthesize a
    # neutral one so the gate has something to inspect. The receipt is
    # still cryptographically valid regardless — what changes is whether
    # validate_for_prod_sidecar would have flagged the upstream stamp.
    enforced = _broker_require_mc_receipt()
    envelope = {
        "brain_id": intent.get("stack") or "unknown",
        "lane": asset.lane,
        "symbol": asset.canonical,
        "direction": side,
        "confidence": float(intent.get("confidence") or 0.0),
        "room_id": intent.get("room_id") or f"{intent.get('stack', 'unknown')}_room",
        "runtime": intent.get("runtime") or {
            # Neutral synthesized stamp — proves the sidecar did NOT
            # claim local execution authority. Real PROD adoption
            # replaces this with the sidecar's actual RuntimeStamp.
            "app_name": "risedual",
            "env_name": os.getenv("RISEDUAL_ENV", "unknown"),
            "git_sha": os.getenv("GIT_SHA", "unknown"),
            "platform": os.getenv("RISEDUAL_PLATFORM", "unknown"),
            "mc_url": os.getenv("RISEDUAL_MC_URL", ""),
            "db_name": os.getenv("RISEDUAL_DB_NAME", os.getenv("DB_NAME", "")),
            "broker_mode": os.getenv("RISEDUAL_BROKER_MODE", "unknown"),
            "sidecar_room": intent.get("stack", "unknown"),
            "sidecar_version": "synthesized_by_mc",
            "policy_hash": policy_hash(),
            "local_execution_authority": False,
            "timestamp_ms": 0,
        },
    }
    # Pre-flight: notional is the broker-effective size; pass it
    # transparently as the confidence input is independent.
    _ = notional_usd

    gate = mc_canonical_gate(envelope)
    receipt = gate.get("receipt")
    verify = broker_verify_receipt(receipt) if receipt else {"ok": False, "reason": "NO_RECEIPT"}

    if not enforced and not verify["ok"]:
        # Rollout mode: log the failure but allow the order through so
        # PROD Alpha can keep trading until its sidecar adopts the kit.
        logger.warning(
            "mc receipt verification FAILED but enforcement is OFF "
            "(RISEDUAL_BROKER_REQUIRE_MC_RECEIPT!=true) — reason=%s intent=%s",
            verify["reason"], intent.get("intent_id"),
        )

    return {
        "ok": verify["ok"],
        "enforced": enforced,
        "reason": verify["reason"],
        "receipt": receipt,
    }


# ─────────────────────── canonical composition ───────────────────────

def compose_asset(intent: dict) -> AssetKey:
    """Compose the canonical AssetKey from an intent.

    Accepts:
        - `intent.canonical` already-composed (preferred — brains
          shipping the canonical themselves)
        - `intent.symbol` + `intent.lane` (MC composes here)

    Fail-closed if neither path works.
    """
    canonical = intent.get("canonical")
    if canonical:
        # Parse back into AssetKey for type-safety downstream. We don't
        # trust the brain to know its own lane — re-derive.
        if canonical.startswith("EQ:"):
            base = canonical.split(":", 1)[1]
            return AssetKey(canonical=canonical, lane="equity", base=base, quote=None)
        if canonical.startswith("CRYPTO:"):
            tail = canonical.split(":", 1)[1]
            base, _, quote = tail.partition("-")
            return AssetKey(
                canonical=canonical, lane="crypto",
                base=base, quote=quote or "USD",
            )
        raise CanonicalError(f"unknown canonical prefix: {canonical!r}")

    symbol = intent.get("symbol")
    lane = intent.get("lane")
    return compose(symbol, lane)


# ─────────────────────── routing ───────────────────────

async def route_order(
    intent: dict,
    *,
    notional_usd: float,
    client_order_id: Optional[str] = None,
) -> dict:
    """Route a single intent's order to the correct broker.

    Returns the adapter's order-response dict on success.
    Raises BrokerRouteBlocked on any NO_TRADE condition.

    The caller (auto-router or /execution/submit) is responsible for
    running its full gate chain BEFORE calling this — the router only
    enforces broker-identity invariants, NOT trade-policy gates.
    """
    intent_id = intent.get("intent_id", "<unknown>")

    # 0. Emergency freeze — supersedes everything. If the operator
    #    flipped the freeze on, NO broker write happens, period. This
    #    runs BEFORE adapter resolution so we never even fetch creds.
    try:
        await assert_not_frozen()
    except BrokerFrozen as e:
        raise BrokerRouteBlocked(str(e)) from e

    # 1. Compose canonical AssetKey.
    try:
        asset = compose_asset(intent)
    except CanonicalError as e:
        raise BrokerRouteBlocked(
            f"intent {intent_id} has no resolvable canonical asset: {e}"
        ) from e

    # 1b. Operator lane toggle — runtime on/off switch per lane. Set
    #     via POST /admin/broker/lanes/{lane}/toggle. Defaults to
    #     enabled when no row exists; explicitly disabling a lane
    #     turns off ALL trades in that lane, ignoring broker creds
    #     and per-brain ladder stage. Runs BEFORE we touch any
    #     credentials so we never even probe Public/Kraken when the
    #     operator has flipped a lane off.
    try:
        from routes.broker_lane_admin import is_lane_enabled  # noqa: WPS433
        if not await is_lane_enabled(asset.lane):
            raise BrokerRouteBlocked(
                f"lane {asset.lane!r} is disabled by operator toggle; NO_TRADE"
            )
    except BrokerRouteBlocked:
        raise
    except Exception as exc:  # noqa: BLE001 — fail open on toggle lookup errors
        # We DELIBERATELY fail-open here: a Mongo blip on the toggle
        # collection must NOT kill all trading. The downstream
        # ladder/credentials/execution gates remain. Log loudly so
        # the operator sees it.
        logger.warning(
            "broker lane toggle lookup failed lane=%s err=%s — failing open",
            asset.lane, exc,
        )

    # 2. Pick broker by lane — unless the intent carries an operator
    #    override (e.g. `broker_override="webull"`). The override is
    #    only honored for brokers in `ROUTE_OVERRIDE_BROKERS`; anything
    #    else falls back to the lane default so a stale or hostile
    #    intent can't redirect to Public/Kraken/Alpaca arbitrarily.
    #
    # 2026-02-19 (operator: "the switch isn't lighting up anything"):
    #    When no per-intent override is set, consult the broker
    #    selection singleton so the UI hamburger ACTUALLY drives
    #    routing. Previously the selection was UI-only — the route
    #    always fell through to the lane default. Now: per-intent
    #    override (still wins) > broker_selection (operator UI) >
    #    lane default. Selection is read once per route_order call;
    #    a failure here falls through to the lane default rather
    #    than killing the trade.
    override = (intent.get("broker_override") or "").strip().lower() or None
    if override and override in ROUTE_OVERRIDE_BROKERS:
        broker_name = override
    else:
        broker_name = None
        try:
            from routes.broker_selection import get_current_selection  # noqa: WPS433
            sel = await get_current_selection()
            sel_choice = (sel.get(asset.lane) or "").strip().lower() or None
            if sel_choice and sel_choice in ADAPTER_LOADERS:
                broker_name = sel_choice
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "broker_selection lookup failed lane=%s err=%s — falling "
                "back to lane default", asset.lane, exc,
            )
        if not broker_name:
            try:
                broker_name = broker_for_lane(asset.lane)
            except LaneRoutingError as e:
                raise BrokerRouteBlocked(str(e)) from e

    # 2b. Webull-specific pre-trade cap gate. Runs BEFORE symbol
    #     resolution / adapter load so a refused order doesn't
    #     consume credentials or burn an MC receipt slot. Doctrine
    #     pin (operator, 2026-06-10): WEBULL_ARMED must be true AND
    #     notional must satisfy $3 ≤ N ≤ $10 per ticker for the
    #     small-pilot route. The cap evaluator carries both checks.
    if broker_name == "webull":
        decision = evaluate_webull_order(
            notional_usd=notional_usd, symbol=asset.canonical,
        )
        if not decision.ok:
            raise BrokerRouteBlocked(decision.reason)

    # 3. Translate canonical → broker-native.
    try:
        broker_symbol = resolve_broker_symbol(asset, broker_name)
    except BrokerSymbolUnresolved as e:
        raise BrokerRouteBlocked(str(e)) from e

    # 4. Fetch the live adapter.
    loader = ADAPTER_LOADERS.get(broker_name)
    if not loader:
        raise BrokerRouteBlocked(
            f"no adapter loader registered for broker {broker_name!r}; NO_TRADE"
        )
    adapter = await loader()
    if adapter is None:
        raise BrokerRouteBlocked(
            f"broker {broker_name!r} adapter not configured (no credentials?); NO_TRADE"
        )

    # 5. Mint + verify the MC execution receipt — the doctrinal seal.
    side = "BUY" if intent.get("action") in ("BUY", "COVER") else "SELL"
    receipt_check = _mint_and_verify_mc_receipt(
        intent=intent,
        asset=asset,
        side=side,
        notional_usd=notional_usd,
    )
    if receipt_check["enforced"] and not receipt_check["ok"]:
        raise BrokerRouteBlocked(
            f"MC receipt rejected: {receipt_check['reason']}; NO_TRADE"
        )

    # 6. Submit through the adapter.
    logger.info(
        "route_order intent=%s canonical=%s lane=%s broker=%s broker_sym=%s side=%s $%.2f receipt=%s override=%s",
        intent_id, asset.canonical, asset.lane, broker_name, broker_symbol,
        side, notional_usd, receipt_check["reason"], override or "none",
    )
    try:
        order = await adapter.submit_market_order(
            symbol=broker_symbol if isinstance(broker_symbol, str) else asset.base,
            notional=notional_usd,
            side=side,
            client_order_id=client_order_id,
            mc_receipt=receipt_check.get("receipt"),
        )
    except WebullCapBlocked as e:
        # Belt-and-braces re-check inside the adapter fired. Re-raise
        # as a clean route block so the auto-router treats it like
        # any other NO_TRADE.
        raise BrokerRouteBlocked(str(e)) from e
    # Stamp routing metadata so receipts can be sliced by broker / lane.
    order.setdefault("broker", broker_name)
    order["lane"] = asset.lane
    order["canonical"] = asset.canonical
    order["broker_symbol"] = broker_symbol if isinstance(broker_symbol, str) else str(broker_symbol)
    # Surface MC receipt provenance so the auto-router/execution receipt
    # rows can slice fills by signature presence.
    order["mc_receipt"] = receipt_check.get("receipt")
    order["mc_receipt_status"] = receipt_check["reason"]
    order["mc_receipt_enforced"] = receipt_check["enforced"]

    # Bracket-outcome training signal capture (2026-02-19, P1).
    # When the brain's intent carries `target_price` + `stop_price`,
    # record the bracket thesis so the outcome resolver can later
    # assign tp_hit/sl_hit/timeout — categorical training labels that
    # are directly aligned with the brain's stated conviction. This
    # is a NO-OP when the brain didn't publish a bracket or when the
    # operator hasn't flipped RISEDUAL_BRACKET_OUTCOMES_ENABLED on.
    # Failures here NEVER block the trade — the bracket recorder
    # is best-effort by design.
    try:
        from shared.broker.webull_brackets import record_bracket_intent
        entry_price = float(
            order.get("filled_avg_price")
            or (order.get("notional") or 0) / max(order.get("qty") or 1e-9, 1e-9)
        )
        await record_bracket_intent(
            intent=intent, order=order, entry_price=entry_price,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "bracket_intent capture failed intent=%s symbol=%s: %s",
            intent_id, asset.canonical, e,
        )

    return order


# ─────────────────────── adapter peek ───────────────────────

async def adapter_for_lane(lane: str, broker_override: Optional[str] = None):
    """Convenience used by gate code that wants to know if a broker is
    even connected for a given lane WITHOUT submitting anything.

    `broker_override` (2026-06-10): when supplied AND in
    `ROUTE_OVERRIDE_BROKERS`, the lookup uses that broker instead of
    the lane default. This mirrors the exact selection logic in
    `route_order` so a pre-trade gate (e.g. `broker_connected` in
    `execution._evaluate_gates`) sees the SAME broker the live path
    will use. Without this, an intent with `broker_override="webull"`
    would dry-run-block on the lane default's missing creds (e.g.
    no Public.com config) even though the actual route will use
    Webull. Any unknown / non-override broker name silently falls
    back to the lane default — same doctrine as `route_order`.

    2026-02-19: also consults the operator's `broker_selection`
    singleton when no per-intent override is set, so the
    `broker_connected` gate matches the live route resolution.

    Returns the adapter (truthy) or None.
    """
    override = (broker_override or "").strip().lower() or None
    if override and override in ROUTE_OVERRIDE_BROKERS:
        broker = override
    else:
        broker = None
        try:
            from routes.broker_selection import get_current_selection  # noqa: WPS433
            sel = await get_current_selection()
            sel_choice = (sel.get(lane) or "").strip().lower() or None
            if sel_choice and sel_choice in ADAPTER_LOADERS:
                broker = sel_choice
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "adapter_for_lane: broker_selection lookup failed lane=%s "
                "err=%s — falling back to lane default", lane, exc,
            )
        if not broker:
            try:
                broker = broker_for_lane(lane)
            except LaneRoutingError:
                return None
    loader = ADAPTER_LOADERS.get(broker)
    if not loader:
        return None
    return await loader()
