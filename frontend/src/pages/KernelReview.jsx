import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState } from "@/components/ui-bits";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import {
  ArrowsClockwise, Check, X, TrendUp, TrendDown, Scales,
  ShieldCheck, Prohibit, Sparkle, Pulse, Brain,
} from "@phosphor-icons/react";

// ── Kernel Review — one operator screen for one learning loop ─────
//
// Doctrine (2026-02-19 simplification pass): ONE learning chain,
// ONE review screen. Executed or blocked directional intent →
// outcome → lesson → Kernel review → approved bounded adjustment.
// The prior "Gate Tuning" queue was folded away — sizing lessons
// are the single lever the operator has over the doctrine overlay.
//
// Backend contract:
//   GET  /api/admin/learning/lessons?state=proposed|approved|rejected|applied
//   POST /api/admin/learning/lessons/{id}/approve
//   POST /api/admin/learning/lessons/{id}/reject
//   POST /api/admin/learning/analyze  (rebuild buckets → propose)

const STATES = [
  { key: "proposed",  label: "Proposed",  color: "#F59E0B", icon: Sparkle },
  { key: "approved",  label: "Approved",  color: "#10B981", icon: ShieldCheck },
  { key: "rejected",  label: "Rejected",  color: "#DC2626", icon: Prohibit },
  { key: "applied",   label: "Applied",   color: "#3B82F6", icon: Pulse },
];

const KIND_META = {
  edge:  { label: "EDGE",  color: "#10B981", icon: TrendUp,
           subtitle: "Bucket shows exploitable positive edge (raise exposure)" },
  bleed: { label: "BLEED", color: "#DC2626", icon: TrendDown,
           subtitle: "Bucket is bleeding — downshift or block" },
};

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

function DimsBadges({ dims }) {
  if (!dims || typeof dims !== "object") return null;
  const entries = Object.entries(dims);
  if (!entries.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5 mt-2" data-testid="lesson-dims">
      {entries.map(([k, v]) => (
        <span
          key={k}
          className="text-[9px] font-mono uppercase tracking-widest border border-rd-border px-1.5 py-0.5 text-rd-muted"
          data-testid={`lesson-dim-${k}`}
        >
          {k}={String(v)}
        </span>
      ))}
    </div>
  );
}

