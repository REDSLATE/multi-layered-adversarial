import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState } from "@/components/ui-bits";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import {
  ArrowsClockwise, Check, X, TrendUp, TrendDown, Scales,
  ShieldCheck, Prohibit, Sparkle, Pulse, Brain, GitFork, Shield,
} from "@phosphor-icons/react";

// ── Stage 3 UI — Kernel Review Queue ──────────────────────────────
//
// Two learning queues share this page:
//
//   1. SIZING LESSONS — proposals from `learning_lessons`; feed the
//      doctrine overlay's per-bucket notional multiplier band (±20%).
//
//   2. GATE TUNING — proposals from `counterfactual_tuning_signals`
//      (distilled from resolved blocked-trade counterfactuals); feed
//      the doctrine overlay's per-gate threshold delta (±20%).
//
// Both use the same state machine:
//   proposed → approved | rejected  (approved feeds next doctrine
//   iteration; nothing self-applies).

const QUEUES = [
  {
    key: "lessons",
    label: "Sizing Lessons",
    icon: Sparkle,
    color: "#F59E0B",
    eyebrow: "Learning Loop · Stage 3",
    itemLabelSingular: "lesson",
    itemLabelPlural: "lessons",
    endpoints: {
      list:    (state) => `/admin/learning/lessons?state=${state}&limit=200`,
      approve: (id)    => `/admin/learning/lessons/${id}/approve`,
      reject:  (id)    => `/admin/learning/lessons/${id}/reject`,
      analyze: ()      => `/admin/learning/analyze`,
    },
    analyzeButtonLabel: "Run analyzer",
    guardrail: (
      <>
        Lessons reach this queue when a bucket clears:
        <span className="text-rd-text"> ≥30 resolved samples</span>,
        <span className="text-rd-text"> Wilson lower ≥ 0.50</span>, and
        <span className="text-rd-text"> shrunk EV ≥ +5 bps</span> (edge)
        or <span className="text-rd-text">avg 5m &lt; −10 bps</span> (bleed).
        Approved lessons feed the doctrine overlay&apos;s notional band (±20%).
      </>
    ),
    kindMeta: {
      edge:  { label: "EDGE",  color: "#10B981", icon: TrendUp,
               subtitle: "Bucket shows exploitable positive edge (raise exposure)" },
      bleed: { label: "BLEED", color: "#DC2626", icon: TrendDown,
               subtitle: "Bucket is bleeding — downshift or block" },
    },
  },
  {
    key: "tuning",
    label: "Gate Tuning",
    icon: GitFork,
    color: "#3B82F6",
    eyebrow: "Learning Loop · Counterfactual Feedback",
    itemLabelSingular: "tuning signal",
    itemLabelPlural: "tuning signals",
    endpoints: {
      list:    (state) => `/admin/counterfactuals/tuning-signals?state=${state}&limit=200`,
      approve: (id)    => `/admin/counterfactuals/tuning-signals/${id}/approve`,
      reject:  (id)    => `/admin/counterfactuals/tuning-signals/${id}/reject`,
      analyze: ()      => `/admin/counterfactuals/tune?horizon=15m`,
    },
    analyzeButtonLabel: "Run tuner",
    guardrail: (
      <>
        Tuning signals reach this queue when a
        <span className="text-rd-text"> (blocked_reason, lane) </span>
        group clears
        <span className="text-rd-text"> ≥30 resolved signals</span>,
        <span className="text-rd-text"> Wilson lower ≥ 0.60</span>, and
        <span className="text-rd-text"> |shrunk avg| ≥ 5 bps</span>.
        Approved signals feed the doctrine overlay&apos;s gate-threshold delta (±20%).
      </>
    ),
    kindMeta: {
      relax_gate:    { label: "RELAX GATE", color: "#10B981", icon: TrendUp,
                       subtitle: "Blocked directions kept winning — gate is over-blocking real wins" },
      preserve_gate: { label: "PRESERVE GATE", color: "#DC2626", icon: Shield,
                       subtitle: "Blocked directions kept losing — gate is dodging real losses" },
    },
  },
];

