import React, { useEffect, useState, useCallback } from "react";
import { api, fmtTime, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";
import { LightningSlash, Lock, Trophy, Question } from "@phosphor-icons/react";

const BRAIN_META = {
  alpha:    { label: "ALPHA",    color: "#3B82F6" },
  camaro:   { label: "CAMARO",   color: "#F59E0B" },
  chevelle: { label: "CHEVELLE", color: "#10B981" },
  redeye:   { label: "REDEYE",   color: "#DC2626" },
};

const STATUS_META = {
  open:     { color: "#FBBF24", label: "OPEN" },
  resolved: { color: "#10B981", label: "RESOLVED" },
  stale:    { color: "#A1A1AA", label: "STALE" },
};

const PAIRS = [
  ["alpha", "redeye"],
  ["alpha", "camaro"],
  ["redeye", "camaro"],
  ["alpha", "chevelle"],
  ["redeye", "chevelle"],
  ["camaro", "chevelle"],
];

export default function Conflicts() {
  const [items, setItems] = useState(null);
  const [pairs, setPairs] = useState({});
  const [filter, setFilter] = useState({ status: "", runtime: "" });
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    setErr("");
    try {
      const params = new URLSearchParams();
      params.set("limit", "200");
      if (filter.status) params.set("status", filter.status);
      if (filter.runtime) params.set("runtime", filter.runtime);
      const [conflicts, ...pairResults] = await Promise.all([
        api.get(`/shared/conflicts?${params.toString()}`),
        ...PAIRS.map(([a, b]) =>
          api.get(`/shared/conflicts/pair-scorecard?a=${a}&b=${b}`)
        ),
      ]);
      setItems(conflicts.data.items);
      const pairMap = {};
      PAIRS.forEach((p, i) => { pairMap[p.join(":")] = pairResults[i].data; });
      setPairs(pairMap);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, [filter]);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    const id = setInterval(refresh, 10000);
    return () => clearInterval(id);
  }, [refresh]);

  async function manualResolve(cid, winner) {
    const note = window.prompt(`Resolve conflict in favour of ${winner.toUpperCase()}? Optional note:`, "") ?? "";
    try {
      await api.post(`/admin/conflicts/${cid}/resolve`, { winner, notes: note });
      refresh();
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }

  return (
    <div className="reveal" data-testid="conflicts-page">
      <PageHeader
        eyebrow="Step 4 · conflict memory"
        title="Conflicts"
        sub="Auto-flagged when two brains take opposing stances on the same topic. Resolved by attached outcomes (auto) or operator override (manual). Pair scorecards show who is right when contradicting whom."
        right={
          <div className="flex items-center gap-2">
            <Badge color="#FBBF24">
              <Lock size={11} weight="bold" className="inline mr-1 -mt-0.5" />
              ADMIN ONLY
            </Badge>
            <Badge color="#DC2626">NO EXECUTION</Badge>
          </div>
        }
        testid="conflicts-header"
      />

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono">
          {err}
        </div>
      )}

      {/* Pair scorecards */}
      <Card className="mb-4" testid="pair-scorecards">
        <div className="label-eyebrow mb-3">Pair scorecards · who is right when contradicting whom</div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2">
          {PAIRS.map(([a, b]) => {
            const p = pairs[`${a}:${b}`];
            if (!p) return null;
            const ma = BRAIN_META[a]; const mb = BRAIN_META[b];
            const decisive = p.decisive || 0;
            return (
              <div key={`${a}:${b}`} className="border border-rd-border p-3" data-testid={`pair-${a}-${b}`}>
                <div className="flex items-center gap-2 text-xs font-mono mb-2">
                  <span style={{ color: ma.color }} className="font-bold">{ma.label}</span>
                  <span className="text-rd-dim">vs</span>
                  <span style={{ color: mb.color }} className="font-bold">{mb.label}</span>
                  <span className="ml-auto text-[10px] text-rd-dim uppercase tracking-widest">
                    decisive · {decisive}
                  </span>
                </div>
                {decisive === 0 ? (
                  <div className="text-[10px] text-rd-dim font-mono">no resolved disagreements yet</div>
                ) : (
                  <div className="space-y-1">
                    <Bar label={ma.label} pct={p.a_win_rate} color={ma.color} count={p.a_wins} />
                    <Bar label={mb.label} pct={p.b_win_rate} color={mb.color} count={p.b_wins} />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Card>

      {/* Filters */}
      <Card className="mb-4" testid="conflict-filters">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
          <div>
            <div className="label-eyebrow mb-1">Status</div>
            <select
              value={filter.status}
              onChange={(e) => setFilter((f) => ({ ...f, status: e.target.value }))}
              className="bg-rd-bg2 border border-rd-border text-rd-text font-mono text-xs px-3 py-2 w-full"
              data-testid="conflict-filter-status"
            >
              <option value="">all</option>
              <option value="open">open</option>
              <option value="resolved">resolved</option>
              <option value="stale">stale</option>
            </select>
          </div>
          <div>
            <div className="label-eyebrow mb-1">Brain</div>
            <select
              value={filter.runtime}
              onChange={(e) => setFilter((f) => ({ ...f, runtime: e.target.value }))}
              className="bg-rd-bg2 border border-rd-border text-rd-text font-mono text-xs px-3 py-2 w-full"
              data-testid="conflict-filter-runtime"
            >
              <option value="">any</option>
              {Object.keys(BRAIN_META).map((k) => (
                <option key={k} value={k}>{BRAIN_META[k].label}</option>
              ))}
            </select>
          </div>
          <button
            onClick={() => setFilter({ status: "", runtime: "" })}
            className="btn-sharp px-3 py-2 border border-rd-border text-rd-muted hover:text-rd-text"
            data-testid="conflict-filter-clear"
          >
            clear filters
          </button>
        </div>
      </Card>

      {!items && <LoadingRow />}
      {items && items.length === 0 && (
        <EmptyState message="No conflicts match these filters." testid="conflicts-empty" />
      )}

      {items && items.length > 0 && (
        <div className="space-y-3" data-testid="conflicts-list">
          {items.map((c) => (
            <ConflictCard key={c.conflict_id} c={c} onManualResolve={manualResolve} />
          ))}
        </div>
      )}
    </div>
  );
}

function Bar({ label, pct, color, count }) {
  const w = pct == null ? 0 : pct * 100;
  return (
    <div className="flex items-center gap-2 text-[10px] font-mono">
      <span className="text-rd-muted w-16" style={{ color }}>{label}</span>
      <div className="flex-1 bg-rd-bg2 h-2 relative overflow-hidden border border-rd-border">
        <div className="absolute inset-y-0 left-0" style={{ width: `${w}%`, background: color }} />
      </div>
      <span className="text-rd-text w-12 text-right">
        {pct == null ? "—" : `${(pct * 100).toFixed(0)}%`}
      </span>
      <span className="text-rd-dim w-8 text-right">{count}</span>
    </div>
  );
}

function ConflictCard({ c, onManualResolve }) {
  const status = STATUS_META[c.status] || STATUS_META.open;
  const winnerMeta = c.winner ? BRAIN_META[c.winner] : null;
  return (
    <Card className="p-0 overflow-hidden" testid={`conflict-${c.conflict_id}`}>
      <div className="px-4 py-2.5 border-b border-rd-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <LightningSlash size={14} weight="bold" className="text-rd-warn" />
          <span className="font-mono text-xs text-rd-muted">topic ·</span>
          <span className="font-mono text-xs text-rd-text">{c.topic}</span>
        </div>
        <div className="flex items-center gap-2">
          <Badge color={status.color}>{status.label}</Badge>
          {winnerMeta && (
            <span className="flex items-center gap-1 text-[11px] font-mono">
              <Trophy size={11} weight="bold" style={{ color: winnerMeta.color }} />
              <span style={{ color: winnerMeta.color }} className="font-bold">{winnerMeta.label}</span>
            </span>
          )}
          <span className="text-[10px] text-rd-dim uppercase tracking-widest">
            {relTime(c.detected_at)}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 divide-y md:divide-y-0 md:divide-x divide-rd-border">
        {c.participants.map((p) => {
          const meta = BRAIN_META[p.runtime] || { label: p.runtime, color: "#A1A1AA" };
          const isWinner = c.winner === p.runtime;
          return (
            <div
              key={p.opinion_id}
              className="px-4 py-3"
              style={{ borderLeft: isWinner ? `3px solid ${meta.color}` : undefined }}
              data-testid={`participant-${p.runtime}`}
            >
              <div className="flex items-baseline gap-2 mb-1">
                <span style={{ color: meta.color }} className="font-mono font-bold text-xs">
                  {meta.label}
                </span>
                <Badge color={meta.color}>{p.stance}</Badge>
                <span className="text-[10px] text-rd-dim font-mono ml-auto">
                  conf {Number(p.confidence).toFixed(2)}
                </span>
              </div>
              <div className="text-[10px] text-rd-dim font-mono">
                {fmtTime(p.posted_at)}
              </div>
            </div>
          );
        })}
      </div>

      {c.status === "open" && (
        <div className="px-4 py-2.5 bg-rd-bg2 border-t border-rd-border flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-2 text-[10px] text-rd-dim uppercase tracking-widest">
            <Question size={11} weight="bold" />
            awaiting outcomes — or operator override:
          </div>
          <div className="flex items-center gap-2">
            {c.participants.map((p) => {
              const meta = BRAIN_META[p.runtime] || { label: p.runtime, color: "#A1A1AA" };
              return (
                <button
                  key={p.runtime}
                  onClick={() => onManualResolve(c.conflict_id, p.runtime)}
                  className="btn-sharp px-3 py-1.5 border text-[11px] font-mono hover:bg-opacity-100"
                  style={{ borderColor: meta.color, color: meta.color }}
                  data-testid={`resolve-${c.conflict_id}-${p.runtime}`}
                >
                  {meta.label} was right
                </button>
              );
            })}
          </div>
        </div>
      )}

      {c.status !== "open" && c.notes && (
        <div className="px-4 py-2 bg-rd-bg2 border-t border-rd-border text-[10px] text-rd-muted font-mono">
          note: {c.notes} {c.resolved_by ? `· by ${c.resolved_by}` : ""} {c.resolution_source ? `· ${c.resolution_source}` : ""}
        </div>
      )}
    </Card>
  );
}
