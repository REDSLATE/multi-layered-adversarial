import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { api, RUNTIME_META, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";
import LivePulse from "@/components/LivePulse";
import {
  Activity, ChatCircle, ArrowsClockwise, Megaphone,
  Pulse, Scales, Trophy, Warning, ShieldCheck, GitBranch,
} from "@phosphor-icons/react";

// Brain meta extended with REDEYE (not in RUNTIME_META by design).
const ALL_BRAINS = {
  ...RUNTIME_META,
  redeye: {
    label: "REDEYE",
    project: "Sigma-RD",
    color: "#DC2626",
    roleTitle: "Advisor",
    note: "Bearish/short-side adversarial scout. Advises Camaro. Never executes.",
  },
};

const STANCE_OPTIONS = [
  "observation", "question", "hypothesis",
  "long", "short", "endorse", "veto",
  "agree", "disagree", "refine", "retract",
];

function SectionCard({ icon: Icon, title, sub, right, children, testid }) {
  return (
    <Card className="mb-6" testid={testid}>
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="flex items-start gap-3">
          {Icon && (
            <div className="mt-0.5 text-rd-dim">
              <Icon size={16} weight="duotone" />
            </div>
          )}
          <div>
            <div className="font-display text-base font-bold text-rd-text leading-none">{title}</div>
            {sub && <div className="text-[11px] text-rd-muted mt-1 font-mono leading-relaxed">{sub}</div>}
          </div>
        </div>
        {right}
      </div>
      {children}
    </Card>
  );
}

function MetricTile({ label, value, hint, color = "#A1A1AA", testid }) {
  return (
    <div className="border border-rd-border bg-rd-bg p-3" data-testid={testid}>
      <div className="flex items-center gap-2 mb-2">
        <span className="inline-block w-1.5 h-1.5 rounded-full" style={{ background: color }} />
        <span className="text-[10px] uppercase tracking-widest text-rd-dim">{label}</span>
      </div>
      <div className="font-display text-lg font-bold text-rd-text leading-none">{value}</div>
      {hint && <div className="text-[10px] text-rd-muted mt-1 font-mono">{hint}</div>}
    </div>
  );
}

function StanceTag({ stance }) {
  const s = (stance || "").toLowerCase();
  const map = {
    long: "#10B981", endorse: "#10B981",
    short: "#DC2626", veto: "#F59E0B",
    question: "#3B82F6", hypothesis: "#A78BFA",
    observation: "#A1A1AA",
    agree: "#10B981", disagree: "#DC2626",
    refine: "#3B82F6", retract: "#A1A1AA",
  };
  const color = map[s] || "#A1A1AA";
  return <Badge color={color}>{s.toUpperCase()}</Badge>;
}

function PulseStrip({ status, contributionAge }) {
  if (!status) return <LoadingRow />;
  const stateColor =
    status.connected === "connected" ? "#10B981"
    : status.connected === "partial" ? "#F59E0B"
    : "#DC2626";
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3" data-testid="brain-pulse-strip">
      <MetricTile
        label="Connection"
        value={(status.connected || "unknown").toUpperCase()}
        color={stateColor}
        testid="brain-pulse-conn"
      />
      <MetricTile
        label="Heartbeat age"
        value={status.heartbeat_age_seconds != null ? `${Math.round(status.heartbeat_age_seconds)}s` : "—"}
        hint="cadence ≤ 60s"
        testid="brain-pulse-hb"
      />
      <MetricTile
        label="Sovereign contrib"
        value={contributionAge != null ? `${Math.round(contributionAge)}s` : "—"}
        hint="cadence ≤ 5m"
        color={contributionAge != null && contributionAge < 600 ? "#10B981" : "#F59E0B"}
        testid="brain-pulse-contrib"
      />
      <MetricTile
        label="Last seen"
        value={relTime(status.last_seen)}
        testid="brain-pulse-seen"
      />
    </div>
  );
}

function OpinionRow({ op }) {
  return (
    <div className="border-b border-rd-border last:border-b-0 py-2.5" data-testid={`opinion-row-${op.opinion_id}`}>
      <div className="flex items-start gap-2 mb-1">
        <StanceTag stance={op.stance} />
        <span className="text-[11px] text-rd-muted font-mono">{op.topic}</span>
        {op.posted_via === "admin_proxy" && (
          <Badge color="#A78BFA">VIA ADMIN</Badge>
        )}
        <span className="text-[10px] text-rd-dim ml-auto font-mono">{relTime(op.posted_at)}</span>
      </div>
      <p className="text-[12px] text-rd-text leading-relaxed line-clamp-2">{op.body}</p>
    </div>
  );
}

