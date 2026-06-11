import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Lightning, X, ArrowRight } from "@phosphor-icons/react";
import { toast } from "sonner";

/**
 * OperatorInjectIntent — admin-only console-free path to post an intent
 * AS the brain holding the executor seat for a given lane, and submit
 * it through the gate chain immediately.
 *
 * This is the manual "operator dip-buy" button. RedEye (or whoever
 * holds the crypto/equity executor seat) is the carrier, but the
 * action and notional are operator-typed. Doctrine intact: the intent
 * is still gate-evaluated, the seat-policy gate still has to pass.
 *
 * Workflow:
 *   1. Operator picks lane → component fetches current executor seat
 *      holder from /admin/seat-registry/diagnose
 *   2. Operator picks symbol, side, $ notional
 *   3. Confirm phrase typed live ("operator dip-buy") to avoid fat-finger
 *   4. POST /intents with stack=<holder>, action=<side>, ...
 *   5. POST /execution/submit with the new intent_id + notional
 *   6. Render the full response (filled / blocked + per-gate reasons)
 */
const CONFIRM_PHRASE = "operator dip-buy";

const SYMBOL_PRESETS = {
  crypto: ["CRYPTO:ETH-USD", "CRYPTO:BTC-USD", "CRYPTO:SOL-USD", "CRYPTO:LINK-USD"],
  equity: ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"],
};