function LessonCard({ lesson, onApprove, onReject, busy }) {
  const kind = lesson.kind || "edge";
  const meta = KIND_META[kind] || KIND_META.edge;
  const Icon = meta.icon;
  const state = lesson.state || "proposed";
  const ev = lesson.evidence || {};
  const proposal = lesson.proposal || {};

  return (
    <div
      className="border border-rd-border bg-rd-bg2 p-4"
      style={{ borderTop: `2px solid ${meta.color}` }}
      data-testid={`lesson-card-${lesson._id}`}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 mb-1">
            <Icon size={13} weight="bold" style={{ color: meta.color }} />
            <span
              className="text-[10px] font-mono uppercase tracking-[0.22em]"
              style={{ color: meta.color }}
              data-testid={`lesson-kind-${lesson._id}`}
            >
              {meta.label}
            </span>
            <Badge color="#71717A">{state}</Badge>
          </div>
          <div
            className="text-sm font-mono text-rd-text truncate"
            data-testid={`lesson-label-${lesson._id}`}
            title={lesson.bucket_label || lesson.bucket_id}
          >
            {lesson.bucket_label || lesson.bucket_id}
          </div>
          <div className="text-[10px] font-mono text-rd-dim mt-0.5">
            {meta.subtitle}
          </div>
          <DimsBadges dims={proposal.target_pattern} />
        </div>

        {state === "proposed" && (
          <div className="flex flex-col gap-1.5 shrink-0">
            <Button
              size="sm"
              onClick={() => onApprove(lesson._id)}
              disabled={busy}
              className="bg-emerald-600 hover:bg-emerald-500 text-white h-7 px-3 text-xs"
              data-testid={`lesson-approve-${lesson._id}`}
            >
              <Check size={12} weight="bold" className="mr-1" />
              Approve
            </Button>
            <Button
              size="sm"
              onClick={() => onReject(lesson._id)}
              disabled={busy}
              variant="outline"
              className="border-red-800 text-red-400 hover:bg-red-900/20 h-7 px-3 text-xs"
              data-testid={`lesson-reject-${lesson._id}`}
            >
              <X size={12} weight="bold" className="mr-1" />
              Reject
            </Button>
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 mt-4 pt-3 border-t border-rd-border">
        <EvidenceRow label="Samples" value={ev.samples ?? "—"}
          testid={`lesson-samples-${lesson._id}`} />
        <EvidenceRow label="Hit rate" value={fmtPct(ev.hit_rate)}
          testid={`lesson-hitrate-${lesson._id}`} />
        <EvidenceRow label="Wilson ↓"
          value={ev.wilson_lower != null ? Number(ev.wilson_lower).toFixed(3) : "—"}
          testid={`lesson-wilson-${lesson._id}`} />
        <EvidenceRow label="Avg 5m" value={fmtBps(ev.avg_5m_bps)}
          color={(ev.avg_5m_bps ?? 0) >= 0 ? "#10B981" : "#DC2626"}
          testid={`lesson-avg5m-${lesson._id}`} />
        <EvidenceRow label="Shrunk EV" value={fmtBps(ev.shrunk_ev_bps)}
          color={(ev.shrunk_ev_bps ?? 0) >= 0 ? "#10B981" : "#DC2626"}
          testid={`lesson-shrunkev-${lesson._id}`} />
        <EvidenceRow label="Wins" value={ev.wins ?? "—"} />
        <EvidenceRow label="Losses" value={ev.losses ?? "—"} />
        <EvidenceRow label="Avg 1h" value={fmtBps(ev.avg_1h_bps)}
          color={(ev.avg_1h_bps ?? 0) >= 0 ? "#10B981" : "#DC2626"} />
      </div>

      {proposal.suggested_action && (
        <div
          className="mt-3 pt-3 border-t border-rd-border text-[11px] font-mono text-rd-muted leading-relaxed"
          data-testid={`lesson-action-${lesson._id}`}
        >
          <span className="text-rd-dim uppercase tracking-widest text-[9px] mr-2">Action:</span>
          {proposal.suggested_action}
        </div>
      )}

      <div className="mt-3 pt-2 border-t border-rd-border flex flex-wrap gap-x-4 gap-y-1 text-[10px] font-mono text-rd-dim">
        <span data-testid={`lesson-proposed-at-${lesson._id}`}>
          Proposed: {fmtTime(lesson.proposed_at)}
        </span>
        {lesson.approved_at && (
          <span data-testid={`lesson-approved-at-${lesson._id}`}>
            Approved by {lesson.approved_by || "operator"}: {fmtTime(lesson.approved_at)}
          </span>
        )}
        {lesson.rejected_at && (
          <span data-testid={`lesson-rejected-at-${lesson._id}`}>
            Rejected by {lesson.rejected_by || "operator"}: {fmtTime(lesson.rejected_at)}
          </span>
        )}
        {lesson.updated_at && (
          <span>Evidence updated: {fmtTime(lesson.updated_at)}</span>
        )}
      </div>
    </div>
  );
}

export default function KernelReview() {
  const [activeState, setActiveState] = useState("proposed");
  const [lessons, setLessons] = useState([]);
  const [counts, setCounts] = useState({});
  const [loading, setLoading] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [busyIds, setBusyIds] = useState(new Set());
  const [lastAnalyze, setLastAnalyze] = useState(null);

  const loadLessons = useCallback(async (state) => {
    setLoading(true);
    try {
      const res = await api.get(`/admin/learning/lessons?state=${state}&limit=200`);
      setLessons(res?.items || []);
    } catch (e) {
      toast.error(`Failed to load ${state} lessons`);
      setLessons([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadAllCounts = useCallback(async () => {
    const nextCounts = {};
    await Promise.all(
      STATES.map(async (s) => {
        try {
          const r = await api.get(`/admin/learning/lessons?state=${s.key}&limit=200`);
          nextCounts[s.key] = (r?.items || []).length;
        } catch {
          nextCounts[s.key] = 0;
        }
      }),
    );
    setCounts(nextCounts);
  }, []);

  useEffect(() => { loadLessons(activeState); }, [activeState, loadLessons]);
  useEffect(() => { loadAllCounts(); }, [loadAllCounts]);

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
      const r = await api.post(`/admin/learning/lessons/${id}/approve`);
      if (r?.ok) {
        toast.success("Lesson approved");
        await Promise.all([loadLessons(activeState), loadAllCounts()]);
      } else {
        toast.error(r?.reason || "Approve failed");
      }
    } catch (e) {
      toast.error("Approve failed");
    } finally {
      setBusy(id, false);
    }
  }, [activeState, loadLessons, loadAllCounts]);

  const handleReject = useCallback(async (id) => {
    setBusy(id, true);
    try {
      const r = await api.post(`/admin/learning/lessons/${id}/reject`);
      if (r?.ok) {
        toast.success("Lesson rejected");
        await Promise.all([loadLessons(activeState), loadAllCounts()]);
      } else {
        toast.error(r?.reason || "Reject failed");
      }
    } catch (e) {
      toast.error("Reject failed");
    } finally {
      setBusy(id, false);
    }
  }, [activeState, loadLessons, loadAllCounts]);

  const handleAnalyze = useCallback(async () => {
    setAnalyzing(true);
    try {
      const r = await api.post(`/admin/learning/analyze`);
      if (r?.ok) {
        const b = r.buckets || {};
        const l = r.lessons || {};
        setLastAnalyze({
          buckets_scanned: b.buckets_scanned ?? b.scanned,
          edge: l.edge_lessons ?? 0,
          bleed: l.bleed_lessons ?? 0,
        });
        toast.success(
          `Analyzer done: ${l.edge_lessons ?? 0} edge · ${l.bleed_lessons ?? 0} bleed`,
        );
        await Promise.all([loadLessons(activeState), loadAllCounts()]);
      } else {
        toast.error("Analyzer failed");
      }
    } catch (e) {
      toast.error("Analyzer failed");
    } finally {
      setAnalyzing(false);
    }
  }, [activeState, loadLessons, loadAllCounts]);

  const sortedLessons = useMemo(() => {
    const kindOrder = { edge: 0, bleed: 1 };
    return [...lessons].sort((a, b) => {
      const ka = kindOrder[a.kind] ?? 2;
      const kb = kindOrder[b.kind] ?? 2;
      if (ka !== kb) return ka - kb;
      const ea = Math.abs(a.evidence?.shrunk_ev_bps ?? 0);
      const eb = Math.abs(b.evidence?.shrunk_ev_bps ?? 0);
      return eb - ea;
    });
  }, [lessons]);

  return (
    <div className="min-h-screen bg-rd-bg text-rd-text p-6" data-testid="kernel-review-page">
      <PageHeader
        eyebrow="Learning Loop · Kernel Review"
        title="Kernel Review"
        sub="Approve or reject learning-loop lessons before they feed the next doctrine iteration. Nothing self-applies — every doctrine change goes through this queue."
        testid="kernel-review-header"
        right={
          <div className="flex items-center gap-2">
            {lastAnalyze && (
              <span className="text-[10px] font-mono text-rd-dim uppercase tracking-widest hidden md:block">
                Buckets: {lastAnalyze.buckets_scanned} · Edge +{lastAnalyze.edge} · Bleed +{lastAnalyze.bleed}
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
              {analyzing ? "Analyzing…" : "Run analyzer"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => Promise.all([loadLessons(activeState), loadAllCounts()])}
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

      {activeState === "proposed" && (
        <Card className="mb-6" accentColor="#F59E0B" testid="guardrail-explainer">
          <div className="flex items-start gap-3">
            <Scales size={16} weight="bold" style={{ color: "#F59E0B" }} className="mt-0.5" />
            <div className="text-[11px] font-mono text-rd-muted leading-relaxed">
              <div className="text-rd-text uppercase tracking-[0.22em] text-[10px] mb-2">
                Guardrails
              </div>
              Lessons ONLY reach this queue when they clear:
              <span className="text-rd-text"> ≥30 resolved samples</span>,
              <span className="text-rd-text"> Wilson lower ≥ 0.50</span>, and
              <span className="text-rd-text"> shrunk EV ≥ +5 bps</span> (edge)
              or <span className="text-rd-text">avg 5m &lt; −10 bps</span> (bleed).
              Approved lessons are trust signals for the next doctrine iteration — nothing self-applies.
            </div>
          </div>
        </Card>
      )}

      {loading ? (
        <div
          className="text-xs font-mono text-rd-dim uppercase tracking-widest text-center py-10"
          data-testid="lessons-loading"
        >
          Loading lessons…
        </div>
      ) : sortedLessons.length === 0 ? (
        <EmptyState
          message={
            activeState === "proposed"
              ? "No proposed lessons yet. Run the analyzer or wait for more resolved experiences."
              : `No ${activeState} lessons.`
          }
          testid="lessons-empty"
        />
      ) : (
        <div
          className="flex flex-col gap-3"
          data-testid="lessons-list"
        >
          {sortedLessons.map((lesson) => (
            <LessonCard
              key={lesson._id}
              lesson={lesson}
              onApprove={handleApprove}
              onReject={handleReject}
              busy={busyIds.has(lesson._id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
