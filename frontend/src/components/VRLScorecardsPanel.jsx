import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import { ArrowsClockwise, ShieldCheck, ShieldWarning, Shield } from "@phosphor-icons/react";
import { toast } from "sonner";

/**
 * VRL Scorecards panel — per-gate precision / recall / accuracy over a
 * rolling window. Backs onto:
 *   GET   /api/admin/vrl/scorecards?latest_only=true
 *   POST  /api/admin/vrl/scorecards/recompute
 *   GET   /api/admin/vrl/scheduler/status
 *
 * The signature operator KPI is `net_protect_rate` (alias of precision):
 * of trades the gate BLOCKED, what fraction would have actually lost?
 * A gate with <40% precision over a meaningful sample is costing more
 * winning trades than it saves losing ones — surface that inline so the
 * operator can decide to retune or retire it.
 */
const RATE_TIERS = [
  // (threshold, color, label) — sorted high → low
  [0.70, "#22C55E", "EFFECTIVE"],
  [0.50, "#FBBF24", "MIXED"],
  [0.0,  "#EF4444", "FRICTION"],
];

function tierFor(rate) {
  if (rate == null) return { color: "#A1A1AA", label: "—" };
  for (const [threshold, color, label] of RATE_TIERS) {
    if (rate >= threshold) return { color, label };
  }
  return { color: "#A1A1AA", label: "—" };
}

const SORTS = [
  { key: "precision", label: "precision" },
  { key: "total", label: "sample" },
  { key: "recall", label: "recall" },
  { key: "accuracy", label: "accuracy" },
  { key: "gate_name", label: "name" },
];

