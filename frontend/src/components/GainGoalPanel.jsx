import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Target, Warning } from "@phosphor-icons/react";

const STATUS_STYLE = {
  NO_GOAL: "text-rd-dim border-rd-border/60",
  INSUFFICIENT_SAMPLE: "text-amber-400 border-amber-400/40",
  ON_PACE: "text-sky-400 border-sky-400/40",
  AHEAD_OF_PACE: "text-emerald-500 border-emerald-500/40",
  BEHIND_PACE: "text-amber-400 border-amber-400/40",
  GOAL_REACHED: "text-emerald-500 border-emerald-500/60",
  DRAWDOWN_WARNING: "text-orange-400 border-orange-400/50",
  DRAWDOWN_BREACHED: "text-rd-danger border-rd-danger/60",
  WINDOW_COMPLETE: "text-rd-dim border-rd-border",
};

const usd = (v, plus = true) =>
  v == null ? "—" : `${plus && v > 0 ? "+" : ""}$${Number(v).toFixed(2)}`;
const pct = (v) => (v == null ? "—" : `${Number(v).toFixed(0)}%`);
const pnlCls = (v) =>
  v == null ? "text-rd-dim" : v >= 0 ? "text-emerald-500" : "text-rd-danger";

function Row({ label, value, cls, tid, dim }) {
  return (
    <div className="flex justify-between text-[10px] font-mono" data-testid={tid}>
      <span className={`uppercase tracking-widest ${dim ? "text-rd-dim/60" : "text-rd-dim"}`}>{label}</span>
      <span className={cls || "text-rd-text"}>{value}</span>
    </div>
  );
}

function LaneGoalConfig({ lane, cfg, busy, onSave }) {
  const c = cfg?.[lane] || {};
  const d = cfg?.defaults || {};
  const [tgt, setTgt] = useState(c.target_net_pnl_usd);
  const [tgtPct, setTgtPct] = useState(c.target_return_pct);
  const [ddUsd, setDdUsd] = useState(c.maximum_window_drawdown_usd);
  const [win, setWin] = useState(c.window_type || d.window_type || "calendar_month");
  useEffect(() => {
    setTgt(c.target_net_pnl_usd); setTgtPct(c.target_return_pct);
    setDdUsd(c.maximum_window_drawdown_usd);
    setWin(c.window_type || d.window_type || "calendar_month");
  }, [c.target_net_pnl_usd, c.target_return_pct, c.maximum_window_drawdown_usd, c.window_type, d.window_type]);
  const num = (v) => (v === "" || v == null ? null : Number(v));
  const inp = "w-14 bg-transparent border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text focus:outline-none focus:border-rd-text";
  return (
    <div className="flex flex-wrap items-center gap-2 text-[10px] font-mono text-rd-dim pt-1" data-testid={`gain-goal-cfg-${lane}`}>
      <label className="flex items-center gap-1">goal $
        <input value={tgt ?? ""} onChange={(e) => setTgt(e.target.value)} className={inp}
          data-testid={`gain-goal-target-usd-${lane}`} />
      </label>
      <label className="flex items-center gap-1">goal %
        <input value={tgtPct ?? ""} onChange={(e) => setTgtPct(e.target.value)} className={inp}
          data-testid={`gain-goal-target-pct-${lane}`} />
      </label>
      <label className="flex items-center gap-1">max dd $
        <input value={ddUsd ?? ""} onChange={(e) => setDdUsd(e.target.value)} className={inp}
          data-testid={`gain-goal-dd-${lane}`} />
      </label>
      <select value={win} onChange={(e) => setWin(e.target.value)}
        className="bg-rd-bg border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text"
        data-testid={`gain-goal-window-${lane}`}>
        <option value="calendar_month">month</option>
        <option value="calendar_week">week</option>
        <option value="rolling_days">rolling 30d</option>
      </select>
      <button disabled={busy} data-testid={`gain-goal-save-${lane}`}
        onClick={() => onSave(lane, {
          target_net_pnl_usd: num(tgt), target_return_pct: num(tgtPct),
          maximum_window_drawdown_usd: num(ddUsd), window_type: win,
        })}
        className="text-[9px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-0.5 disabled:opacity-40">
        save
      </button>
    </div>
  );
}

