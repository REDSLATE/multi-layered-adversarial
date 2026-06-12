import React, { useState } from "react";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { Target, Warning, CheckCircle, XCircle } from "@phosphor-icons/react";

/**
 * WebullOtocoTestPanel — operator-driven atomic OTOCO bracket
 * submitter. Fires `POST /api/admin/webull/otoco/test` which builds
 * a 3-leg combo (MARKET entry + LIMIT TP + STOP SL) and submits
 * atomically via Webull's v3 combo API.
 *
 * Doctrine (P1 Phase 2, 2026-02-19):
 *   * Whole shares only — Webull's combo API doesn't accept the
 *     AMOUNT entrust used by the $1-$10 fractional pilot.
 *   * Operator-driven so we observe Webull's lifecycle behavior
 *     before wiring this into the auto-router.
 *   * Geometry sanity (stop < entry < target for BUY) is enforced
 *     server-side. UI shows a live preview of the bracket so the
 *     operator can see the math.
 */
export default function WebullOtocoTestPanel() {
  const [symbol, setSymbol] = useState("");
  const [qty, setQty] = useState("1");
  const [side, setSide] = useState("BUY");
  const [targetPrice, setTargetPrice] = useState("");
  const [stopPrice, setStopPrice] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [lastResult, setLastResult] = useState(null);
  const [lastError, setLastError] = useState(null);

  const tpNum = Number(targetPrice);
  const slNum = Number(stopPrice);
  const qtyNum = Number(qty);
  const ready =
    symbol.trim().length >= 1 &&
    Number.isInteger(qtyNum) && qtyNum >= 1 &&
    Number.isFinite(tpNum) && tpNum > 0 &&
    Number.isFinite(slNum) && slNum > 0 &&
    (side === "BUY" ? slNum < tpNum : tpNum < slNum);

  const handleSubmit = async () => {
    if (!ready) return;
    const ok = window.confirm(
      `Fire atomic OTOCO bracket?\n\n` +
      `Symbol: ${symbol.toUpperCase()}\n` +
      `Side:   ${side}\n` +
      `Qty:    ${qtyNum} share(s)\n` +
      `TP:     $${tpNum.toFixed(2)} (LIMIT)\n` +
      `SL:     $${slNum.toFixed(2)} (STOP)\n\n` +
      `Webull will manage the OCO lifecycle — one child cancels the other.`,
    );
    if (!ok) return;
    setSubmitting(true);
    setLastResult(null);
    setLastError(null);
    try {
      const res = await api.post("/admin/webull/otoco/test", {
        symbol: symbol.toUpperCase(),
        qty: qtyNum,
        side,
        target_price: tpNum,
        stop_price: slNum,
        confirm: "execute-otoco",
      });
      setLastResult(res.data);
      toast.success(
        `OTOCO submitted · ${symbol.toUpperCase()} ${side} ${qtyNum} · master=${res.data?.combo?.master_broker_order_id}`,
      );
    } catch (e) {
      const detail = e?.response?.data?.detail || e.message;
      setLastError(typeof detail === "string" ? detail : JSON.stringify(detail));
      toast.error(typeof detail === "string" ? detail : "OTOCO submit failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="border border-rd-border bg-rd-bg2 p-3 space-y-3"
      data-testid="webull-otoco-panel"
    >
      <div className="flex items-center gap-2">
        <Target size={13} weight="bold" className="text-rd-accent" />
        <span className="text-[10px] font-mono uppercase tracking-widest text-rd-text">
          Webull Atomic OTOCO
        </span>
        <span className="text-[10px] font-mono text-rd-dim ml-2">
          whole-share only · operator-driven
        </span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
        <div className="md:col-span-1">
          <label className="text-[9px] font-mono uppercase text-rd-dim block">Symbol</label>
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
            placeholder="AAL"
            className="w-full bg-rd-bg border border-rd-border px-2 py-1 font-mono text-xs text-rd-text focus:outline-none focus:border-rd-accent"
            data-testid="otoco-symbol"
          />
        </div>
        <div>
          <label className="text-[9px] font-mono uppercase text-rd-dim block">Qty</label>
          <input
            type="number"
            min="1"
            step="1"
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            className="w-full bg-rd-bg border border-rd-border px-2 py-1 font-mono text-xs text-rd-text focus:outline-none focus:border-rd-accent"
            data-testid="otoco-qty"
          />
        </div>
        <div>
          <label className="text-[9px] font-mono uppercase text-rd-dim block">Side</label>
          <div className="grid grid-cols-2">
            {["BUY", "SELL"].map((s) => (
              <button
                key={s}
                onClick={() => setSide(s)}
                className={
                  "py-1 font-mono text-[10px] font-bold uppercase border " +
                  (side === s
                    ? s === "BUY"
                      ? "border-rd-success bg-rd-success/10 text-rd-success"
                      : "border-rd-danger bg-rd-danger/10 text-rd-danger"
                    : "border-rd-border bg-rd-bg text-rd-dim")
                }
                data-testid={`otoco-side-${s.toLowerCase()}`}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="text-[9px] font-mono uppercase text-rd-dim block">TP (limit)</label>
          <input
            type="number"
            step="0.01"
            value={targetPrice}
            onChange={(e) => setTargetPrice(e.target.value)}
            placeholder={side === "BUY" ? "above entry" : "below entry"}
            className="w-full bg-rd-bg border border-rd-border px-2 py-1 font-mono text-xs text-rd-text focus:outline-none focus:border-rd-accent"
            data-testid="otoco-tp"
          />
        </div>
        <div>
          <label className="text-[9px] font-mono uppercase text-rd-dim block">SL (stop)</label>
          <input
            type="number"
            step="0.01"
            value={stopPrice}
            onChange={(e) => setStopPrice(e.target.value)}
            placeholder={side === "BUY" ? "below entry" : "above entry"}
            className="w-full bg-rd-bg border border-rd-border px-2 py-1 font-mono text-xs text-rd-text focus:outline-none focus:border-rd-accent"
            data-testid="otoco-sl"
          />
        </div>
      </div>

      {/* Geometry warning */}
      {!ready && (symbol || targetPrice || stopPrice) && (
        <div className="flex items-start gap-1.5 text-[10px] font-mono text-rd-danger">
          <Warning size={11} weight="bold" className="mt-0.5 shrink-0" />
          {!Number.isInteger(qtyNum) || qtyNum < 1
            ? "qty must be an integer ≥ 1"
            : !Number.isFinite(tpNum) || tpNum <= 0
            ? "TP must be a positive number"
            : !Number.isFinite(slNum) || slNum <= 0
            ? "SL must be a positive number"
            : side === "BUY" && !(slNum < tpNum)
            ? "BUY: stop must be below TP"
            : side === "SELL" && !(tpNum < slNum)
            ? "SELL: TP must be below stop"
            : symbol.trim().length < 1
            ? "symbol required"
            : "incomplete"}
        </div>
      )}

      <div className="flex items-center justify-end gap-2">
        <button
          onClick={handleSubmit}
          disabled={!ready || submitting}
          className={
            "px-3 py-1.5 font-mono text-[11px] uppercase tracking-widest border flex items-center gap-1.5 " +
            (ready && !submitting
              ? "border-rd-accent bg-rd-accent/10 text-rd-accent hover:bg-rd-accent/20"
              : "border-rd-border bg-rd-bg text-rd-dim cursor-not-allowed")
          }
          data-testid="otoco-submit"
        >
          <Target size={11} weight="bold" />
          {submitting ? "Submitting…" : "Fire OTOCO"}
        </button>
      </div>

      {/* Results */}
      {lastResult && (
        <div
          className="border border-rd-success bg-rd-bg p-2 font-mono text-[10px] space-y-1"
          data-testid="otoco-result"
        >
          <div className="flex items-center gap-1.5 text-rd-success">
            <CheckCircle size={11} weight="bold" />
            <span className="uppercase tracking-widest">OTOCO accepted by Webull</span>
          </div>
          <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 mt-1">
            <span className="text-rd-dim">master_order_id</span>
            <span className="text-rd-text truncate">{lastResult.combo?.master_broker_order_id}</span>
            <span className="text-rd-dim">combo_id</span>
            <span className="text-rd-text truncate">{lastResult.combo?.combo_client_order_id}</span>
            <span className="text-rd-dim">tp_client_id</span>
            <span className="text-rd-text truncate">{lastResult.combo?.tp_client_order_id}</span>
            <span className="text-rd-dim">sl_client_id</span>
            <span className="text-rd-text truncate">{lastResult.combo?.sl_client_order_id}</span>
            <span className="text-rd-dim">entry_proxy</span>
            <span className="text-rd-text">${Number(lastResult.combo?.entry_proxy_price).toFixed(2)}</span>
            <span className="text-rd-dim">tp / sl</span>
            <span className="text-rd-text">
              ${Number(lastResult.combo?.tp_limit_price).toFixed(2)} /{" "}
              ${Number(lastResult.combo?.sl_stop_price).toFixed(2)}
            </span>
          </div>
        </div>
      )}
      {lastError && (
        <div
          className="border border-rd-danger bg-rd-bg p-2 font-mono text-[10px] text-rd-danger flex items-start gap-1.5"
          data-testid="otoco-error"
        >
          <XCircle size={11} weight="bold" className="mt-0.5 shrink-0" />
          <div className="flex-1">{lastError}</div>
        </div>
      )}
    </div>
  );
}
