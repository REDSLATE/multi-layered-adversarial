import React, { useCallback, useEffect, useState } from "react";
import { api, RUNTIME_META, fmtTime, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";
import { Eye, Flask, Lightning, Rocket, Buildings, CurrencyBtc, Warning } from "@phosphor-icons/react";
import { toast } from "sonner";

// Doctrine pin (2026-02-XX): this page is the operator's single
// place to flip a (brain, lane) along the LEARNING LADDER —
//   observation_only  → no broker fill, hypothesis-only
//   micro_paper       → paper fills @ LADDER_MICRO_PAPER_USD
//   micro_live        → real $ fills @ LADDER_MICRO_LIVE_USD
//   normal_live       → full sizing
//
// This is distinct from the AUTHORITY ladder on /admin/promotion
// (observer → … → primary), which governs WHO can speak.
// Learning ladder governs WHEN real capital deploys.
const STAGES = [
  {
    key: "observation_only",
    label: "Observation",
    short: "OBS",
    desc: "no broker fill · hypothesis only",
    color: "#A1A1AA",
    icon: Eye,
  },
  {
    key: "micro_paper",
    label: "Micro Paper",
    short: "PAPER",
    desc: "paper fills only · simulated",
    color: "#3B82F6",
    icon: Flask,
  },
  {
    key: "micro_live",
    label: "Micro Live",
    short: "LIVE",
    desc: "real $$ · capped at micro size",
    color: "#F59E0B",
    icon: Lightning,
  },
  {
    key: "normal_live",
    label: "Normal Live",
    short: "FULL",
    desc: "real $$ · full sizing",
    color: "#DC2626",
    icon: Rocket,
  },
];

const LANES = [
  { key: "equity", label: "Equity", icon: Buildings },
  { key: "crypto", label: "Crypto", icon: CurrencyBtc },
];

export default function LearningLadder() {
  const [data, setData] = useState(null);
  const [history, setHistory] = useState(null);
  const [err, setErr] = useState("");
  const [pending, setPending] = useState(null);  // {brain, lane, fromStage, toStage}
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(null);

  const refresh = useCallback(async () => {
    setErr("");
    try {
      const [d, h] = await Promise.all([
        api.get("/admin/learning-ladder"),
        api.get("/admin/learning-ladder/history?limit=30"),
      ]);
      setData(d.data);
      setHistory(h.data);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // requestChange opens the modal. We DON'T memoise it because eslint's
  // react-hooks/set-state-in-effect rule false-positives on a named
  // function that calls multiple setState's even when invoked from
  // onClick. Inlining the body into each onClick avoids the rule.

  async function submitChange() {
    if (!pending) return;
    const { brain, lane, toStage } = pending;
    if (!reason.trim()) {
      toast.error("Reason is required — every transition is audit-logged");
      return;
    }
    setBusy(`${brain}-${lane}`);
    try {
      const { data: resp } = await api.post("/admin/learning-ladder/set", {
        brain,
        lane,
        stage: toStage,
        reason: reason.trim(),
      });
      toast.success(
        resp.noop
          ? `${brain}/${lane} already at ${toStage}`
          : `${brain}/${lane}: ${resp.previous} → ${resp.current}`,
      );
      setPending(null);
      await refresh();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(null);
    }
  }

  // Group items by brain so each card shows both lanes side-by-side.
  const byBrain = {};
  for (const item of data?.items || []) {
    byBrain[item.brain] = byBrain[item.brain] || {};
    byBrain[item.brain][item.lane] = item;
  }

  return (
    <div className="reveal" data-testid="learning-ladder-page">
      <PageHeader
        eyebrow="Admin · Learning Ladder"
        title="Live-routing toggle"
        sub="Flip each brain × lane between hypothesis-only, paper fills, and real-money fills. Every transition is audit-logged with your reason. Default for all combinations is `observation_only` — no order touches Public.com or Kraken until you explicitly promote."
        right={<Badge color="#F59E0B"><Warning size={11} weight="bold" className="inline mr-1 -mt-0.5" />OPERATOR ONLY</Badge>}
        testid="learning-ladder-header"
      />

      {err && (
        <div
          className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono"
          data-testid="learning-ladder-error"
        >
          {err}
        </div>
      )}

      {/* Stage legend */}
      <Card className="mb-6" testid="stage-legend">
        <div className="label-eyebrow mb-3">Stages</div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
          {STAGES.map((s) => {
            const Icon = s.icon;
            return (
              <div
                key={s.key}
                className="border border-rd-border p-3"
                style={{ borderLeft: `3px solid ${s.color}` }}
                data-testid={`legend-${s.key}`}
              >
                <div className="flex items-center gap-2 mb-1">
                  <Icon size={14} weight="bold" style={{ color: s.color }} />
                  <span className="font-mono text-xs font-bold" style={{ color: s.color }}>
                    {s.short}
                  </span>
                  <span className="text-[10px] text-rd-dim uppercase tracking-widest ml-auto">
                    {s.label}
                  </span>
                </div>
                <div className="text-[11px] text-rd-muted font-mono leading-snug">
                  {s.desc}
                </div>
              </div>
            );
          })}
        </div>
      </Card>

      {!data && <LoadingRow />}

      {data && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6" data-testid="ladder-grid">
          {Object.keys(RUNTIME_META).map((brain) => {
            const meta = RUNTIME_META[brain];
            const lanes = byBrain[brain] || {};
            return (
              <Card
                key={brain}
                accentColor={meta.color}
                testid={`brain-card-${brain}`}
              >
                <div className="flex items-baseline justify-between mb-4">
                  <div>
                    <div
                      className="font-display text-2xl font-black tracking-tighter"
                      style={{ color: meta.color }}
                    >
                      {meta.label}
                    </div>
                    <div className="label-eyebrow mt-0.5">{meta.roleTagline}</div>
                  </div>
                </div>

                {LANES.map((lane) => {
                  const state = lanes[lane.key];
                  const curStage = state?.stage || "observation_only";
                  const isLive = curStage === "micro_live" || curStage === "normal_live";
                  const LaneIcon = lane.icon;
                  return (
                    <div
                      key={lane.key}
                      className="border border-rd-border p-3 mb-3 last:mb-0"
                      data-testid={`lane-row-${brain}-${lane.key}`}
                    >
                      <div className="flex items-center justify-between mb-3">
                        <div className="flex items-center gap-2">
                          <LaneIcon size={14} weight="bold" className="text-rd-muted" />
                          <span className="text-xs font-mono uppercase tracking-widest text-rd-text">
                            {lane.label}
                          </span>
                          {isLive && (
                            <Badge color="#DC2626">
                              REAL $$
                            </Badge>
                          )}
                        </div>
                        <div className="text-[10px] font-mono text-rd-dim">
                          {state?.updated_at
                            ? `${relTime(state.updated_at)} · ${state?.updated_by || "—"}`
                            : "default"}
                        </div>
                      </div>

                      {/* Toggle bar — 4 stage buttons */}
                      <div
                        className="grid grid-cols-4 gap-1"
                        data-testid={`toggle-${brain}-${lane.key}`}
                      >
                        {STAGES.map((s) => {
                          const isCurrent = s.key === curStage;
                          return (
                            <button
                              key={s.key}
                              onClick={() => {
                                if (s.key === curStage) return;
                                setReason("");
                                setPending({
                                  brain,
                                  lane: lane.key,
                                  fromStage: curStage,
                                  toStage: s.key,
                                });
                              }}
                              disabled={busy === `${brain}-${lane.key}` || isCurrent}
                              className="btn-sharp px-2 py-2 border text-[10px] font-mono uppercase tracking-widest disabled:cursor-default transition-colors"
                              style={{
                                background: isCurrent ? s.color : "transparent",
                                color: isCurrent ? "#0A0A0A" : s.color,
                                borderColor: s.color,
                                opacity: busy === `${brain}-${lane.key}` && !isCurrent ? 0.3 : 1,
                                fontWeight: isCurrent ? 800 : 600,
                              }}
                              data-testid={`stage-btn-${brain}-${lane.key}-${s.key}`}
                              title={s.desc}
                            >
                              {s.short}
                            </button>
                          );
                        })}
                      </div>

                      {/* Inline note when about to leave observation */}
                      {curStage === "observation_only" && (
                        <div className="text-[10px] text-rd-dim font-mono mt-2 leading-snug">
                          Safe state · no broker call. Click PAPER to enable
                          simulated fills, or LIVE for real-money execution
                          (subject to per-order / per-day caps).
                        </div>
                      )}
                    </div>
                  );
                })}
              </Card>
            );
          })}
        </div>
      )}

      {/* History */}
      <Card className="p-0 overflow-hidden mb-6" testid="ladder-history">
        <div className="px-4 py-3 border-b border-rd-border">
          <div className="label-eyebrow">Transition history</div>
          <div className="font-mono text-sm">last 30 stage changes (audit log)</div>
        </div>
        {!history && <LoadingRow />}
        {history && history.items.length === 0 && (
          <EmptyState message="No transitions yet — every brain × lane is at default (observation_only)." />
        )}
        {history && history.items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-xs font-mono">
              <thead>
                <tr className="bg-rd-bg3 text-rd-dim uppercase tracking-widest">
                  <th className="text-left px-4 py-3 border-b border-rd-border">Time</th>
                  <th className="text-left px-4 py-3 border-b border-rd-border">Brain</th>
                  <th className="text-left px-4 py-3 border-b border-rd-border">Lane</th>
                  <th className="text-left px-4 py-3 border-b border-rd-border">From → To</th>
                  <th className="text-left px-4 py-3 border-b border-rd-border">Actor</th>
                  <th className="text-left px-4 py-3 border-b border-rd-border">Reason</th>
                </tr>
              </thead>
              <tbody>
                {history.items.map((row, idx) => {
                  const meta = RUNTIME_META[row.brain] || { label: row.brain, color: "#A1A1AA" };
                  const fromS = STAGES.find((s) => s.key === row.previous);
                  const toS = STAGES.find((s) => s.key === row.next);
                  return (
                    <tr
                      key={`${row.ts}-${idx}`}
                      className="border-b border-rd-border last:border-b-0 hover:bg-rd-bg3"
                      data-testid={`history-row-${idx}`}
                    >
                      <td className="px-4 py-2.5 text-rd-muted whitespace-nowrap">{fmtTime(row.ts)}</td>
                      <td className="px-4 py-2.5">
                        <span style={{ color: meta.color }} className="font-bold">{meta.label}</span>
                      </td>
                      <td className="px-4 py-2.5 text-rd-muted">{row.lane}</td>
                      <td className="px-4 py-2.5">
                        <span style={{ color: fromS?.color }}>{fromS?.short || row.previous}</span>
                        <span className="text-rd-dim mx-2">→</span>
                        <span style={{ color: toS?.color }} className="font-bold">{toS?.short || row.next}</span>
                      </td>
                      <td className="px-4 py-2.5 text-rd-muted truncate max-w-[180px]" title={row.actor}>{row.actor}</td>
                      <td className="px-4 py-2.5 text-rd-muted truncate max-w-[280px]" title={row.reason}>{row.reason}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Confirmation modal */}
      {pending && (
        <ChangeModal
          pending={pending}
          reason={reason}
          onReason={setReason}
          onCancel={() => setPending(null)}
          onSubmit={submitChange}
          busy={busy === `${pending.brain}-${pending.lane}`}
        />
      )}
    </div>
  );
}

function ChangeModal({ pending, reason, onReason, onCancel, onSubmit, busy }) {
  const { brain, lane, fromStage, toStage } = pending;
  const fromS = STAGES.find((s) => s.key === fromStage);
  const toS = STAGES.find((s) => s.key === toStage);
  const meta = RUNTIME_META[brain] || { label: brain, color: "#A1A1AA" };
  const goingLive = toStage === "micro_live" || toStage === "normal_live";
  const isDemote = STAGES.findIndex((s) => s.key === toStage) < STAGES.findIndex((s) => s.key === fromStage);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
      data-testid="ladder-modal"
      onClick={onCancel}
    >
      <div
        className="bg-rd-bg border border-rd-border w-full max-w-lg font-mono"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-3 py-2 border-b border-rd-border text-xs text-rd-muted flex items-center justify-between">
          <span>
            <span style={{ color: meta.color }} className="font-bold">{meta.label}</span>
            <span className="text-rd-dim mx-1">·</span>
            <span className="uppercase">{lane}</span>
          </span>
          <span>
            <span style={{ color: fromS?.color }}>{fromS?.short}</span>
            <span className="text-rd-dim mx-2">→</span>
            <span style={{ color: toS?.color }} className="font-bold">{toS?.short}</span>
          </span>
        </div>

        {goingLive && (
          <div className="mx-3 mt-3 border border-rd-danger bg-rd-danger/10 p-3 text-[11px] text-rd-danger leading-snug">
            <Warning size={14} weight="bold" className="inline mr-1 -mt-0.5" />
            <strong>Live execution.</strong> Orders from this brain × lane
            will hit {lane === "equity" ? "Public.com (real $)" : "Kraken (real $)"} subject to
            $25 / order · $50 / day · $200 open caps. Demote at any time on this page.
          </div>
        )}
        {isDemote && (
          <div className="mx-3 mt-3 border border-rd-chevelle bg-rd-chevelle/10 p-3 text-[11px] text-rd-chevelle leading-snug">
            <Warning size={14} weight="bold" className="inline mr-1 -mt-0.5" />
            <strong>Safety demote.</strong> Lowering authority — always allowed.
          </div>
        )}

        <div className="p-3">
          <label className="label-eyebrow block mb-1.5">Reason (required, audit-logged)</label>
          <textarea
            value={reason}
            onChange={(e) => onReason(e.target.value)}
            placeholder={goingLive ? "Why are we going live? (e.g., $500 pilot day 1)" : "Why this change?"}
            rows={4}
            autoFocus
            className="w-full bg-black border border-rd-border focus:border-rd-text focus:outline-none p-2 text-sm text-rd-text placeholder:text-rd-dim resize-y"
            data-testid="ladder-modal-reason"
          />
        </div>

        <div className="px-3 py-2 border-t border-rd-border flex justify-end gap-2 text-xs">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 border border-rd-border text-rd-muted hover:text-rd-text"
            data-testid="ladder-modal-cancel"
          >
            Cancel
          </button>
          <button
            onClick={onSubmit}
            disabled={busy || !reason.trim()}
            className="px-3 py-1.5 border text-rd-text hover:bg-rd-text hover:text-black disabled:opacity-40 disabled:cursor-not-allowed"
            style={{ borderColor: toS?.color || "#fff" }}
            data-testid="ladder-modal-submit"
          >
            {busy ? "..." : goingLive ? "Confirm — go live" : "Confirm"}
          </button>
        </div>
      </div>
    </div>
  );
}
