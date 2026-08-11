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
  const [slicer, setSlicer] = useState(null);
  const [slicerBusy, setSlicerBusy] = useState(false);
  const [dd, setDd] = useState(null);
  const [ddBusy, setDdBusy] = useState(false);
  const [ew, setEw] = useState(null);

  const loadDd = async () => {
    setDdBusy(true);
    try {
      const { data: d } = await api.get("/admin/entry-mode/drawdown-autopsy?lane=crypto");
      setDd(d);
    } catch (e) {
      setDd({ error: e?.response?.data?.detail || e.message });
    } finally { setDdBusy(false); }
  };

  const toggleEw = async () => {
    try {
      const { data: d } = await api.post("/admin/entry-mode/edge-weight", { enabled: !(ew?.config?.enabled ?? true) });
      setEw((p) => ({ ...p, config: d.config }));
    } catch { /* silent */ }
  };

  const loadSlicer = async () => {
    setSlicerBusy(true);
    try {
      const { data: s } = await api.get("/admin/entry-mode/edge-slicer");
      setSlicer(s);
    } catch (e) {
      setSlicer({ error: e?.response?.data?.detail || e.message });
    } finally { setSlicerBusy(false); }
  };

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
    api.get("/admin/entry-mode/promotion-gate-v2").then(({ data: g }) => setGate(g)).catch(() => {});
    api.get("/admin/entry-mode/edge-weight").then(({ data: w }) => setEw(w)).catch(() => {});
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

  const beginEpoch = async () => {
    const reason = window.prompt("New evaluation epoch — reason (material execution change, e.g. 'maker execution ladder enabled'):");
    if (!reason || reason.trim().length < 4) return;
    try {
      await api.post("/admin/entry-mode/epoch", { reason: reason.trim() });
      setMsg({ ok: true, text: "new evaluation epoch begun — readiness now measures the current build" });
      load();
    } catch (e) {
      setMsg({ ok: false, text: String(e?.response?.data?.detail || e.message) });
    }
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
        <button
          onClick={loadDd}
          disabled={ddBusy}
          className="px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-amber-500/60 text-amber-500 hover:bg-amber-500/10 transition-colors disabled:opacity-40"
          data-testid="drawdown-autopsy-btn"
        >
          {ddBusy ? "dissecting…" : "explain the drawdown"}
        </button>
        <button
          onClick={loadSlicer}
          disabled={slicerBusy}
          className="px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-emerald-500/60 text-emerald-500 hover:bg-emerald-500/10 transition-colors disabled:opacity-40"
          data-testid="edge-slicer-btn"
        >
          {slicerBusy ? "slicing…" : "where does edge hide?"}
        </button>
      </div>
      {slicer && (
        <div className="border border-rd-border bg-rd-bg px-2.5 py-2 mb-2" data-testid="edge-slicer-result">
          {slicer.error ? (
            <div className="text-[10px] font-mono text-red-500">{slicer.error}</div>
          ) : (
            <>
              <div className={`text-[10px] font-mono font-bold mb-1 ${(slicer.cost_autopsy?.verdict || "").startsWith("POSITIVE") ? "text-rd-success" : (slicer.cost_autopsy?.verdict || "").startsWith("COSTS") ? "text-amber-500" : "text-red-500"}`} data-testid="edge-slicer-autopsy">
                {slicer.cost_autopsy?.verdict}
              </div>
              <div className="text-[10px] font-mono text-rd-dim mb-1.5">
                {slicer.scored_observations} scored · gross {slicer.cost_autopsy?.expectancy_gross}% → net {slicer.cost_autopsy?.expectancy_net}% at {slicer.cost_pct_assumed}% assumed costs · win rate {slicer.cost_autopsy?.win_rate}
              </div>
              {(slicer.cost_scenarios || []).length > 0 && (
                <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[10px] font-mono mb-1.5" data-testid="cost-scenarios">
                  <span className="text-[9px] uppercase tracking-widest text-rd-dim">cost scenarios:</span>
                  {slicer.cost_scenarios.map((s) => (
                    <span key={s.label} className={(s.expectancy_net ?? 0) > 0 ? "text-rd-success" : "text-red-500"}>
                      {s.label} ({s.cost_pct}%): {(s.expectancy_net ?? 0) > 0 ? "+" : ""}{s.expectancy_net}% · pf {s.profit_factor ?? "—"}
                    </span>
                  ))}
                </div>
              )}
              {(slicer.positive_slices || []).length > 0 ? (
                <div className="space-y-0.5" data-testid="edge-slicer-positive">
                  <div className="text-[9px] font-mono uppercase tracking-widest text-emerald-500 mb-0.5">slices with positive net edge</div>
                  {slicer.positive_slices.map((s, i) => (
                    <div key={i} className="flex flex-wrap gap-x-3 text-[10px] font-mono">
                      <span className="w-28 shrink-0 text-rd-dim">{s.dimension.replace("by_", "")}</span>
                      <span className="w-32 text-rd-text">{s.slice}</span>
                      <span className="text-rd-success">{s.expectancy_net > 0 ? "+" : ""}{s.expectancy_net}%</span>
                      <span className="text-rd-muted">×{s.n}</span>
                      <span className="text-rd-dim">pf {s.profit_factor ?? "—"} · wr {s.win_rate}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-[10px] font-mono text-red-500" data-testid="edge-slicer-none">
                  no slice with positive net edge found (min 30 obs, 20 for symbols) — the strategy loses across every dimension measured
                </div>
              )}
            </>
          )}
        </div>
      )}
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
              {funnel.ladder && (
                <div className="mt-1 text-[10px] font-mono" data-testid="ladder-stats">
                  <span className="text-rd-dim uppercase tracking-widest text-[9px]">execution ladder (7d): </span>
                  <span className="text-rd-success" data-testid="ladder-recovered-fills">recovered fills {funnel.ladder.recovered_fills}</span>
                  <span className="text-rd-dim"> · </span>
                  <span className={funnel.ladder.qualified_but_unexecuted > 0 ? "text-amber-500" : "text-rd-dim"} data-testid="qualified-but-unexecuted-count">
                    qualified-but-unexecuted {funnel.ladder.qualified_but_unexecuted}
                  </span>
                  {(funnel.ladder.by_stage || []).length > 0 && (
                    <span className="text-rd-dim"> · {funnel.ladder.by_stage.map((s) => `${s.outcome === "filled" ? "fill" : "abandon"}@${s.stage}×${s.n}`).join(" ")}</span>
                  )}
                </div>
              )}
              {(funnel.top_blockers || []).length > 0 && (
                <div className="mt-1.5 text-[9px] font-mono text-rd-dim">
                  top blockers: {funnel.top_blockers.slice(0, 5).map((b) => `${b.reason} ×${b.n}`).join(" · ")}
                </div>
              )}
            </>
          )}
        </div>
      )}
      {dd && (
        <div className="border border-rd-border bg-rd-bg px-2.5 py-2 mb-2" data-testid="drawdown-autopsy-result">
          {dd.error ? (
            <div className="text-[10px] font-mono text-red-500">{dd.error}</div>
          ) : (
            <>
              {(dd.verdicts || []).map((v, i) => (
                <div key={i} className={`text-[10px] font-mono mb-1 ${i === 0 ? "text-rd-text font-bold" : "text-amber-500"}`}>{v}</div>
              ))}
              {dd.scenarios && (
                <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[10px] font-mono mb-1" data-testid="dd-scenarios">
                  {Object.entries(dd.scenarios).map(([k, s]) => (
                    <span key={k} className="text-rd-muted">
                      {k.replaceAll("_", " ")} ({s.cost_pct}%): dd/100 <span className={s.dd_per_100_obs <= 10 ? "text-rd-success" : "text-red-500"}>{s.dd_per_100_obs}</span> · ${s.max_dd_dollars_at_fixed_size} at $5 sizing · exp {s.expectancy_pct}%
                    </span>
                  ))}
                </div>
              )}
              {(dd.loss_contributions?.by_symbol || []).length > 0 && (
                <div className="text-[9px] font-mono text-rd-dim">
                  top loss contributors: {dd.loss_contributions.by_symbol.slice(0, 5).map((s) => `${s.slice} ${s.loss_pct_points} (${Math.round((s.share_of_losses || 0) * 100)}%)`).join(" · ")}
                </div>
              )}
              {dd.clustering && (
                <div className="text-[9px] font-mono text-rd-dim">
                  clustering: {dd.clustering.repeat_obs_within_60min_same_symbol} repeat obs within 60min ({Math.round((dd.clustering.repeat_share || 0) * 100)}%) · {dd.clustering.distinct_symbols} distinct symbols · top: {(dd.clustering.top_symbols_by_obs || []).slice(0, 4).map((s) => `${s.symbol}×${s.n}`).join(" ")}
                </div>
              )}
            </>
          )}
        </div>
      )}
      {ew && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] font-mono mb-2" data-testid="edge-weight-row">
          <span className="text-[9px] uppercase tracking-widest text-rd-dim">edge weight</span>
          <button
            onClick={toggleEw}
            className={`px-1.5 text-[9px] font-mono font-bold uppercase border transition-colors ${(ew.config?.enabled ?? true) ? "border-rd-success text-rd-success" : "border-red-500 text-red-500"}`}
            data-testid="edge-weight-toggle"
          >
            {(ew.config?.enabled ?? true) ? "ON" : "OFF"}
          </button>
          <span className="text-rd-dim">sizing only, never a gate · floor {ew.config?.floor}× · now {ew.current_receipt?.weight ?? "—"}×</span>
          {ew.current_receipt?.components && (
            <span className="text-rd-muted">
              {Object.entries(ew.current_receipt.components).map(([d, c]) => `${d}:${c.slice}${c.n ? ` ${c.expectancy_net > 0 ? "+" : ""}${c.expectancy_net}%×${c.n}` : " (no data)"}→${c.score}`).join(" · ")}
            </span>
          )}
        </div>
      )}
      {lanes.length > 0 && (
        <div data-testid="promotion-gate-summary">
          <div className="flex items-center gap-2 mb-1.5 flex-wrap">
            <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">promotion gate v2</span>
            <span className="text-[9px] font-mono text-rd-dim" data-testid="gate-v2-epoch">
              epoch: {gate?.epoch?.epoch_id === "default" ? "default (all history)" : `${gate?.epoch?.epoch_id} · ${gate?.epoch?.reason || ""}`}
            </span>
            <button
              onClick={beginEpoch}
              className="px-1.5 py-0.5 text-[9px] font-mono uppercase tracking-wider border border-rd-border text-rd-muted hover:text-rd-text hover:border-rd-text transition-colors"
              data-testid="begin-epoch-btn"
            >
              begin new epoch
            </button>
          </div>
          <div className="flex flex-wrap gap-4">
            {lanes.map(([lane, v]) => {
              const stateStyle = {
                PASS: "border-rd-success text-rd-success",
                NEAR_PASS: "border-teal-500 text-teal-400",
                NEEDS_RECALIBRATION: "border-amber-500 text-amber-500",
                FAIL: "border-red-500 text-red-500",
                HARD_STOP: "border-red-600 text-red-600 bg-red-600/10",
              }[v.state] || "border-rd-border text-rd-dim";
              const critStyle = (s) => ({
                PASS: "text-rd-success",
                NEAR_PASS: "text-teal-400",
                NEEDS_RECALIBRATION: "text-amber-500",
                FAIL: "text-red-500",
                HARD_STOP: "text-red-600 font-bold",
              }[s] || "text-rd-dim");
              return (
                <div key={lane} className="border border-rd-border px-2.5 py-1.5">
                  <div className="flex items-center gap-2 mb-1 flex-wrap">
                    <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">{lane}</span>
                    <span
                      className={`px-1.5 text-[9px] font-mono font-bold uppercase border ${stateStyle}`}
                      data-testid={`promotion-gate-${lane}`}
                    >
                      {(v.state || "?").replaceAll("_", " ")}
                    </span>
                    <span className="text-[9px] font-mono text-rd-dim">
                      {v.evaluated_observations}/{v.lifetime_observations} obs (epoch/lifetime)
                    </span>
                    {v.cost && (
                      <span className={`text-[9px] font-mono ${v.cost.source === "measured" ? "text-rd-success" : "text-rd-dim"}`} data-testid={`gate-v2-cost-${lane}`}>
                        cost: {v.cost.round_trip_cost_pct?.toFixed(3)}% {v.cost.source}{v.cost.source === "assumed" ? ` (${v.cost.eligible_fill_count}/${gate?.config?.min_measured_fills} fills to measured)` : ` (${v.cost.maker_fill_count}m/${v.cost.taker_fill_count}t)`}
                      </span>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                    {(v.criteria || []).map((c) => (
                      <span key={c.name} className={`text-[9px] font-mono ${critStyle(c.state)}`} title={c.explanation}>
                        {c.name.replaceAll("_", " ")} {c.actual ?? "—"} ({c.target}){c.state === "NEEDS_RECALIBRATION" ? " ⚠ recalibrate?" : ""}
                      </span>
                    ))}
                  </div>
                  {(v.recalibration_candidates || []).length > 0 && (
                    <div className="mt-1 text-[9px] font-mono text-amber-500" data-testid={`gate-v2-recal-${lane}`}>
                      NEEDS RECALIBRATION: positive-edge sample keeps missing {v.recalibration_candidates.join(", ")} by a large multiple — the target is suspect, not the strategy. Threshold changes stay operator-owned.
                    </div>
                  )}
                </div>
              );
            })}
          </div>
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
