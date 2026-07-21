import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Warning, ArrowCounterClockwise } from "@phosphor-icons/react";

/** Daily Budget — spent/cap gauge + RESET SPEND + cap knob.
 *  Wired to the live risk gate (shared/risk/check.py). */
export default function DailyBudgetTile() {
  const [data, setData] = useState(null);
  const [capInput, setCapInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data: d } = await api.get("/admin/risk/budget");
      setData(d);
      setCapInput(String(d.cap_daily_usd));
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const run = async (fn, okText) => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await fn();
      setMsg({ ok: true, text: okText(r) });
      await load();
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally {
      setBusy(false);
    }
  };

  const resetSpend = () => run(
    () => api.post("/admin/risk/budget/reset"),
    (r) => `spend tally reset — now $${r.data.spent_today_usd.toFixed(2)}`,
  );
  const saveCap = () => run(
    () => api.post("/admin/risk/budget/cap", { cap_daily_usd: Number(capInput) }),
    (r) => `daily cap set to $${r.data.cap_daily_usd}`,
  );

  if (!data) return null;

  const frac = Math.min(1, data.spent_today_usd / (data.cap_daily_usd || 1));
  const exhausted = data.remaining_usd <= 0.5;
  const hrs = Math.floor(data.resets_in_s / 3600);
  const mins = Math.floor((data.resets_in_s % 3600) / 60);

  return (
    <div className="border border-rd-border p-3 mb-5" data-testid="daily-budget-tile">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
          Daily Budget · risk gate
        </div>
        <div
          className="text-[10px] font-mono"
          style={{ color: exhausted ? "#EF4444" : "#10B981" }}
          data-testid="daily-budget-state"
        >
          {exhausted ? "EXHAUSTED — ALL INTENTS RISK_REJECTED" : `$${data.remaining_usd.toFixed(2)} remaining`}
        </div>
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <div className="text-sm font-mono text-rd-text" data-testid="daily-budget-spent">
          ${data.spent_today_usd.toFixed(2)}
          <span className="text-rd-dim"> / ${Number(data.cap_daily_usd).toFixed(0)}</span>
        </div>
        <div className="flex-1 min-w-[120px] h-1.5 bg-rd-border/40">
          <div
            className="h-full"
            style={{ width: `${frac * 100}%`, backgroundColor: exhausted ? "#EF4444" : frac > 0.8 ? "#F59E0B" : "#10B981" }}
          />
        </div>
        <span className="text-[10px] font-mono text-rd-dim" data-testid="daily-budget-countdown">
          auto-reset in {hrs}h {mins}m (UTC midnight)
        </span>
        <button
          onClick={resetSpend}
          disabled={busy}
          data-testid="daily-budget-reset-btn"
          className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-1 disabled:opacity-40 flex items-center gap-1"
        >
          <ArrowCounterClockwise size={10} />reset spend
        </button>
        <label className="text-[10px] font-mono text-rd-dim flex items-center gap-1">
          cap $
          <input
            value={capInput}
            onChange={(e) => setCapInput(e.target.value)}
            className="w-16 bg-transparent border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text focus:outline-none focus:border-rd-text"
            data-testid="daily-budget-cap-input"
          />
        </label>
        <button
          onClick={saveCap}
          disabled={busy}
          data-testid="daily-budget-cap-save"
          className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-1 disabled:opacity-40"
        >
          set cap
        </button>
      </div>

      {msg && (
        <div
          className="mt-2 px-2 py-1 text-[10px] font-mono border"
          style={{ color: msg.ok ? "#10B981" : "#EF4444", borderColor: msg.ok ? "#10B981" : "#EF4444" }}
          data-testid="daily-budget-msg"
        >
          {!msg.ok && <Warning size={10} className="inline mr-1" />}{msg.text}
        </div>
      )}

      <div className="text-[10px] text-rd-muted mt-2 font-mono leading-relaxed">
        Spend = executed notional since UTC midnight (or your last manual reset
        {data.last_manual_reset_at ? ` — last reset ${data.last_manual_reset_at.slice(5, 16).replace("T", " ")} by ${data.last_manual_reset_by}` : ""}).
        Cap {data.cap_source === "override" ? "is an operator override" : `from env default ($${data.cap_env_default})`} — set null via API to revert.
        When exhausted, EVERY intent is RISK_REJECTED until reset.
      </div>
    </div>
  );
}