export default function OperatorInjectIntent({ onSubmitted }) {
  const [open, setOpen] = useState(false);
  const [lane, setLane] = useState("crypto");
  const [symbol, setSymbol] = useState("CRYPTO:ETH-USD");
  const [side, setSide] = useState("BUY");
  const [notional, setNotional] = useState("10");
  const [confirm, setConfirm] = useState("");
  const [holder, setHolder] = useState(null);
  const [holderLoading, setHolderLoading] = useState(false);
  // Broker route override (2026-06-10). null = lane default
  // (Public.com for equity, Kraken for crypto). "webull" routes the
  // intent through the parallel Webull adapter — capped at $3-$10
  // per ticker by the backend's `evaluate_webull_order` gate.
  const [brokerOverride, setBrokerOverride] = useState(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);

  const loadHolder = async (laneArg) => {
    setHolderLoading(true);
    try {
      const r = await api.get("/admin/seat-registry/diagnose");
      const summary = r.data?.lane_executor_summary?.[laneArg];
      setHolder(summary || null);
    } catch (e) {
      setHolder(null);
    } finally {
      setHolderLoading(false);
    }
  };

  useEffect(() => {
    if (open) loadHolder(lane);
  }, [open, lane]);

  const reset = () => {
    setResult(null);
    setConfirm("");
    setNotional("10");
  };

  const close = () => {
    setOpen(false);
    reset();
  };

  const canFire =
    !busy &&
    confirm.trim().toLowerCase() === CONFIRM_PHRASE &&
    holder?.would_route_pass === true &&
    Number(notional) > 0 &&
    symbol.trim() !== "";

  // Strip the canonical prefix ("EQ:", "CRYPTO:", "CR:") before
  // sending. The persisted intent row stores BARE tickers and the
  // `symbol_in_universe` gate keys on the bare form. The backend
  // also strips defensively (broker_symbol_resolver._strip_canonical_prefix
  // / shared.intents.post_intent), so this is belt-and-braces — the
  // operator's visual workflow is unchanged.
  const _stripCanonicalPrefix = (s) => {
    if (!s) return s;
    const upper = String(s).trim().toUpperCase();
    for (const p of ["CRYPTO:", "EQUITY:", "EQ:", "CR:"]) {
      if (upper.startsWith(p)) return upper.slice(p.length);
    }
    return upper;
  };

  const fire = async () => {
    setBusy(true);
    setResult(null);
    try {
      const cleanSymbol = _stripCanonicalPrefix(symbol);
      // 1) Post the intent as the seat holder
      const intent = await api.post("/intents", {
        stack: holder.holder,
        symbol: cleanSymbol,
        action: side,
        lane,
        confidence: 0.9,
        rationale: `Operator-injected ${side} · $${notional} ${cleanSymbol}${brokerOverride ? ` via ${brokerOverride}` : ""}`,
        snapshot: { spread_bps: 5.0 },
        broker_override: brokerOverride,
      });
      const intent_id = intent.data?.intent_id;
      if (!intent_id) throw new Error("intent post returned no intent_id");

      // 2) Submit it through the gate chain
      const sub = await api.post("/execution/submit", {
        intent_id,
        order_notional_usd: Number(notional),
        confirm: "execute",
      });
      setResult({ ok: true, intent_id, ...sub.data });
      toast.success(`Order routed · ${sub.data?.order?.status || "submitted"}`);
      if (onSubmitted) onSubmitted();
    } catch (e) {
      const status = e?.response?.status;
      const detail = e?.response?.data?.detail;
      setResult({
        ok: false,
        status,
        detail,
        message: typeof detail === "string"
          ? detail
          : (detail?.blocked_by
              ? `${detail.blocked_by}: ${detail.reason}`
              : (e?.message || "submit failed")),
      });
      toast.error("Operator-inject blocked — see panel for details");
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        data-testid="operator-inject-open"
        className="px-3 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-warning text-rd-warning hover:bg-rd-warning/10"
        title="Operator-inject an intent under the current executor seat"
      >
        <Lightning size={10} weight="bold" className="inline mr-1" />
        operator inject
      </button>
    );
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
      data-testid="operator-inject-modal"
    >
      <div className="bg-rd-bg border border-rd-warning w-full max-w-lg p-5 font-mono text-[12px] space-y-4">
        <div className="flex items-center justify-between border-b border-rd-border pb-3">
          <div className="flex items-center gap-2 text-rd-warning">
            <Lightning size={14} weight="bold" />
            <span className="font-bold uppercase tracking-widest text-[11px]">
              Operator Inject Intent
            </span>
          </div>
          <button onClick={close} className="text-rd-dim hover:text-rd-text" data-testid="operator-inject-close">
            <X size={14} weight="bold" />
          </button>
        </div>

        {/* Lane */}
        <div className="flex items-center gap-2">
          <span className="text-rd-dim w-20">lane</span>
          <div className="flex gap-1">
            {["crypto", "equity"].map((l) => (
              <button
                key={l}
                onClick={() => { setLane(l); setSymbol(SYMBOL_PRESETS[l][0]); }}
                data-testid={`operator-inject-lane-${l}`}
                className={
                  "px-2 py-0.5 uppercase tracking-wider border " +
                  (lane === l
                    ? "border-rd-warning text-rd-warning"
                    : "border-rd-border text-rd-dim hover:text-rd-text")
                }
              >
                {l}
              </button>
            ))}
          </div>
        </div>

        {/* Seat-holder readout */}
        <div className="border border-rd-border bg-rd-bg2 px-3 py-2 space-y-1">
          <div className="text-rd-dim text-[10px] uppercase">posting as · seat executor</div>
          {holderLoading ? (
            <div className="text-rd-dim">loading…</div>
          ) : holder?.holder ? (
            <div className="text-rd-text">
              {holder.holder} <span className="text-rd-dim">({holder.executor_seat})</span>
            </div>
          ) : (
            <div className="text-rd-danger">no executor assigned for {lane} — assign one in QSS first</div>
          )}
        </div>

        {/* Symbol */}
        <div className="flex items-center gap-2">
          <span className="text-rd-dim w-20">symbol</span>
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            data-testid="operator-inject-symbol"
            className="flex-1 bg-rd-bg2 border border-rd-border px-2 py-1 text-rd-text"
            placeholder={lane === "crypto" ? "CRYPTO:ETH-USD" : "SPY"}
          />
        </div>
        <div className="flex flex-wrap gap-1 pl-22">
          {SYMBOL_PRESETS[lane].map((s) => (
            <button
              key={s}
              onClick={() => setSymbol(s)}
              data-testid={`operator-inject-preset-${s}`}
              className="px-1.5 py-0.5 text-[10px] border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text"
            >
              {s}
            </button>
          ))}
        </div>

        {/* Side */}
        <div className="flex items-center gap-2">
          <span className="text-rd-dim w-20">side</span>
          <div className="flex gap-1">
            {["BUY", "SELL"].map((s) => (
              <button
                key={s}
                onClick={() => setSide(s)}
                data-testid={`operator-inject-side-${s}`}
                className={
                  "px-3 py-0.5 uppercase tracking-wider border " +
                  (side === s
                    ? (s === "BUY"
                        ? "border-rd-success text-rd-success"
                        : "border-rd-danger text-rd-danger")
                    : "border-rd-border text-rd-dim hover:text-rd-text")
                }
              >
                {s}
              </button>
            ))}
          </div>
        </div>

        {/* Notional */}
        <div className="flex items-center gap-2">
          <span className="text-rd-dim w-20">notional</span>
          <span className="text-rd-dim">$</span>
          <input
            value={notional}
            onChange={(e) => setNotional(e.target.value.replace(/[^0-9.]/g, ""))}
            data-testid="operator-inject-notional"
            inputMode="decimal"
            className="flex-1 bg-rd-bg2 border border-rd-border px-2 py-1 text-rd-text"
            placeholder="10"
          />
        </div>

        {/* Broker route override — opt INTO a parallel broker without
            erasing the lane defaults. Default ("none") routes
            equity → Public.com and crypto → Kraken as before.
            Webull is the small-pilot route: $3-$10 per ticker, must
            be armed with WEBULL_ARMED=true on the backend. */}
        <div className="flex items-center gap-2">
          <span className="text-rd-dim w-20">route</span>
          <div className="flex gap-1">
            {[
              { key: null, label: "default" },
              { key: "webull", label: "Webull (live $3-$10)" },
            ].map(({ key, label }) => {
              const active = brokerOverride === key;
              return (
                <button
                  key={String(key)}
                  onClick={() => setBrokerOverride(key)}
                  data-testid={`operator-inject-broker-${key || "default"}`}
                  className={
                    "px-2 py-0.5 text-[11px] uppercase tracking-wider border " +
                    (active
                      ? "border-rd-warning text-rd-warning"
                      : "border-rd-border text-rd-dim hover:text-rd-text")
                  }
                >
                  {label}
                </button>
              );
            })}
          </div>
        </div>
        {brokerOverride === "webull" && (
          <div
            className="pl-22 text-[10px] text-rd-warning"
            data-testid="operator-inject-webull-hint"
          >
            ⚠ live Webull route · $3 ≤ notional ≤ $10 · requires
            <code className="mx-1 text-rd-text">WEBULL_ARMED=true</code>
            on the backend
          </div>
        )}

        {/* Confirm */}
        <div className="space-y-1">
          <div className="text-rd-dim text-[10px]">
            type <span className="text-rd-warning">{CONFIRM_PHRASE}</span> to enable fire
          </div>
          <input
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            data-testid="operator-inject-confirm"
            className="w-full bg-rd-bg2 border border-rd-border px-2 py-1 text-rd-text"
            placeholder="confirm phrase"
          />
        </div>

        {/* Fire */}
        <button
          onClick={fire}
          disabled={!canFire}
          data-testid="operator-inject-fire"
          className={
            "w-full py-2 font-bold uppercase tracking-widest border " +
            (canFire
              ? (side === "BUY"
                  ? "border-rd-success text-rd-success hover:bg-rd-success/10"
                  : "border-rd-danger text-rd-danger hover:bg-rd-danger/10")
              : "border-rd-border text-rd-dim cursor-not-allowed")
          }
        >
          <ArrowRight size={11} weight="bold" className="inline mr-2" />
          {busy ? "firing…" : `place ${side} · $${notional || "0"} ${symbol}`}
        </button>

        {/* Result panel */}
        {result && (
          <div
            className={
              "border px-3 py-2 space-y-1 max-h-64 overflow-auto " +
              (result.ok && result.order?.status === "filled"
                ? "border-rd-success bg-rd-success/5 text-rd-success"
                : result.ok
                  ? "border-rd-warning bg-rd-warning/5 text-rd-warning"
                  : "border-rd-danger bg-rd-danger/5 text-rd-danger")
            }
            data-testid="operator-inject-result"
          >
            <div className="font-bold uppercase tracking-wider text-[10px]">
              {result.ok
                ? (result.order?.status || "SUBMITTED")
                : `BLOCKED · HTTP ${result.status || "?"}`}
            </div>
            {result.ok ? (
              <>
                {result.order?.broker_order_id && (
                  <div className="text-rd-text">broker_id: {result.order.broker_order_id}</div>
                )}
                {result.order?.txid && (
                  <div className="text-rd-text">txid: {result.order.txid}</div>
                )}
                {result.order?.filled_qty != null && (
                  <div className="text-rd-text">
                    filled: {result.order.filled_qty} @ {result.order.avg_fill_price}
                  </div>
                )}
                <details className="mt-1">
                  <summary className="cursor-pointer text-rd-dim text-[10px]">raw</summary>
                  <pre className="text-[10px] mt-1 text-rd-dim">{JSON.stringify(result, null, 2)}</pre>
                </details>
              </>
            ) : (
              <>
                <div className="text-rd-text">{result.message}</div>
                {Array.isArray(result.detail?.gates) && (
                  <div className="border border-rd-border bg-rd-bg2 mt-2">
                    {result.detail.gates
                      .filter((g) => g.passed === false)
                      .map((g) => (
                        <div key={g.name} className="px-2 py-1 border-b border-rd-border last:border-b-0">
                          <div className="text-rd-danger">{g.name}</div>
                          <div className="text-[10px] text-rd-dim">{g.reason}</div>
                        </div>
                      ))}
                  </div>
                )}
              </>
            )}
            <button
              onClick={reset}
              className="mt-2 text-[10px] text-rd-dim hover:text-rd-text underline"
            >
              clear
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
