import React, { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { Crosshair } from "@phosphor-icons/react";

const KNOBS = [
  { key: "stage_wait_s", scope: "ladder", label: "stage wait (s)", step: 1 },
  { key: "poll_s", scope: "ladder", label: "poll (s)", step: 0.5 },
  { key: "adaptive_spread_frac", scope: "ladder", label: "adaptive spread frac", step: 0.05 },
  { key: "max_chase_bps", scope: "ladder", label: "chase cap (bps)", step: 10 },
  { key: "max_spread_bps", scope: "spread", label: "ladder trigger (bps)", step: 5 },
  { key: "hard_reject_spread_bps", scope: "spread", label: "hard reject (bps)", step: 25 },
];

/** Execution Recovery Ladder — live knobs + real-time hunt toasts. */
export const LadderControlPanel = () => {
  const [cfg, setCfg] = useState(null);
  const [form, setForm] = useState({});
  const [busy, setBusy] = useState(false);
  const [activity, setActivity] = useState(null);
  const seenHunts = useRef(new Set());
  const seenEvents = useRef(null); // null until first poll baselines

  const applyCfg = (d) => {
    setCfg(d);
    const flat = {};
    KNOBS.forEach((k) => { flat[k.key] = d?.[k.scope]?.[k.key]; });
    setForm(flat);
  };

  useEffect(() => {
    api.get("/admin/execution-ladder/config").then(({ data }) => applyCfg(data)).catch(() => {});
  }, []);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const { data } = await api.get("/admin/execution-ladder/activity");
        if (!alive) return;
        setActivity(data);
        (data.active || []).forEach((h) => {
          const key = `${h.intent_id}:${h.stage}`;
          if (!seenHunts.current.has(key)) {
            seenHunts.current.add(key);
            toast.info(`Ladder hunting ${h.symbol} — ${String(h.stage).replaceAll("_", " ")} @ ${h.limit_price}`, { duration: 8000 });
          }
        });
        const evts = data.recent || [];
        if (seenEvents.current === null) {
          seenEvents.current = new Set(evts.map((e) => `${e.intent_id}:${e.ts}`));
        } else {
          evts.forEach((e) => {
            const key = `${e.intent_id}:${e.ts}`;
            if (seenEvents.current.has(key)) return;
            seenEvents.current.add(key);
            if (e.outcome === "filled") {
              toast.success(`Ladder FILLED ${e.symbol} at ${String(e.final_stage).replaceAll("_", " ")}`);
            } else {
              toast.warning(`Ladder abandoned ${e.symbol} — qualified but unexecuted (${String(e.final_stage).replaceAll("_", " ")})`);
            }
          });
        }
      } catch { /* silent */ }
    };
    poll();
    const t = setInterval(poll, 8000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const save = async () => {
    setBusy(true);
    try {
      const body = {};
      KNOBS.forEach((k) => {
        const v = parseFloat(form[k.key]);
        if (!Number.isNaN(v)) body[k.key] = v;
      });
      body.enabled = cfg?.ladder?.enabled ?? true;
      const { data } = await api.post("/admin/execution-ladder/config", body);
      applyCfg(data);
      toast.success("Ladder knobs applied — live, no redeploy needed");
    } catch (e) {
      toast.error(String(e?.response?.data?.detail || e.message));
    } finally { setBusy(false); }
  };

  const toggle = async () => {
    setBusy(true);
    try {
      const { data } = await api.post("/admin/execution-ladder/config", { enabled: !cfg?.ladder?.enabled });
      applyCfg(data);
      toast.success(`Execution ladder ${data.ladder.enabled ? "ENABLED" : "DISABLED"}`);
    } catch (e) {
      toast.error(String(e?.response?.data?.detail || e.message));
    } finally { setBusy(false); }
  };

  const active = activity?.active || [];

  return (
    <div className="border border-rd-border p-3 mb-4 bg-rd-panel" data-testid="ladder-control-panel">
      <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
        <div className="flex items-center gap-2">
          <Crosshair size={15} weight="bold" className="text-emerald-500" />
          <span className="text-xs font-mono font-bold uppercase tracking-widest text-rd-text">Execution Ladder</span>
          <button
            onClick={toggle}
            disabled={busy || !cfg}
            className={`px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider border transition-colors disabled:opacity-40 ${
              cfg?.ladder?.enabled ? "border-rd-success text-rd-success" : "border-red-500 text-red-500"
            }`}
            data-testid="ladder-enabled-toggle"
          >
            {cfg ? (cfg.ladder.enabled ? "ON" : "OFF") : "…"}
          </button>
          {active.length > 0 && (
            <span className="px-2 py-0.5 text-[10px] font-mono font-bold uppercase border border-amber-500 text-amber-500 animate-pulse" data-testid="ladder-hunting-badge">
              hunting ×{active.length}
            </span>
          )}
        </div>
        <button
          onClick={save}
          disabled={busy || !cfg}
          className="px-2.5 py-1 text-[10px] font-mono uppercase tracking-wider border border-emerald-500/60 text-emerald-500 hover:bg-emerald-500/10 transition-colors disabled:opacity-40"
          data-testid="ladder-save-btn"
        >
          {busy ? "applying…" : "apply live"}
        </button>
      </div>
      <div className="text-[10px] font-mono text-rd-dim mb-2">
        Wide spreads hunt fills (maker → adaptive → capped limit) instead of returning to HOLD. Knobs apply instantly — no redeploy.
      </div>
      <div className="flex flex-wrap gap-3" data-testid="ladder-knobs">
        {KNOBS.map((k) => (
          <label key={k.key} className="flex flex-col gap-0.5">
            <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">{k.label}</span>
            <input
              type="number"
              step={k.step}
              value={form[k.key] ?? ""}
              onChange={(e) => setForm((p) => ({ ...p, [k.key]: e.target.value }))}
              className="w-28 bg-rd-bg border border-rd-border px-2 py-1 text-[11px] font-mono text-rd-text focus:border-emerald-500 outline-none"
              data-testid={`ladder-knob-${k.key}`}
            />
          </label>
        ))}
      </div>
      {active.length > 0 && (
        <div className="mt-2 space-y-0.5" data-testid="ladder-active-hunts">
          {active.map((h) => (
            <div key={h.intent_id} className="text-[10px] font-mono text-amber-500">
              ▸ {h.symbol} · {String(h.stage).replaceAll("_", " ")} · limit {h.limit_price} · ${h.notional_usd}
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default LadderControlPanel;