export default function VRLScorecardsPanel() {
  const [rows, setRows] = useState(null);
  const [schedStatus, setSchedStatus] = useState(null);
  const [sortKey, setSortKey] = useState("precision");
  const [sortDir, setSortDir] = useState("asc");  // worst first
  const [busy, setBusy] = useState(false);
  const [windowH, setWindowH] = useState(720);

  const load = useCallback(async () => {
    try {
      const [r1, r2] = await Promise.all([
        api.get("/admin/vrl/scorecards", { params: { latest_only: true, limit: 100 } }),
        api.get("/admin/vrl/scheduler/status").catch(() => ({ data: null })),
      ]);
      setRows(r1.data.items || []);
      setSchedStatus(r2?.data);
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const recompute = async () => {
    setBusy(true);
    try {
      const { data } = await api.post("/admin/vrl/scorecards/recompute", { window_hours: windowH });
      const n = (data.scorecards || []).length;
      toast.success(`Recomputed · ${n} gates · ${data.intents_scored} intents`);
      await load();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  };

  const sorted = useMemo(() => {
    if (!rows) return null;
    const copy = [...rows];
    copy.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      // Nulls go last regardless of direction.
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "string") {
        return sortDir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
      }
      return sortDir === "asc" ? av - bv : bv - av;
    });
    return copy;
  }, [rows, sortKey, sortDir]);

  const flipSort = (key) => {
    if (key === sortKey) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      // Sample defaults to high→low; rates default low→high (worst first).
      setSortDir(key === "total" ? "desc" : "asc");
    }
  };

  const summary = useMemo(() => {
    if (!rows || rows.length === 0) return null;
    const effective = rows.filter((r) => (r.precision ?? 0) >= 0.70).length;
    const mixed = rows.filter((r) => (r.precision ?? 0) >= 0.50 && (r.precision ?? 0) < 0.70).length;
    const friction = rows.filter((r) => (r.precision ?? 0) < 0.50).length;
    return { effective, mixed, friction, total: rows.length };
  }, [rows]);

  return (
    <Card className="p-0 overflow-hidden" testid="vrl-scorecards-panel">
      <div className="px-4 py-3 border-b border-rd-border flex items-baseline justify-between gap-3 flex-wrap">
        <div className="flex items-baseline gap-3">
          <ShieldCheck size={14} weight="bold" className="text-rd-text" />
          <span className="label-eyebrow">VRL · gate scorecards</span>
          {summary && (
            <span className="text-[10px] font-mono text-rd-dim">
              <span className="text-rd-success">{summary.effective} effective</span>
              {" · "}
              <span className="text-rd-warn">{summary.mixed} mixed</span>
              {" · "}
              <span className="text-rd-danger">{summary.friction} friction</span>
            </span>
          )}
        </div>
        <div className="flex items-baseline gap-3 flex-wrap">
          <label className="text-[10px] uppercase tracking-widest text-rd-dim flex items-baseline gap-1">
            window
            <input
              type="number"
              value={windowH}
              min={1}
              max={8760}
              onChange={(e) => setWindowH(parseInt(e.target.value || "720", 10))}
              className="bg-rd-bg3 border border-rd-border text-rd-text text-xs px-1 py-0.5 w-16 font-mono"
              data-testid="vrl-scorecards-window"
            />
            <span className="text-rd-dim">h</span>
          </label>
          <button
            type="button"
            onClick={recompute}
            disabled={busy}
            className="text-[10px] uppercase tracking-widest text-rd-dim hover:text-rd-text flex items-center gap-1 font-mono disabled:opacity-40"
            data-testid="vrl-scorecards-recompute"
          >
            <ArrowsClockwise size={10} weight="bold" /> recompute
          </button>
        </div>
      </div>

      {!rows && (
        <div className="px-4 py-6 text-[11px] font-mono text-rd-dim">loading…</div>
      )}

      {rows && rows.length === 0 && (
        <div className="px-4 py-6 text-[11px] font-mono text-rd-dim italic" data-testid="vrl-scorecards-empty">
          — no scorecards yet. recompute to seed —
        </div>
      )}

      {sorted && sorted.length > 0 && (
        <div className="overflow-x-auto">
          <table className="text-[11px] font-mono w-full" data-testid="vrl-scorecards-table">
            <thead>
              <tr className="text-rd-dim uppercase tracking-widest text-[10px]">
                {SORTS.map((s) => (
                  <th
                    key={s.key}
                    className="text-left py-2 px-3 cursor-pointer hover:text-rd-text"
                    onClick={() => flipSort(s.key)}
                    data-testid={`vrl-scorecards-sort-${s.key}`}
                  >
                    {s.label}
                    {sortKey === s.key && (
                      <span className="ml-1">{sortDir === "asc" ? "↑" : "↓"}</span>
                    )}
                  </th>
                ))}
                <th className="text-left py-2 px-3" title="TP / FP / TN / FN">
                  tp/fp/tn/fn
                </th>
                <th className="text-left py-2 px-3">verdict</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((r) => {
                const tier = tierFor(r.precision);
                return (
                  <tr
                    key={`${r.gate_name}-${r.window_end}`}
                    className="border-t border-rd-border"
                    data-testid={`vrl-scorecard-row-${r.gate_name}`}
                  >
                    <td className="py-1.5 px-3 text-rd-text">{r.gate_name}</td>
                    <td className="py-1.5 px-3 text-rd-text">{r.total}</td>
                    <td className="py-1.5 px-3" style={{ color: tier.color }}>
                      {r.precision == null ? "—" : `${(r.precision * 100).toFixed(1)}%`}
                    </td>
                    <td className="py-1.5 px-3 text-rd-text">
                      {r.recall == null ? "—" : `${(r.recall * 100).toFixed(1)}%`}
                    </td>
                    <td className="py-1.5 px-3 text-rd-text">
                      {r.accuracy == null ? "—" : `${(r.accuracy * 100).toFixed(1)}%`}
                    </td>
                    <td className="py-1.5 px-3 text-rd-dim">
                      {r.tp}/{r.fp}/{r.tn}/{r.fn}
                    </td>
                    <td className="py-1.5 px-3">
                      <Badge color={tier.color}>{tier.label}</Badge>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="px-4 py-2.5 bg-rd-bg2 border-t border-rd-border text-[10px] font-mono text-rd-dim flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3 flex-wrap">
          <span className="uppercase tracking-widest">scheduler</span>
          {schedStatus ? (
            <>
              <Badge color={schedStatus.running ? "#22C55E" : (schedStatus.enabled ? "#FBBF24" : "#A1A1AA")}>
                {schedStatus.running ? "RUNNING" : (schedStatus.enabled ? "ENABLED" : "DISABLED")}
              </Badge>
              <span>
                every <span className="text-rd-text">{schedStatus.interval_hours}h</span>
                {" · "}
                rolling <span className="text-rd-text">{schedStatus.window_hours}h</span>
                {schedStatus.last_scheduled_run_at && (
                  <>
                    {" · "}last <span className="text-rd-text">{schedStatus.last_scheduled_run_at.slice(0, 19).replace("T", " ")}</span>
                  </>
                )}
              </span>
            </>
          ) : (
            <span>—</span>
          )}
        </div>
        <span className="uppercase tracking-widest text-right">
          Precision = blocks that prevented losses ÷ total blocks
        </span>
      </div>
    </Card>
  );
}