function LaneGoalCard({ ev, cfg, busy, onSave, onAck }) {
  if (!ev) return null;
  const tp = ev.time_progress || {};
  const sessionLabel = tp.mode === "rth_sessions"
    ? `${tp.completed_sessions}/${tp.total_sessions} sessions`
    : tp.mode === "rolling" ? "rolling" : `${tp.elapsed_hours}/${tp.total_hours}h`;
  return (
    <div className="border border-rd-border/60 p-2 space-y-1 flex-1 min-w-[260px]"
      data-testid={`gain-goal-lane-${ev.lane}`}>
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-mono uppercase tracking-widest text-rd-text">
          {ev.lane} {ev.session_scope === "RTH" ? "RTH" : ""} — {ev.label}
        </span>
        <span className={`text-[9px] font-mono uppercase tracking-widest border px-1.5 py-0.5 ${STATUS_STYLE[ev.status] || "text-rd-dim border-rd-border"}`}
          data-testid={`gain-goal-status-${ev.lane}`}>
          {ev.status.replaceAll("_", " ")}
        </span>
      </div>
      <Row label="Goal" tid={`gain-goal-target-${ev.lane}`}
        value={ev.effective_target_usd != null ? usd(ev.effective_target_usd) : "not set"} />
      <Row label="Net realized" value={usd(ev.net_realized_pnl_usd)}
        cls={pnlCls(ev.net_realized_pnl_usd)} tid={`gain-goal-net-${ev.lane}`} />
      {ev.return_on_deployed_pct != null && (
        <Row label="Return on deployed" dim
          value={`${ev.return_on_deployed_pct.toFixed(2)}%`}
          cls={pnlCls(ev.return_on_deployed_pct)} tid={`gain-goal-return-${ev.lane}`} />
      )}
      <Row label="Goal progress" value={pct(ev.goal_progress_pct)} tid={`gain-goal-progress-${ev.lane}`} />
      <Row label={tp.mode === "rth_sessions" ? "Session progress" : "Time progress"}
        value={`${pct((tp.fraction ?? 0) * 100)} · ${sessionLabel}`}
        tid={`gain-goal-time-${ev.lane}`} />
      <Row label="Pace" tid={`gain-goal-pace-${ev.lane}`}
        value={ev.pace_variance_usd == null ? "—"
          : `${ev.pace_variance_usd >= 0 ? "Ahead" : "Behind"} by $${Math.abs(ev.pace_variance_usd).toFixed(2)}`}
        cls={pnlCls(ev.pace_variance_usd)} />
      <Row label="Projected (est.)" dim value={usd(ev.projected_end_usd)}
        cls="text-rd-dim" tid={`gain-goal-projected-${ev.lane}`} />
      <Row label="Window drawdown" tid={`gain-goal-drawdown-${ev.lane}`}
        value={`-$${(ev.max_window_drawdown_usd ?? 0).toFixed(2)}${ev.drawdown_limit_usd != null ? ` / -$${ev.drawdown_limit_usd.toFixed(2)} max` : ""}`}
        cls={ev.drawdown_breached ? "text-rd-danger" : "text-rd-text"} />
      <Row label="Resolved trades"
        value={`${ev.resolved_trades} / ${ev.minimum_resolved_trades} min`}
        cls={ev.sample_sufficient ? "text-rd-text" : "text-amber-400"}
        tid={`gain-goal-trades-${ev.lane}`} />
      {ev.throttle_active_multiplier != null && (
        <Row label="Throttle active" value={`×${ev.throttle_active_multiplier}`}
          cls="text-sky-400" tid={`gain-goal-throttle-${ev.lane}`} />
      )}
      {ev.entries_blocked && (
        <div className="flex items-center justify-between gap-2 border border-rd-danger/50 px-2 py-1"
          data-testid={`gain-goal-breach-${ev.lane}`}>
          <span className="text-[9px] font-mono text-rd-danger flex items-center gap-1">
            <Warning size={11} weight="bold" /> ENTRIES BLOCKED — {ev.breach_latch?.reason}
          </span>
          <button onClick={() => onAck(ev.lane)} disabled={busy}
            data-testid={`gain-goal-ack-${ev.lane}`}
            className="text-[9px] font-mono uppercase tracking-widest border border-rd-danger text-rd-danger hover:bg-rd-danger/10 px-2 py-0.5 shrink-0 disabled:opacity-40">
            acknowledge
          </button>
        </div>
      )}
      <LaneGoalConfig lane={ev.lane} cfg={cfg} busy={busy} onSave={onSave} />
    </div>
  );
}

export default function GainGoalPanel() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data: r } = await api.get("/admin/gain-goals");
      setData(r);
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [load]);

  const saveLane = async (lane, fields) => {
    setBusy(true);
    try {
      const { data: r } = await api.post("/admin/gain-goals/config", { [lane]: fields });
      setData(r);
      setMsg({ ok: true, text: `${lane} goal saved` });
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally {
      setBusy(false);
    }
  };

  const ack = async (lane) => {
    setBusy(true);
    try {
      await api.post("/admin/gain-goals/ack", { lane });
      await load();
      setMsg({ ok: true, text: `${lane} breach acknowledged — entries resume` });
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally {
      setBusy(false);
    }
  };

  if (!data) return null;
  const lanes = data.lanes || {};
  const rollup = data.global_rollup || {};
  return (
    <div className="border border-rd-border p-3 space-y-2" data-testid="gain-goal-panel">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Target size={14} className="text-rd-dim" />
          <span className="text-[11px] font-mono uppercase tracking-widest text-rd-text">Gain Goal</span>
          <span className="text-[9px] font-mono text-rd-dim/70">
            measures progress · never chases the target
          </span>
        </div>
        <span className="text-[10px] font-mono text-rd-dim" data-testid="gain-goal-rollup">
          account rollup: <span className={pnlCls(rollup.net_realized_pnl_usd)}>{usd(rollup.net_realized_pnl_usd)}</span>
          {" "}· {rollup.resolved_trades} trades (read-only)
        </span>
      </div>
      {msg && (
        <div className={`text-[10px] font-mono ${msg.ok ? "text-emerald-500" : "text-rd-danger"}`}
          data-testid="gain-goal-msg">{msg.text}</div>
      )}
      <div className="flex flex-wrap gap-2">
        <LaneGoalCard ev={lanes.equity} cfg={data.config} busy={busy} onSave={saveLane} onAck={ack} />
        <LaneGoalCard ev={lanes.crypto} cfg={data.config} busy={busy} onSave={saveLane} onAck={ack} />
      </div>
    </div>
  );
}