const STATES = [
  { key: "proposed",  label: "Proposed",  color: "#F59E0B", icon: Sparkle },
  { key: "approved",  label: "Approved",  color: "#10B981", icon: ShieldCheck },
  { key: "rejected",  label: "Rejected",  color: "#DC2626", icon: Prohibit },
  { key: "applied",   label: "Applied",   color: "#3B82F6", icon: Pulse },
];

function fmtBps(v) {
  if (v == null || isNaN(v)) return "—";
  const n = Number(v);
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(1)} bps`;
}

function fmtPct(v) {
  if (v == null || isNaN(v)) return "—";
  return `${(Number(v) * 100).toFixed(1)}%`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

function EvidenceRow({ label, value, mono = true, color, testid }) {
  return (
    <div
      className="flex items-baseline justify-between py-1"
      data-testid={testid}
    >
      <span className="text-[10px] uppercase tracking-widest text-rd-dim">
        {label}
      </span>
      <span
        className={`text-xs ${mono ? "font-mono" : ""}`}
        style={color ? { color } : undefined}
      >
        {value}
      </span>
    </div>
  );
}

function DimsBadges({ dims, testidPrefix = "lesson-dim" }) {
  if (!dims || typeof dims !== "object") return null;
  const entries = Object.entries(dims);
  if (!entries.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5 mt-2" data-testid="item-dims">
      {entries.map(([k, v]) => (
        <span
          key={k}
          className="text-[9px] font-mono uppercase tracking-widest border border-rd-border px-1.5 py-0.5 text-rd-muted"
          data-testid={`${testidPrefix}-${k}`}
        >
          {k}={String(v)}
        </span>
      ))}
    </div>
  );
}

// ── Lesson evidence (queue=lessons) ───────────────────────────────
function LessonEvidence({ item }) {
  const ev = item.evidence || {};
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 mt-4 pt-3 border-t border-rd-border">
      <EvidenceRow label="Samples" value={ev.samples ?? "—"}
        testid={`lesson-samples-${item._id}`} />
      <EvidenceRow label="Hit rate" value={fmtPct(ev.hit_rate)}
        testid={`lesson-hitrate-${item._id}`} />
      <EvidenceRow label="Wilson ↓"
        value={ev.wilson_lower != null ? Number(ev.wilson_lower).toFixed(3) : "—"}
        testid={`lesson-wilson-${item._id}`} />
      <EvidenceRow label="Avg 5m" value={fmtBps(ev.avg_5m_bps)}
        color={(ev.avg_5m_bps ?? 0) >= 0 ? "#10B981" : "#DC2626"}
        testid={`lesson-avg5m-${item._id}`} />
      <EvidenceRow label="Shrunk EV" value={fmtBps(ev.shrunk_ev_bps)}
        color={(ev.shrunk_ev_bps ?? 0) >= 0 ? "#10B981" : "#DC2626"}
        testid={`lesson-shrunkev-${item._id}`} />
      <EvidenceRow label="Wins" value={ev.wins ?? "—"} />
      <EvidenceRow label="Losses" value={ev.losses ?? "—"} />
      <EvidenceRow label="Avg 1h" value={fmtBps(ev.avg_1h_bps)}
        color={(ev.avg_1h_bps ?? 0) >= 0 ? "#10B981" : "#DC2626"} />
    </div>
  );
}

// ── Tuning-signal evidence (queue=tuning) ─────────────────────────
function TuningEvidence({ item }) {
  const ev = item.evidence || {};
  const isRelax = item.kind === "relax_gate";
  // For relax: highlight missed-win rate + wilson.
  // For preserve: highlight correct-block rate + wilson.
  const focusRate = isRelax ? ev.missed_win_rate : ev.correct_block_rate;
  const focusWilson = isRelax
    ? ev.wilson_lower_missed_win : ev.wilson_lower_correct_block;
  const focusRateLabel = isRelax ? "Missed-win %" : "Correct-block %";
  const focusWilsonLabel = isRelax ? "Wilson MW ↓" : "Wilson CB ↓";
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 mt-4 pt-3 border-t border-rd-border">
      <EvidenceRow label="Samples" value={ev.samples ?? "—"}
        testid={`tuning-samples-${item._id}`} />
      <EvidenceRow label={focusRateLabel} value={fmtPct(focusRate)}
        testid={`tuning-rate-${item._id}`} />
      <EvidenceRow label={focusWilsonLabel}
        value={focusWilson != null ? Number(focusWilson).toFixed(3) : "—"}
        testid={`tuning-wilson-${item._id}`} />
      <EvidenceRow label="Shrunk avg" value={fmtBps(ev.shrunk_avg_bps)}
        color={(ev.shrunk_avg_bps ?? 0) >= 0 ? "#10B981" : "#DC2626"}
        testid={`tuning-shrunk-${item._id}`} />
      <EvidenceRow label="Missed wins" value={ev.missed_wins ?? "—"} />
      <EvidenceRow label="Correct blocks" value={ev.correct_blocks ?? "—"} />
      <EvidenceRow label="Undetermined" value={ev.undetermined ?? "—"} />
      <EvidenceRow label="Avg return" value={fmtBps(ev.avg_return_bps)}
        color={(ev.avg_return_bps ?? 0) >= 0 ? "#10B981" : "#DC2626"} />
    </div>
  );
}

function ItemCard({ item, queue, onApprove, onReject, busy }) {
  const kind = item.kind || Object.keys(queue.kindMeta)[0];
  const meta = queue.kindMeta[kind]
    || { label: kind.toUpperCase(), color: "#71717A", icon: Sparkle,
         subtitle: "" };
  const Icon = meta.icon;
  const state = item.state || "proposed";
  const proposal = item.proposal || {};

  // Lesson uses bucket_label / bucket_id; tuning uses group.blocked_reason.
  const label = item.bucket_label
    || item.bucket_id
    || (item.group
        ? `${item.group.blocked_reason} · ${item.group.lane}`
        : item._id);
  // Lesson uses proposal.target_pattern; tuning uses proposal target_gate/lane.
  const dims = proposal.target_pattern
    || (proposal.target_gate
        ? { gate: proposal.target_gate, lane: proposal.target_lane }
        : null);

  return (
    <div
      className="border border-rd-border bg-rd-bg2 p-4"
      style={{ borderTop: `2px solid ${meta.color}` }}
      data-testid={`${queue.key}-card-${item._id}`}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 mb-1">
            <Icon size={13} weight="bold" style={{ color: meta.color }} />
            <span
              className="text-[10px] font-mono uppercase tracking-[0.22em]"
              style={{ color: meta.color }}
              data-testid={`${queue.key}-kind-${item._id}`}
            >
              {meta.label}
            </span>
            <Badge color="#71717A">{state}</Badge>
          </div>
          <div
            className="text-sm font-mono text-rd-text truncate"
            data-testid={`${queue.key}-label-${item._id}`}
            title={label}
          >
            {label}
          </div>
          <div className="text-[10px] font-mono text-rd-dim mt-0.5">
            {meta.subtitle}
          </div>
          <DimsBadges dims={dims} testidPrefix={`${queue.key}-dim`} />
        </div>

        {state === "proposed" && (
          <div className="flex flex-col gap-1.5 shrink-0">
            <Button
              size="sm"
              onClick={() => onApprove(item._id)}
              disabled={busy}
              className="bg-emerald-600 hover:bg-emerald-500 text-white h-7 px-3 text-xs"
              data-testid={`${queue.key}-approve-${item._id}`}
            >
              <Check size={12} weight="bold" className="mr-1" />
              Approve
            </Button>
            <Button
              size="sm"
              onClick={() => onReject(item._id)}
              disabled={busy}
              variant="outline"
              className="border-red-800 text-red-400 hover:bg-red-900/20 h-7 px-3 text-xs"
              data-testid={`${queue.key}-reject-${item._id}`}
            >
              <X size={12} weight="bold" className="mr-1" />
              Reject
            </Button>
          </div>
        )}
      </div>

      {queue.key === "tuning"
        ? <TuningEvidence item={item} />
        : <LessonEvidence item={item} />}

      {/* Suggested action */}
      {proposal.suggested_action && (
        <div
          className="mt-3 pt-3 border-t border-rd-border text-[11px] font-mono text-rd-muted leading-relaxed"
          data-testid={`${queue.key}-action-${item._id}`}
        >
          <span className="text-rd-dim uppercase tracking-widest text-[9px] mr-2">Action:</span>
          {proposal.suggested_action}
        </div>
      )}

      {/* Footer timeline */}
      <div className="mt-3 pt-2 border-t border-rd-border flex flex-wrap gap-x-4 gap-y-1 text-[10px] font-mono text-rd-dim">
        <span data-testid={`${queue.key}-proposed-at-${item._id}`}>
          Proposed: {fmtTime(item.proposed_at)}
        </span>
        {item.approved_at && (
          <span data-testid={`${queue.key}-approved-at-${item._id}`}>
            Approved by {item.approved_by || "operator"}: {fmtTime(item.approved_at)}
          </span>
        )}
        {item.rejected_at && (
          <span data-testid={`${queue.key}-rejected-at-${item._id}`}>
            Rejected by {item.rejected_by || "operator"}: {fmtTime(item.rejected_at)}
          </span>
        )}
        {item.updated_at && (
          <span>Evidence updated: {fmtTime(item.updated_at)}</span>
        )}
      </div>
    </div>
  );
}

// ── Sort ordering per queue ───────────────────────────────────────
function useSortedItems(items, queueKey) {
  return useMemo(() => {
    if (queueKey === "tuning") {
      // Relax first (unlocks profit), then preserve; by |shrunk_avg|.
      const kindOrder = { relax_gate: 0, preserve_gate: 1 };
      return [...items].sort((a, b) => {
        const ka = kindOrder[a.kind] ?? 2;
        const kb = kindOrder[b.kind] ?? 2;
        if (ka !== kb) return ka - kb;
        const ea = Math.abs(a.evidence?.shrunk_avg_bps ?? 0);
        const eb = Math.abs(b.evidence?.shrunk_avg_bps ?? 0);
        return eb - ea;
      });
    }
    // Lessons: edge first, then bleed; by |shrunk_ev|.
    const kindOrder = { edge: 0, bleed: 1 };
    return [...items].sort((a, b) => {
      const ka = kindOrder[a.kind] ?? 2;
      const kb = kindOrder[b.kind] ?? 2;
      if (ka !== kb) return ka - kb;
      const ea = Math.abs(a.evidence?.shrunk_ev_bps ?? 0);
      const eb = Math.abs(b.evidence?.shrunk_ev_bps ?? 0);
      return eb - ea;
    });
  }, [items, queueKey]);
}

export default function KernelReview() {
  const [activeQueue, setActiveQueue] = useState("lessons");
  const [activeState, setActiveState] = useState("proposed");
  const [items, setItems] = useState([]);
  const [counts, setCounts] = useState({});
  const [loading, setLoading] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [busyIds, setBusyIds] = useState(new Set());
  const [lastAnalyze, setLastAnalyze] = useState(null);

  const queue = useMemo(
    () => QUEUES.find((q) => q.key === activeQueue) || QUEUES[0],
    [activeQueue],
  );

  const loadItems = useCallback(async (q, state) => {
    setLoading(true);
    try {
      const res = await api.get(q.endpoints.list(state));
      setItems(res?.items || []);
    } catch (e) {
      toast.error(`Failed to load ${state} ${q.itemLabelPlural}`);
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadAllCounts = useCallback(async (q) => {
    const nextCounts = {};
    await Promise.all(
      STATES.map(async (s) => {
        try {
          const r = await api.get(q.endpoints.list(s.key));
          nextCounts[s.key] = (r?.items || []).length;
        } catch {
          nextCounts[s.key] = 0;
        }
      }),
    );
    setCounts(nextCounts);
  }, []);

  useEffect(() => {
    setLastAnalyze(null);
    loadItems(queue, activeState);
    loadAllCounts(queue);
  }, [queue, activeState, loadItems, loadAllCounts]);

  const setBusy = (id, on) => {
    setBusyIds((prev) => {
      const next = new Set(prev);
      if (on) next.add(id); else next.delete(id);
      return next;
    });
  };

  const handleApprove = useCallback(async (id) => {
    setBusy(id, true);
    try {
      const r = await api.post(queue.endpoints.approve(id));
      if (r?.ok) {
        toast.success(`${queue.itemLabelSingular} approved`);
        await Promise.all([
          loadItems(queue, activeState),
          loadAllCounts(queue),
        ]);
      } else {
        toast.error(r?.reason || "Approve failed");
      }
    } catch (e) {
      toast.error("Approve failed");
    } finally {
      setBusy(id, false);
    }
  }, [queue, activeState, loadItems, loadAllCounts]);

  const handleReject = useCallback(async (id) => {
    setBusy(id, true);
    try {
      const r = await api.post(queue.endpoints.reject(id));
      if (r?.ok) {
        toast.success(`${queue.itemLabelSingular} rejected`);
        await Promise.all([
          loadItems(queue, activeState),
          loadAllCounts(queue),
        ]);
      } else {
        toast.error(r?.reason || "Reject failed");
      }
    } catch (e) {
      toast.error("Reject failed");
    } finally {
      setBusy(id, false);
    }
  }, [queue, activeState, loadItems, loadAllCounts]);

  const handleAnalyze = useCallback(async () => {
    setAnalyzing(true);
    try {
      const r = await api.post(queue.endpoints.analyze());
      if (r?.ok) {
        if (queue.key === "tuning") {
          setLastAnalyze({
            scanned: r.groups_scanned ?? 0,
            relax: r.relax_proposals ?? 0,
            preserve: r.preserve_proposals ?? 0,
          });
          toast.success(
            `Tuner done: ${r.relax_proposals ?? 0} relax · ${r.preserve_proposals ?? 0} preserve`,
          );
        } else {
          const b = r.buckets || {};
          const l = r.lessons || {};
          setLastAnalyze({
            scanned: b.buckets_scanned ?? b.scanned,
            edge: l.edge_lessons ?? 0,
            bleed: l.bleed_lessons ?? 0,
          });
          toast.success(
            `Analyzer done: ${l.edge_lessons ?? 0} edge · ${l.bleed_lessons ?? 0} bleed`,
          );
        }
        await Promise.all([
          loadItems(queue, activeState),
          loadAllCounts(queue),
        ]);
      } else {
        toast.error(`${queue.analyzeButtonLabel} failed`);
      }
    } catch (e) {
      toast.error(`${queue.analyzeButtonLabel} failed`);
    } finally {
      setAnalyzing(false);
    }
  }, [queue, activeState, loadItems, loadAllCounts]);

  const sortedItems = useSortedItems(items, queue.key);

  return (
    <div className="min-h-screen bg-rd-bg text-rd-text p-6" data-testid="kernel-review-page">
      <PageHeader
        eyebrow={queue.eyebrow}
        title="Kernel Review"
        sub={`Approve or reject ${queue.itemLabelPlural} before they feed the next doctrine iteration. Nothing self-applies — every doctrine change goes through this queue.`}
        testid="kernel-review-header"
        right={
          <div className="flex items-center gap-2">
            {lastAnalyze && (
              <span className="text-[10px] font-mono text-rd-dim uppercase tracking-widest hidden md:block">
                {queue.key === "tuning"
                  ? `Groups: ${lastAnalyze.scanned} · Relax +${lastAnalyze.relax} · Preserve +${lastAnalyze.preserve}`
                  : `Buckets: ${lastAnalyze.scanned} · Edge +${lastAnalyze.edge} · Bleed +${lastAnalyze.bleed}`}
              </span>
            )}
            <Button
              size="sm"
              onClick={handleAnalyze}
              disabled={analyzing}
              className="bg-amber-600 hover:bg-amber-500 text-black h-8 px-3 text-xs"
              data-testid="analyze-button"
            >
              <Brain size={12} weight="bold" className="mr-1" />
              {analyzing ? "Working…" : queue.analyzeButtonLabel}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => Promise.all([
                loadItems(queue, activeState),
                loadAllCounts(queue),
              ])}
              disabled={loading}
              className="h-8 px-3 text-xs"
              data-testid="refresh-button"
            >
              <ArrowsClockwise size={12} weight="bold" className="mr-1" />
              Refresh
            </Button>
          </div>
        }
      />

      {/* Queue switcher */}
      <div
        className="flex items-center gap-2 mb-4"
        data-testid="queue-switcher"
      >
        {QUEUES.map((q) => {
          const Icon = q.icon;
          const active = activeQueue === q.key;
          return (
            <button
              key={q.key}
              onClick={() => {
                setActiveQueue(q.key);
                setActiveState("proposed");
              }}
              className={`
                flex items-center gap-2 px-4 py-2 text-[11px] font-mono uppercase tracking-[0.18em]
                border transition-colors
                ${active
                  ? "border-rd-text text-rd-text bg-rd-bg2"
                  : "border-rd-border text-rd-dim hover:text-rd-muted hover:border-rd-muted"}
              `}
              style={active ? { borderColor: q.color, color: q.color } : {}}
              data-testid={`queue-tab-${q.key}`}
            >
              <Icon size={12} weight="bold" />
              {q.label}
            </button>
          );
        })}
      </div>

      {/* State tabs */}
      <div
        className="flex items-center gap-2 border-b border-rd-border mb-6"
        data-testid="state-tabs"
      >
        {STATES.map((s) => {
          const Icon = s.icon;
          const active = activeState === s.key;
          const cnt = counts[s.key];
          return (
            <button
              key={s.key}
              onClick={() => setActiveState(s.key)}
              className={`
                flex items-center gap-2 px-3 py-2 text-[10px] font-mono uppercase tracking-[0.22em]
                border-b-2 -mb-px transition-colors
                ${active
                  ? "text-rd-text"
                  : "text-rd-dim border-transparent hover:text-rd-muted"}
              `}
              style={active ? { borderColor: s.color, color: s.color } : {}}
              data-testid={`state-tab-${s.key}`}
            >
              <Icon size={11} weight="bold" />
              {s.label}
              {cnt != null && (
                <span className="text-rd-dim">· {cnt}</span>
              )}
            </button>
          );
        })}
      </div>

      {/* Guardrail explainer card — only on proposed tab */}
      {activeState === "proposed" && (
        <Card className="mb-6" accentColor={queue.color} testid="guardrail-explainer">
          <div className="flex items-start gap-3">
            <Scales size={16} weight="bold" style={{ color: queue.color }} className="mt-0.5" />
            <div className="text-[11px] font-mono text-rd-muted leading-relaxed">
              <div className="text-rd-text uppercase tracking-[0.22em] text-[10px] mb-2">
                Guardrails · {queue.label}
              </div>
              {queue.guardrail}
            </div>
          </div>
        </Card>
      )}

      {/* Items list */}
      {loading ? (
        <div
          className="text-xs font-mono text-rd-dim uppercase tracking-widest text-center py-10"
          data-testid="items-loading"
        >
          Loading {queue.itemLabelPlural}…
        </div>
      ) : sortedItems.length === 0 ? (
        <EmptyState
          message={
            activeState === "proposed"
              ? `No proposed ${queue.itemLabelPlural} yet. Run the ${queue.key === "tuning" ? "tuner" : "analyzer"} or wait for more resolved ${queue.key === "tuning" ? "counterfactuals" : "experiences"}.`
              : `No ${activeState} ${queue.itemLabelPlural}.`
          }
          testid="items-empty"
        />
      ) : (
        <div
          className="flex flex-col gap-3"
          data-testid="items-list"
        >
          {sortedItems.map((item) => (
            <ItemCard
              key={item._id}
              item={item}
              queue={queue}
              onApprove={handleApprove}
              onReject={handleReject}
              busy={busyIds.has(item._id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