function ConflictRow({ c }) {
  return (
    <div className="border-b border-rd-border last:border-b-0 py-2" data-testid={`conflict-row-${c.conflict_id}`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Warning size={11} weight="bold" className="text-rd-warn" />
          <span className="font-mono text-[12px] text-rd-text">{c.topic}</span>
          <Badge color={c.resolved_at ? "#10B981" : "#F59E0B"}>
            {c.resolved_at ? "RESOLVED" : "OPEN"}
          </Badge>
        </div>
        <span className="text-[10px] text-rd-dim font-mono">{relTime(c.detected_at)}</span>
      </div>
    </div>
  );
}

function SpeakAsForm({ brain, onPosted }) {
  const [topic, setTopic] = useState("free");
  const [stance, setStance] = useState("observation");
  const [confidence, setConfidence] = useState(0.5);
  const [body, setBody] = useState("");
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState(false);

  const submit = async () => {
    if (!body.trim() || sending) return;
    setSending(true); setErr(""); setOk(false);
    try {
      await api.post("/admin/runtime-discussion/opinion", {
        runtime: brain,
        topic: topic.trim() || "free",
        stance,
        confidence: Number(confidence),
        body: body.trim(),
        evidence: {},
        in_reply_to: null,
        may_execute: false,
      });
      setBody(""); setOk(true);
      onPosted?.();
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setSending(false);
    }
  };

  return (
    <div data-testid="speak-as-form">
      <div className="grid grid-cols-1 md:grid-cols-[1fr_140px_100px] gap-2 mb-2">
        <input
          value={topic} onChange={(e) => setTopic(e.target.value)}
          placeholder='free  or  symbol:TSLA'
          data-testid="speak-topic"
          className="bg-rd-bg border border-rd-border px-3 py-2 font-mono text-[12px] text-rd-text focus:border-rd-accent focus:outline-none"
        />
        <select
          value={stance} onChange={(e) => setStance(e.target.value)}
          data-testid="speak-stance"
          className="bg-rd-bg border border-rd-border px-2 py-2 font-mono text-[12px] text-rd-text"
        >
          {STANCE_OPTIONS.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <input
          type="number" min="0" max="1" step="0.05"
          value={confidence} onChange={(e) => setConfidence(e.target.value)}
          data-testid="speak-confidence"
          className="bg-rd-bg border border-rd-border px-2 py-2 font-mono text-[12px] text-rd-text"
        />
      </div>
      <textarea
        value={body} onChange={(e) => setBody(e.target.value)}
        placeholder={`say something (${brain}'s voice)…`}
        rows={3}
        maxLength={8000}
        data-testid="speak-body"
        className="w-full bg-rd-bg border border-rd-border px-3 py-2 font-mono text-[12px] text-rd-text focus:border-rd-accent focus:outline-none resize-y"
      />
      <div className="flex items-center justify-between mt-2">
        <div className="text-[10px] text-rd-dim font-mono">
          {body.length}/8000 chars · proxied via <code>/api/admin/runtime-discussion/opinion</code>
        </div>
        <div className="flex items-center gap-2">
          {err && <span className="text-[11px] text-rd-danger font-mono" data-testid="speak-error">{err}</span>}
          {ok && <span className="text-[11px] text-rd-success font-mono" data-testid="speak-ok">posted</span>}
          <button
            onClick={submit} disabled={sending || !body.trim()}
            data-testid="speak-submit"
            className="px-3 py-1.5 bg-rd-accent text-black font-mono text-[11px] uppercase tracking-widest hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {sending ? "Posting…" : `Post as ${brain}`}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function BrainConsole() {
  const { brain } = useParams();
  const meta = ALL_BRAINS[brain];

  const [status, setStatus] = useState(null);
  const [opinions, setOpinions] = useState(null);
  const [scorecard, setScorecard] = useState(null);
  const [conflicts, setConflicts] = useState(null);
  const [proposals, setProposals] = useState(null);
  const [authority, setAuthority] = useState(null);
  const [roster, setRoster] = useState(null);
  const [err, setErr] = useState("");

  const reload = useCallback(async () => {
    if (!meta) return;
    setErr("");
    try {
      const [s, o, sc, cf, pr, au, rs] = await Promise.all([
        api.get(`/heartbeat-status/${brain}`),
        api.get("/shared/opinions", { params: { runtime: brain, limit: 10 } }),
        api.get("/shared/scorecard", { params: { runtime: brain } }).catch(() => ({ data: null })),
        api.get("/shared/conflicts", { params: { runtime: brain, limit: 8 } }).catch(() => ({ data: { items: [] } })),
        api.get("/admin/promotion/proposals").catch(() => ({ data: { items: [] } })),
        api.get("/admin/promotion/state").catch(() => ({ data: { items: [] } })),
        api.get("/admin/roster").catch(() => ({ data: null })),
      ]);
      setStatus(s.data);
      setOpinions(o.data?.items || []);
      setScorecard(sc.data);
      setConflicts(cf.data?.items || []);
      setProposals((pr.data?.items || []).filter((p) => p.runtime === brain));
      setAuthority((au.data?.items || []).find((a) => a.runtime === brain) || null);
      setRoster(rs.data);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, [brain, meta]);

  useEffect(() => { reload(); }, [reload]);

  const summary = scorecard?.summary || {};
  const winRate = summary.total_resolved
    ? ((summary.wins || 0) / summary.total_resolved * 100).toFixed(1)
    : null;

  const pendingForBrain = useMemo(
    () => (proposals || []).filter((p) => p.status === "awaiting_second_sign" || p.status === "pending"),
    [proposals],
  );

  // Pass #19 (2026-05-28) — seat-as-authority doctrine.
  // The brain's current seat IS its authority. The promotion-ladder
  // rank (CHALLENGER / CO_TRADER / PRIMARY / ADVISOR) is no longer
  // an authority concept on this page — it's been removed per
  // operator decision: "restrictions belong with the position not
  // the brains. The seats restrict their movements."
  const currentSeat = useMemo(() => {
    const assignments = roster?.assignments || {};
    const seatsHeld = Object.entries(assignments)
      .filter(([_, b]) => b === brain)
      .map(([seat, _]) => seat);
    if (seatsHeld.length === 0) return null;
    // Prefer equity seats over crypto when a brain holds both.
    const preferenceOrder = [
      "strategist", "executor", "governor", "auditor",
      "crypto_strategist", "crypto", "crypto_governor", "crypto_auditor",
    ];
    seatsHeld.sort(
      (a, b) => preferenceOrder.indexOf(a) - preferenceOrder.indexOf(b),
    );
    return seatsHeld;
  }, [roster, brain]);

  const seatLabel = useMemo(() => {
    if (!currentSeat || currentSeat.length === 0) return "VACANT";
    const friendly = {
      strategist: "STRATEGIST",
      executor: "EXECUTOR",
      governor: "GOVERNOR",
      auditor: "AUDITOR",
      crypto: "CRYPTO EXECUTOR",
      crypto_strategist: "CRYPTO STRATEGIST",
      crypto_governor: "CRYPTO GOVERNOR",
      crypto_auditor: "CRYPTO AUDITOR",
    };
    return currentSeat.map((s) => friendly[s] || s.toUpperCase()).join(" · ");
  }, [currentSeat]);

  if (!meta) {
    return (
      <div className="p-10 text-center text-rd-danger" data-testid="brain-unknown">
        Unknown brain: {brain}.{" "}
        <Link to="/admin" className="underline">Back to overview</Link>
      </div>
    );
  }

  return (
    <div className="reveal" data-testid={`brain-console-${brain}`}>
      <PageHeader
        eyebrow={`Brain Console · ${meta.project}`}
        title={meta.label}
        sub={meta.note}
        right={
          <div className="flex items-center gap-3">
            <LivePulse runtime={brain} />
            <Badge
              color={currentSeat && currentSeat.length > 0 ? meta.color : "#6B7280"}
              data-testid="brain-header-seat-badge"
            >
              {seatLabel}
            </Badge>
            <button
              onClick={reload}
              data-testid="brain-reload"
              className="p-1.5 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text transition-colors"
              title="Reload"
            >
              <ArrowsClockwise size={12} weight="bold" />
            </button>
          </div>
        }
        testid={`brain-header-${brain}`}
      />

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono" data-testid="brain-error">
          {err}
        </div>
      )}

      {/* Top row — pulse + authority */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6">
        <div className="lg:col-span-2">
          <SectionCard
            icon={Pulse}
            title="Mission Control Pulse"
            sub={`${meta.label} ↔ MC heartbeat + sovereign contribution`}
            testid="mc-pulse-section"
          >
            <PulseStrip
              status={status}
              contributionAge={status?.contribution_age_seconds}
            />
          </SectionCard>
        </div>
        <SectionCard
          icon={ShieldCheck}
          title="Authority"
          sub="seat policy is the source of authority"
          testid="authority-section"
        >
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-[10px] uppercase tracking-widest text-rd-dim">Seat</span>
              <Badge
                color={currentSeat && currentSeat.length > 0 ? meta.color : "#6B7280"}
                data-testid="authority-seat-badge"
              >
                {seatLabel}
              </Badge>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-[10px] uppercase tracking-widest text-rd-dim">May execute</span>
              <Badge
                color={currentSeat && currentSeat.some((s) => s === "executor" || s === "crypto") ? "#10B981" : "#6B7280"}
                data-testid="authority-may-execute"
              >
                {currentSeat && currentSeat.some((s) => s === "executor" || s === "crypto") ? "TRUE" : "FALSE"}
              </Badge>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-[10px] uppercase tracking-widest text-rd-dim">May veto</span>
              <Badge
                color={currentSeat && currentSeat.some((s) => s === "governor" || s === "crypto_governor" || s === "auditor" || s === "crypto_auditor") ? "#10B981" : "#6B7280"}
                data-testid="authority-may-veto"
              >
                {currentSeat && currentSeat.some((s) => s === "governor" || s === "crypto_governor" || s === "auditor" || s === "crypto_auditor") ? "TRUE" : "FALSE"}
              </Badge>
            </div>
          </div>
        </SectionCard>
      </div>

      {/* Scorecard + Conflicts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
        <SectionCard
          icon={Trophy}
          title="Scorecard"
          sub="resolved-outcome calibration"
          testid="scorecard-section"
        >
          {!scorecard ? <LoadingRow /> : (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <MetricTile label="Resolved" value={summary.total_resolved ?? 0} testid="sc-total" />
              <MetricTile label="Wins"     value={summary.wins ?? 0} color="#10B981" testid="sc-wins" />
              <MetricTile label="Losses"   value={summary.losses ?? 0} color="#DC2626" testid="sc-losses" />
              <MetricTile
                label="Win rate"
                value={winRate != null ? `${winRate}%` : "—"}
                color={winRate != null && Number(winRate) >= 50 ? "#10B981" : "#F59E0B"}
                testid="sc-winrate"
              />
            </div>
          )}
        </SectionCard>

        <SectionCard
          icon={GitBranch}
          title="Conflicts"
          sub="adversarial disagreements involving this brain"
          testid="conflicts-section"
        >
          {!conflicts ? <LoadingRow /> :
            conflicts.length === 0 ? (
              <EmptyState>No conflicts on file.</EmptyState>
            ) : (
              <div data-testid="conflicts-list">
                {conflicts.map((c) => <ConflictRow key={c.conflict_id} c={c} />)}
              </div>
            )
          }
        </SectionCard>
      </div>

      {/* Discussion bus + Speak as form */}
      <SectionCard
        icon={ChatCircle}
        title="Discussion bus"
        sub={`Last 10 opinions from ${meta.label} into the cross-brain channel`}
        testid="discussion-bus-section"
      >
        {!opinions ? <LoadingRow /> :
          opinions.length === 0 ? (
            <EmptyState>No opinions on the bus yet.</EmptyState>
          ) : (
            <div data-testid="opinions-list">
              {opinions.map((op) => <OpinionRow key={op.opinion_id} op={op} />)}
            </div>
          )
        }
      </SectionCard>

      <SectionCard
        icon={Megaphone}
        title={`Speak as ${meta.label}`}
        sub="Operator override — admin-authenticated proxy post into the discussion bus. Stamped via admin_proxy in the audit trail."
        testid="speak-as-section"
      >
        <SpeakAsForm brain={brain} onPosted={reload} />
      </SectionCard>

      {/* Pending approvals section removed in pass #19 (2026-05-28).
          Per operator decision: "restrictions belong with the position
          not the brains." The promotion-ladder approval flow
          (challenger → co_trader → primary) is no longer an authority
          gate. Seat assignment IS the authority — see Quick Seat
          Switches on the Intents page. The backend
          /admin/promotion/proposals endpoint still exists for forensic
          audit but its output is not consumed on this page. */}
    </div>
  );
}
