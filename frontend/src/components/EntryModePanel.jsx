import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Prohibit, Warning } from "@phosphor-icons/react";

const MODE_STYLE = {
  exit_only: "border-red-500 text-red-500",
  canary: "border-amber-500 text-amber-500",
  live: "border-rd-success text-rd-success",
};

/** Entry-mode governor — exit_only default per 2026-08-05 directive.
 *  Canary/live are gated behind the promotion gate. */
export const EntryModePanel = () => {
  const [data, setData] = useState(null);
  const [gate, setGate] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [funnel, setFunnel] = useState(null);
  const [funnelBusy, setFunnelBusy] = useState(false);

  const loadFunnel = async () => {
    setFunnelBusy(true);
    try {
      const { data: f } = await api.get("/admin/entry-mode/funnel");
      setFunnel(f);
    } catch (e) {
      setFunnel({ error: e?.response?.data?.detail || e.message });
    } finally { setFunnelBusy(false); }
  };

  const load = () => {
    api.get("/admin/entry-mode").then(({ data: d }) => setData(d)).catch(() => {});
    api.get("/admin/entry-mode/promotion-gate").then(({ data: g }) => setGate(g)).catch(() => {});
  };
  useEffect(() => { load(); }, []);

  const setMode = async (mode, override = false) => {
    if (mode !== "exit_only" && !window.confirm(
      `Switch entry mode to ${mode.toUpperCase()}? New automated entries will ${mode === "canary" ? "resume under the daily canary cap" : "fully resume"}.`,
    )) return;
    setBusy(true); setMsg(null);
    try {
      const { data: d } = await api.post("/admin/entry-mode", { mode, override });
      setData((p) => ({ ...p, config: d.config }));
      setMsg({ ok: true, text: `entry mode → ${d.config.mode}` });
    } catch (e) {
      const det = e?.response?.data?.detail;
      if (det?.error === "promotion_gate_not_met") {
        if (window.confirm("PROMOTION GATE NOT MET — forward-recorded expectancy is not yet positive. Force override? (audited)")) {
          return setMode(mode, true);
        }
        setMsg({ ok: false, text: "blocked: promotion gate not met" });
      } else {
        setMsg({ ok: false, text: typeof det === "string" ? det : String(e) });
      }
    } finally { setBusy(false); }
  };

  const cfg = data?.config;
  const lanes = Object.entries(gate?.per_lane || {});

  return (
    <div className={`border-2 p-3 mb-4 bg-rd-panel ${cfg?.mode === "exit_only" ? "border-red-500" : "border-rd-border"}`} data-testid="entry-mode-panel">
      <div className="flex items-center justify-between mb-1.5 flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Prohibit size={15} weight="bold" className="text-red-500" />
          <span className="text-xs font-mono font-bold uppercase tracking-widest text-rd-text">Entry Mode</span>
          <span
            className={`px-2 py-0.5 text-[11px] font-mono font-bold uppercase tracking-wider border ${MODE_STYLE[cfg?.mode] || "border-rd-border text-rd-dim"}`}
            data-testid="entry-mode-badge"
          >
            {cfg ? cfg.mode.replace("_", "-") : "…"}
          </span>
          {cfg?.mode === "exit_only" && (
            <span className="text-[10px] font-mono text-red-500">no new automated entries · exits + stops + manual + scanning stay live</span>
          )}
        </div>
        <div className="flex gap-2">
          {["exit_only", "canary", "live"].map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              disabled={busy || cfg?.mode === m}
              className={`px-2 py-1 text-[10px] font-mono uppercase tracking-wider border transition-colors disabled:opacity-40 ${
                cfg?.mode === m ? MODE_STYLE[m] : "border-rd-border text-rd-muted hover:text-rd-text hover:border-rd-text"
              }`}
              data-testid={`entry-mode-set-${m}`}
            >
              {m.replace("_", "-")}
            </button>
          ))}
        </div>
      </div>
      <div className="text-[10px] font-mono text-rd-dim mb-2 flex items-center justify-between flex-wrap gap-2">
        <span>Fully-gated entries blocked by exit-only are recorded as shadow fills ({data?.shadow_fills_total ?? 0} so far) and scored 4h later — that forward record is the only road back to canary/live.</span>
        <button
          onClick={loadFunnel}
          disabled={funnelBusy}
          className="px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-muted hover:text-rd-text hover:border-rd-text transition-colors disabled:opacity-40"
          data-testid="promotion-funnel-btn"
        >
          {funnelBusy ? "tracing…" : "why is the count stuck?"}
        </button>
      </div>
      {funnel && (
        <div className="border border-rd-border bg-rd-bg px-2.5 py-2 mb-2" data-testid="promotion-funnel-result">
          {funnel.error ? (
            <div className="text-[10px] font-mono text-red-500">{funnel.error}</div>
          ) : (
            <>
              <div className={`text-[10px] font-mono font-bold mb-1.5 ${funnel.verdict?.startsWith("FLOWING") ? "text-rd-success" : "text-red-500"}`} data-testid="promotion-funnel-verdict">
                {funnel.verdict}
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[10px] font-mono mb-1">
                <span className={funnel.master_switch_armed === false ? "text-red-500 font-bold" : "text-rd-dim"}>
                  master switch: {funnel.master_switch_armed == null ? "?" : funnel.master_switch_armed ? "ARMED" : "DISARMED"}
                </span>
                <span className="text-rd-dim">mode: {funnel.execution_mode}</span>
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[10px] font-mono" data-testid="promotion-funnel-stages">
                {Object.entries(funnel.funnel || {}).map(([k, v]) => (
                  <span key={k} className={v === 0 ? "text-amber-500" : "text-rd-muted"}>
                    {k.replaceAll("_", " ")}: <span className={v === 0 ? "font-bold" : "text-rd-text"}>{v}</span>
                  </span>
                ))}
              </div>
              {(funnel.top_blockers || []).length > 0 && (
                <div className="mt-1.5 text-[9px] font-mono text-rd-dim">
                  top blockers: {funnel.top_blockers.slice(0, 5).map((b) => `${b.reason} ×${b.n}`).join(" · ")}
                </div>
              )}
            </>
          )}
        </div>
      )}
      {lanes.length > 0 && (
        <div className="flex flex-wrap gap-4" data-testid="promotion-gate-summary">
          {lanes.map(([lane, v]) => (
            <div key={lane} className="border border-rd-border px-2.5 py-1.5">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">{lane}</span>
                <span
                  className={`px-1.5 text-[9px] font-mono font-bold uppercase border ${v.passed ? "border-rd-success text-rd-success" : "border-red-500 text-red-500"}`}
                  data-testid={`promotion-gate-${lane}`}
                >
                  {v.passed ? "GATE MET" : "GATE NOT MET"}
                </span>
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                {(v.criteria || []).map((c) => (
                  <span key={c.name} className={`text-[9px] font-mono ${c.pass ? "text-rd-success" : "text-red-500"}`}>
                    {c.name.replaceAll("_", " ")} {c.value ?? "—"} ({c.threshold})
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
      {msg && (
        <div className={`mt-1.5 text-[10px] font-mono flex items-center gap-1 ${msg.ok ? "text-rd-success" : "text-red-500"}`} data-testid="entry-mode-msg">
          {!msg.ok && <Warning size={10} weight="bold" />} {msg.text}
        </div>
      )}
    </div>
  );
};

export default EntryModePanel;
