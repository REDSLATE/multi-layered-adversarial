import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, RUNTIME_META, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";
import SeatRosterStrip from "@/components/SeatRosterStrip";
import QuickSeatSwitches from "@/components/QuickSeatSwitches";
import PublicConnect from "@/components/PublicConnect";
import KrakenBrokerTile from "@/components/KrakenBrokerTile";
import LaneRoutingPill from "@/components/LaneRoutingPill";
import MasterTradingSwitch from "@/components/MasterTradingSwitch";
import WebullEntitlementsCard from "@/components/WebullEntitlementsCard";
import WebullOtocoTestPanel from "@/components/WebullOtocoTestPanel";
import WebullOtocoLivePanel from "@/components/WebullOtocoLivePanel";
import LegacyWrapperTogglePanel from "@/components/LegacyWrapperTogglePanel";
import IntentPostMortemPanel from "@/components/IntentPostMortemPanel";
import ParabolicPhaseStrip from "@/components/ParabolicPhaseStrip";
import BrokerSelectionMenu from "@/components/BrokerSelectionMenu";
import DoctrineStrip from "@/components/DoctrineStrip";
import AutoRetireStrip from "@/components/AutoRetireStrip";
import DoctrineHealthPanel from "@/components/DoctrineHealthPanel";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";
import SeatRegistryDriftBanner from "@/components/SeatRegistryDriftBanner";
import OperatorInjectIntent from "@/components/OperatorInjectIntent";
import SubmitOrderModal from "@/components/SubmitOrderModal";
import { toast } from "sonner";
import {
  Lightning, ArrowsClockwise, Funnel, Pulse,
  CheckCircle, XCircle, Hourglass, Eye, CaretDown, CaretUp, Rocket,
  CurrencyBtc, Buildings,
} from "@phosphor-icons/react";

const BRAIN_META = {
  ...RUNTIME_META,
  redeye: { label: "REDEYE", color: "#DC2626" },
};

const ACTION_COLOR = {
  BUY: "#10B981",
  SELL: "#DC2626",
  SHORT: "#A78BFA",
  COVER: "#3B82F6",
  HOLD: "#A1A1AA",
};

const GATE_COLOR = {
  pending: "#A1A1AA",
  passed: "#10B981",
  blocked: "#DC2626",
  dry_run_passed: "#10B981",
  dry_run_blocked: "#F59E0B",
};

const GATE_ICON = {
  pending: Hourglass,
  passed: CheckCircle,
  blocked: XCircle,
  dry_run_passed: CheckCircle,
  dry_run_blocked: XCircle,
};

const STACKS = ["all", "alpha", "camaro", "chevelle", "redeye"];
const ACTIONS = ["all", "BUY", "SELL", "SHORT", "COVER", "HOLD"];
const LANES = ["all", "equity", "crypto"];
const GATE_STATES = ["all", "pending", "passed", "blocked", "dry_run_passed", "dry_run_blocked", "rejected_at_ingest"];

function SectionDivider({ title, sub, icon: IconComponent, testid, rightSlot }) {
  return (
    <div
      className="mt-6 mb-3 flex items-baseline gap-3 border-t border-rd-border pt-4"
      data-testid={testid}
    >
      {IconComponent && (
        <IconComponent size={14} weight="bold" className="text-rd-text shrink-0" />
      )}
      <div className="min-w-0 flex-1">
        <div className="text-[11px] font-mono uppercase tracking-[0.25em] text-rd-text">
          {title}
        </div>
        {sub && (
          <div className="text-[10px] font-mono text-rd-dim mt-1 leading-relaxed">
            {sub}
          </div>
        )}
      </div>
      {rightSlot && (
        <div className="shrink-0 self-baseline">{rightSlot}</div>
      )}
    </div>
  );
}

function FilterPill({ label, value, options, onChange, testid }) {
  return (
    <div className="flex items-center gap-1.5" data-testid={testid}>
      <span className="text-[10px] uppercase tracking-widest text-rd-dim">{label}</span>
      <div className="flex rounded-sm border border-rd-border bg-rd-bg p-0.5">
        {options.map((opt) => {
          const active = opt === value;
          return (
            <button
              key={opt}
              onClick={() => onChange(opt)}
              data-testid={`${testid}-${opt}`}
              className={
                "px-2 py-1 text-[10px] font-mono uppercase tracking-wide rounded-sm transition-colors " +
                (active
                  ? "bg-rd-accent text-black"
                  : "text-rd-dim hover:text-rd-text")
              }
            >
              {opt}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function StatTile({ label, value, color, testid }) {  return (
    <div
      className="border border-rd-border bg-rd-bg p-3"
      data-testid={testid}
    >
      <div className="flex items-center gap-2 mb-1.5">
        <span
          className="inline-block w-1.5 h-1.5 rounded-full"
          style={{ background: color || "#A1A1AA" }}
        />
        <span className="text-[10px] uppercase tracking-widest text-rd-dim">{label}</span>
      </div>
      <div className="font-display text-xl font-bold text-rd-text leading-none">{value}</div>
    </div>
  );
}

function IntentRow({ intent, expanded, onToggle, onDryRun, onSubmit, dryRunResult, submitResult }) {
  const meta = BRAIN_META[intent.stack] || { label: intent.stack, color: "#A1A1AA" };
  const GateIcon = GATE_ICON[intent.gate_state] || Hourglass;
  const gateColor = GATE_COLOR[intent.gate_state] || "#A1A1AA";
  const actionColor = ACTION_COLOR[intent.action] || "#A1A1AA";
  const isExecuted = intent.executed === true;
  const submitEligible = !isExecuted && (intent.gate_state === "dry_run_passed" || intent.gate_state === "passed");

  return (
    <>
      <tr
        className="border-b border-rd-border hover:bg-rd-bg cursor-pointer transition-colors"
        onClick={onToggle}
        data-testid={`intent-row-${intent.intent_id}`}
      >
        <td className="px-3 py-2 font-mono text-[10px] text-rd-muted whitespace-nowrap">
          {relTime(intent.ingest_ts)}
        </td>
        <td className="px-3 py-2">
          <Badge color={meta.color}>{meta.label}</Badge>
        </td>
        <td className="px-3 py-2 font-display text-sm text-rd-text">{intent.symbol}</td>
        <td className="px-3 py-2">
          {intent.lane ? (
            <Badge color={intent.lane === "crypto" ? "#A855F7" : "#3B82F6"}>
              {intent.lane.toUpperCase()}
            </Badge>
          ) : (
            <span className="font-mono text-[10px] text-rd-dim">—</span>
          )}
        </td>
        <td className="px-3 py-2">
          <span
            className="font-mono text-[11px] font-bold tracking-wider"
            style={{ color: actionColor }}
          >
            {intent.action}
          </span>
        </td>
        <td className="px-3 py-2 font-mono text-xs text-rd-text">
          {Number(intent.confidence).toFixed(3)}
        </td>
        <td className="px-3 py-2 font-mono text-xs text-rd-text">
          {Number(intent.risk_multiplier).toFixed(3)}
        </td>
        <td className="px-3 py-2">
          <span
            className="inline-flex items-center gap-1 font-mono text-[10px] uppercase tracking-wider"
            style={{ color: gateColor }}
          >
            <GateIcon size={11} weight="bold" />
            {intent.gate_state}
          </span>
        </td>
        <td className="px-3 py-2 text-right">
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={(e) => { e.stopPropagation(); onDryRun(); }}
              data-testid={`intent-dryrun-${intent.intent_id}`}
              className="px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text"
              title="Run gate chain against this intent (no broker call)"
            >
              <Lightning size={10} weight="bold" className="inline mr-1" />
              dry-run
            </button>
            {isExecuted ? (
              <span
                className="px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-rd-success text-rd-success"
                data-testid={`intent-executed-${intent.intent_id}`}
                title="Already executed"
              >
                <CheckCircle size={10} weight="bold" className="inline mr-1" />
                executed
              </span>
            ) : submitEligible ? (
              <button
                onClick={(e) => { e.stopPropagation(); onSubmit(); }}
                data-testid={`intent-submit-${intent.intent_id}`}
                className="px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-rd-accent text-rd-accent hover:bg-rd-accent hover:text-black"
                title="Route this intent to the broker"
              >
                <Rocket size={10} weight="bold" className="inline mr-1" />
                submit
              </button>
            ) : null}
            {expanded ? (
              <CaretUp size={12} weight="bold" className="text-rd-dim" />
            ) : (
              <CaretDown size={12} weight="bold" className="text-rd-dim" />
            )}
          </div>
        </td>
      </tr>
      {intent.doctrine_packet && (
        <tr
          className="border-b border-rd-border"
          data-testid={`intent-doctrine-row-${intent.intent_id}`}
        >
          <td colSpan={9} className="p-0">
            <PanelErrorBoundary
              panelName="Doctrine"
              testid={`panel-error-doctrine-${intent.intent_id}`}
              compact
            >
              <DoctrineStrip
                packet={intent.doctrine_packet}
                intentId={intent.intent_id}
              />
            </PanelErrorBoundary>
          </td>
        </tr>
      )}
      {expanded && (
        <tr className="bg-rd-bg" data-testid={`intent-detail-${intent.intent_id}`}>
          <td colSpan={9} className="px-3 py-4">
            <div className="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-6">
              <div>
                <div className="label-eyebrow mb-2">Rationale</div>
                <p className="text-xs text-rd-text leading-relaxed font-mono whitespace-pre-wrap break-words">
                  {intent.rationale || "—"}
                </p>
                {intent.evidence && Object.keys(intent.evidence).length > 0 && (
                  <>
                    <div className="label-eyebrow mt-4 mb-2">Evidence</div>
                    <pre className="text-[11px] text-rd-text font-mono bg-rd-bg2 border border-rd-border p-3 overflow-x-auto leading-relaxed">
                      {JSON.stringify(intent.evidence, null, 2)}
                    </pre>
                  </>
                )}
                {dryRunResult && (
                  <>
                    <div className="label-eyebrow mt-4 mb-2">
                      Dry-run verdict ·{" "}
                      <span style={{
                        color: dryRunResult.verdict === "would_pass" ? "#10B981" : "#F59E0B",
                      }}>
                        {dryRunResult.verdict?.replace("_", " ")?.toUpperCase()}
                      </span>
                    </div>
                    <div className="border border-rd-border bg-rd-bg2 divide-y divide-rd-border">
                      {(dryRunResult.gates || []).map((g) => (
                        <div key={g.name} className="px-3 py-2 flex items-start gap-3" data-testid={`gate-${g.name}`}>
                          {g.passed ? (
                            <CheckCircle size={13} weight="bold" className="text-rd-success mt-0.5 shrink-0" />
                          ) : (
                            <XCircle size={13} weight="bold" className="text-rd-danger mt-0.5 shrink-0" />
                          )}
                          <div className="flex-1 min-w-0">
                            <div className="font-mono text-[11px] text-rd-text">{g.name}</div>
                            <div className="text-[10px] text-rd-muted leading-relaxed mt-0.5">{g.reason}</div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </>
                )}
                {submitResult && (
                  <>
                    <div className="label-eyebrow mt-4 mb-2">
                      Submit ·{" "}
                      <span style={{ color: submitResult.error ? "#DC2626" : "#10B981" }}>
                        {submitResult.error ? "BLOCKED / ERROR" : "EXECUTED"}
                      </span>
                    </div>
                    {submitResult.error ? (
                      (() => {
                        const err = submitResult.error;
                        const isObj = err && typeof err === "object";
                        const blockedBy = isObj ? err.blocked_by : null;
                        const reason = isObj ? err.reason : null;
                        const gates = isObj ? err.gates : null;
                        const failingGates = Array.isArray(gates)
                          ? gates.filter((g) => g && g.passed === false)
                          : [];
                        return (
                          <div
                            className="border border-rd-danger bg-rd-danger/5 px-3 py-2 text-[11px] font-mono space-y-2"
                            data-testid={`submit-error-${intent.intent_id}`}
                          >
                            <div className="text-rd-danger">
                              {blockedBy ? (
                                <>
                                  <span className="font-bold">blocked_by</span>{" "}
                                  <span className="text-rd-text">{blockedBy}</span>
                                  {submitResult.status ? (
                                    <span className="text-rd-dim ml-2">· HTTP {submitResult.status}</span>
                                  ) : null}
                                </>
                              ) : (
                                <span>{typeof err === "string" ? err : JSON.stringify(err)}</span>
                              )}
                            </div>
                            {reason && (
                              <div className="text-rd-text leading-relaxed">{reason}</div>
                            )}
                            {failingGates.length > 0 && (
                              <div className="border border-rd-border bg-rd-bg2 divide-y divide-rd-border">
                                {failingGates.map((g) => (
                                  <div
                                    key={g.name}
                                    className="px-2 py-1.5 flex items-start gap-2"
                                    data-testid={`submit-failing-gate-${g.name}`}
                                  >
                                    <XCircle size={12} weight="bold" className="text-rd-danger mt-0.5 shrink-0" />
                                    <div className="flex-1 min-w-0">
                                      <div className="text-rd-text">{g.name}</div>
                                      <div className="text-[10px] text-rd-dim leading-relaxed mt-0.5">{g.reason}</div>
                                    </div>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        );
                      })()
                    ) : (
                      <div className="border border-rd-success bg-rd-bg2 p-3 text-[11px] font-mono space-y-1" data-testid={`submit-receipt-${intent.intent_id}`}>
                        <div><span className="text-rd-dim">broker_order_id</span> <span className="text-rd-text">{submitResult.receipt?.broker_order_id}</span></div>
                        <div><span className="text-rd-dim">side · notional</span> <span className="text-rd-text">{submitResult.receipt?.side} · ${Number(submitResult.receipt?.notional_usd).toFixed(2)}</span></div>
                        <div><span className="text-rd-dim">status</span> <span className="text-rd-text">{submitResult.order?.status}</span></div>
                        <div><span className="text-rd-dim">executed_at</span> <span className="text-rd-text">{submitResult.receipt?.executed_at}</span></div>
                        {submitResult.receipt?.action_overridden && (
                          <div className="text-rd-accent">
                            <span className="text-rd-dim">action_override</span>{" "}
                            <span>{submitResult.receipt?.original_action} → {submitResult.receipt?.action}</span>
                          </div>
                        )}
                        {submitResult.receipt?.operator_override && (
                          <div className="text-rd-accent">
                            <span className="text-rd-dim">operator_override</span>{" "}
                            <span>
                              {(submitResult.receipt?.overridden_gate_names || []).length} gates bypassed · {submitResult.receipt?.override_reason}
                            </span>
                          </div>
                        )}
                      </div>
                    )}
                  </>
                )}
              </div>
              <div className="text-[11px] font-mono space-y-2">
                <div className="label-eyebrow mb-2">Stamped by MC</div>
                {[
                  ["intent_id", intent.intent_id],
                  ["seat_at_post_time", intent.seat_at_post_time || "—"],
                  ["ingest_method", intent.ingest_method || "—"],
                  ["regime", intent.regime || "—"],
                  ["decision_id", intent.decision_id || "—"],
                  ["may_execute", String(intent.may_execute)],
                  ["requires_gate_pass", String(intent.requires_gate_pass)],
                  ["executed", String(intent.executed)],
                ].map(([k, v]) => (
                  <div key={k} className="flex justify-between gap-2 border-b border-rd-border pb-1">
                    <span className="text-rd-dim">{k}</span>
                    <span className="text-rd-text truncate" title={String(v)}>{String(v)}</span>
                  </div>
                ))}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export default function Intents() {
  const [intents, setIntents] = useState(null);
  const [err, setErr] = useState("");
  const [stack, setStack] = useState("all");
  const [action, setAction] = useState("all");
  const [gateState, setGateState] = useState("all");
  const [lane, setLane] = useState("all");
  const [expanded, setExpanded] = useState(null);
  const [dryRunByIntent, setDryRunByIntent] = useState({});
  const [submitByIntent, setSubmitByIntent] = useState({});
  const [autoRefresh, setAutoRefresh] = useState(true);
  // Submit modal — replaces the legacy prompt+confirm pair. Carries
  // notional, BUY/SELL toggle, and operator override flag/reason.
  const [submitModal, setSubmitModal] = useState({ open: false, intent: null });
  // Single source of truth for caps — fetched on mount, refreshed on
  // submit so any cap tuning the operator just made is reflected.
  // Shape: { per_order_usd, per_day_usd, open_notional_usd, per_order_by_lane_usd: { <lane>: cap } }
  const [caps, setCaps] = useState(null);

  const loadCaps = useCallback(async () => {
    try {
      const res = await api.get("/config/exposure-caps");
      setCaps(res.data || null);
    } catch (e) {
      // Caps endpoint unavailable — submit will fall back to a safe
      // hardcoded minimum and warn.
      console.warn("exposure-caps fetch failed:", e?.message);
    }
  }, []);

  useEffect(() => { loadCaps(); }, [loadCaps]);

  // Resolve effective per-order cap for an intent's lane.
  const capForLane = useCallback((lane) => {
    if (!caps) return null;
    const byLane = caps.per_order_by_lane_usd || {};
    if (lane && byLane[lane] != null) return Number(byLane[lane]);
    return Number(caps.per_order_usd);
  }, [caps]);

  const load = useCallback(async () => {
    try {
      const params = { limit: 100 };
      if (stack !== "all") params.stack = stack;
      if (gateState !== "all") params.gate_state = gateState;
      if (lane !== "all") params.lane = lane;
      // action filter happens client-side since the API doesn't expose it
      const res = await api.get("/intents", {
        params,
        // Need any runtime token to read intents; use alpha by convention
        // since admin JWT alone isn't accepted on this endpoint.
        headers: {
          "X-Runtime-Token": "alpha-ingest-2cf91b5e-3a44-4c1b-9e07-4e1b7d2c3a55",
        },
      });
      setIntents(res.data?.items || []);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, [stack, gateState, lane]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!autoRefresh) return;
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, [autoRefresh, load]);

  const filtered = useMemo(() => {
    if (!intents) return null;
    if (action === "all") return intents;
    return intents.filter((it) => it.action === action);
  }, [intents, action]);

  const stats = useMemo(() => {
    if (!filtered) return null;
    const byStack = {};
    const byGate = {};
    const byAction = {};
    for (const it of filtered) {
      byStack[it.stack] = (byStack[it.stack] || 0) + 1;
      byGate[it.gate_state] = (byGate[it.gate_state] || 0) + 1;
      byAction[it.action] = (byAction[it.action] || 0) + 1;
    }
    return { total: filtered.length, byStack, byGate, byAction };
  }, [filtered]);

  const runDryRun = async (intentId) => {
    setDryRunByIntent((m) => ({ ...m, [intentId]: { loading: true } }));
    try {
      const res = await api.post(`/execution/dry_run?intent_id=${encodeURIComponent(intentId)}`);
      setDryRunByIntent((m) => ({ ...m, [intentId]: res.data }));
      setExpanded(intentId);
      // refresh to pick up gate_state change
      load();
    } catch (e) {
      setDryRunByIntent((m) => ({
        ...m,
        [intentId]: { error: e?.response?.data?.detail || e.message },
      }));
    }
  };

  const runSubmit = async (intentId, lane) => {
    // Always re-fetch caps right before opening the modal so the
    // operator sees the current truth (the cap may have been retuned
    // since page-load).
    await loadCaps();
    const cap = capForLane(lane);
    if (cap == null) {
      toast.error("Cap config unavailable — refresh the page and retry.");
      return;
    }
    const intent = (intents || []).find((it) => it.intent_id === intentId);
    if (!intent) {
      toast.error("Intent vanished from the table — reload and retry.");
      return;
    }
    setSubmitModal({ open: true, intent });
  };

  const performSubmit = async (payload) => {
    const intentId = submitModal.intent?.intent_id;
    if (!intentId) return;
    setSubmitByIntent((m) => ({ ...m, [intentId]: { loading: true } }));
    setExpanded(intentId);
    setSubmitModal({ open: false, intent: null });
    try {
      const body = {
        intent_id: intentId,
        order_notional_usd: payload.order_notional_usd,
        confirm: "execute",
      };
      if (payload.operator_override) {
        body.operator_override = true;
        body.override_reason = payload.override_reason;
      }
      if (payload.action_override) {
        body.action_override = payload.action_override;
      }
      const res = await api.post("/execution/submit", body);
      setSubmitByIntent((m) => ({ ...m, [intentId]: res.data }));
      const sideLabel = payload.action_override || submitModal.intent?.action || "";
      const overrideTag = payload.operator_override ? " · OVERRIDE" : "";
      toast.success(
        `Order routed · ${sideLabel} $${payload.order_notional_usd.toFixed(2)}${overrideTag} · ${res.data?.order?.status || "submitted"}`,
      );
      load();
    } catch (e) {
      const status = e?.response?.status;
      let detail = e?.response?.data?.detail;
      // ── Prod proxy fallback (2026-02-19, P1) ──
      // Some proxies (Cloudflare, ingress configs that strip 4xx
      // bodies for security) silently drop the response body on 403,
      // leaving `detail` undefined and the operator staring at a bare
      // "HTTP 403". MC persists every submit block to
      // `shared_gate_results`; fetch that audit row to recover the
      // structured `blocked_by`/`reason`/`gates` payload so the UI
      // can render the same detail it would have shown inline.
      let auditFetchFailed = false;
      let auditFetchStatus = null;
      if (!detail || typeof detail === "string") {
        try {
          const audit = await api.get(
            `/execution/last-submit-block?intent_id=${encodeURIComponent(intentId)}`,
          );
          if (audit?.data && audit.data.blocked_by) {
            detail = {
              blocked_by: audit.data.blocked_by,
              reason: audit.data.reason,
              gates: audit.data.gates,
              kind: audit.data.kind,
              _from_audit: true,
            };
          } else if (audit?.data) {
            // 2026-02-19 (rev2): audit row exists but is missing
            // `blocked_by` — surface what we DO have rather than
            // leaving the red bar blank.
            detail = {
              blocked_by: audit.data.kind || "unknown",
              reason:
                audit.data.reason ||
                "audit row found but no reason/gate detail was recorded; check backend logs around " +
                  (audit.data.ts || "submit time"),
              gates: audit.data.gates || [],
              _from_audit: true,
              _audit_partial: true,
            };
          }
        } catch (auditErr) {
          // 2026-02-19 (rev2): if the fallback ALSO fails, capture
          // the status so the UI can tell the operator the audit
          // pipeline itself is broken (vs. a real 403 with no body).
          auditFetchFailed = true;
          auditFetchStatus = auditErr?.response?.status || null;
        }
      }
      // Guarantee the red bar has SOMETHING to render — never blank.
      if (!detail) {
        detail = {
          blocked_by: `http_${status || "unknown"}`,
          reason: auditFetchFailed
            ? `Submit returned HTTP ${status || "?"} with no body, and the audit ` +
              `fallback also failed (HTTP ${auditFetchStatus || "?"}). The block ` +
              `reason was not persisted server-side — check backend logs for ` +
              `intent ${intentId.slice(0, 8)}.`
            : `Submit returned HTTP ${status || "?"} with no body and no audit ` +
              `row was found. The request likely never reached the gate chain ` +
              `(network / proxy / auth). Check backend logs for intent ` +
              `${intentId.slice(0, 8)}.`,
          gates: [],
          _from_audit: false,
          _audit_missing: true,
        };
      }
      setSubmitByIntent((m) => ({
        ...m,
        [intentId]: { error: detail, status },
      }));
      const shortReason = typeof detail === "string"
        ? detail
        : (detail?.blocked_by ? `${detail.blocked_by}: ${detail.reason}` : (detail?.reason || `HTTP ${status || "?"}`));
      toast.error(shortReason);
    }
  };

  return (
    <div className="reveal" data-testid="intents-page">
      <PageHeader
        eyebrow="Decision Machine"
        title="Intents"
        sub="Intent envelopes emitted by the four brains. Every intent is a candidate; MC's gate chain decides if it lives. Schema pins may_execute=false and requires_gate_pass=true — brains cannot route an order through this surface."
        right={
          <div className="flex items-center gap-2">
            <OperatorInjectIntent onSubmitted={load} />
            <button
              onClick={() => setAutoRefresh((v) => !v)}
              data-testid="intents-autorefresh"
              className={
                "px-2 py-1 text-[10px] font-mono uppercase tracking-wider border " +
                (autoRefresh
                  ? "border-rd-success text-rd-success"
                  : "border-rd-border text-rd-dim")
              }
              title="Auto-refresh every 8s"
            >
              <Pulse size={10} weight="bold" className="inline mr-1" />
              {autoRefresh ? "live · 8s" : "paused"}
            </button>
            <button
              onClick={load}
              data-testid="intents-reload"
              className="p-1.5 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text"
              title="Reload now"
            >
              <ArrowsClockwise size={11} weight="bold" />
            </button>
          </div>
        }
        testid="intents-header"
      />

      {/* ─── Seat registry drift banner (2026-02-17, pass #48) ───
          Surfaces any roster ↔ legacy executor_seat mismatch (or a
          vacant execute-seat in any lane) so the operator sees the
          desync immediately — instead of days of executor_seat_check
          blocks piling up under a stale legacy holder. Read-only;
          polls /admin/seat-registry/diagnose every 30s. */}
      <SeatRegistryDriftBanner />

      {/* ─── Why are we not trading? (2026-02-19, prod-incident smoking gun) ───
          Single tile that answers the operator's persistent question:
          which gate/layer is killing intents and how often. Surfaces
          the dominant failure mode + top blockers ranked by frequency
          so we stop guessing. Always visible at the top — this is
          the single most important panel for diagnosing prod. */}
      <div className="mb-4" data-testid="intents-post-mortem-mount">
        <PanelErrorBoundary label="Intent Post-Mortem">
          <IntentPostMortemPanel />
        </PanelErrorBoundary>
      </div>

      {/* ─── Seat Roster strip (2026-05-27, pass #15) ───
          All four seats per lane in one view + freshness of each
          brain's last opinion/sovereign-contribution. Surfaces the
          gap between "heartbeating" and "actually contributing" so
          the operator can tell at a glance when MC is showing
          doctrine-fallback values instead of real brain voices. */}
      <div className="mb-4" data-testid="intents-seat-roster-mount">
        <PanelErrorBoundary label="Seat Roster">
          <SeatRosterStrip />
        </PanelErrorBoundary>
      </div>

      {/* ─── Quick Seat Switches (2026-05-27, pass #17) ───
          One-click seat assignment for all 4 seats per lane. Optional
          reason field. Uses /api/admin/roster/assign — same backend
          path as the full RosterPanel, but compact and inline so the
          operator can react to the SeatRosterStrip's freshness chips
          without navigating away. */}
      <div className="mb-4" data-testid="intents-quick-switches-mount">
        <PanelErrorBoundary label="Quick Seat Switches">
          <QuickSeatSwitches />
        </PanelErrorBoundary>
      </div>

      {/* ─── Twin authority lanes ─── Doctrine: equity and crypto are
          symmetric. Each lane has its own broker tile and exposure
          caps. Seat assignment for all 4 seats × 2 lanes is handled
          by the QuickSeatSwitches panel above — the legacy per-seat
          dedicated tiles (ExecutorSeatTile, AuditorSeatTile,
          RosterSeatTile) were removed 2026-05-27 (pass #18) per
          operator request to eliminate redundant assignment surfaces. */}
      <SectionDivider
        title="Master Trading Switch"
        icon={Buildings}
        sub="Global kill switch. When OFF, the auto-router short-circuits on every tick regardless of lane / seat / broker state. OFF is one-click; ON requires confirmation + reason."
        testid="intents-section-master-switch"
      />
      <MasterTradingSwitch />

      <SectionDivider
        title="Equity Lane"
        icon={Buildings}
        sub="Webull-routed equity execution. Public.com and Alpaca are deprecated. Seat assignment lives in Quick Seat Switches above."
        testid="intents-section-equity"
        rightSlot={<LaneRoutingPill lane="equity" />}
      />
      <WebullEntitlementsCard />

      {/* Atomic OTOCO bracket — P1 Phase 2 (2026-02-19). Whole-share
          only; operator-driven so we can observe Webull's combo
          lifecycle before wiring this into the auto-router. */}
      <div className="mt-3">
        <PanelErrorBoundary panelName="Webull OTOCO" testid="panel-error-otoco">
          <WebullOtocoTestPanel />
        </PanelErrorBoundary>
      </div>

      {/* Live OTOCO tile — polls Webull's v3 open-orders API and
          groups the rows into bracket envelopes so the operator can
          watch the MASTER + TP + SL legs without opening the Webull
          mobile app. */}
      <div className="mt-3">
        <PanelErrorBoundary panelName="Webull OTOCO Live" testid="panel-error-otoco-live">
          <WebullOtocoLivePanel />
        </PanelErrorBoundary>
      </div>

      {/* Legacy wrapper A/B switch — per-brain toggle so the operator
          can confirm whether the penalty-stacking wrappers are
          compressing size_bias and causing 403/502 cascades. */}
      <div className="mt-3">
        <PanelErrorBoundary panelName="Legacy Wrapper Toggle" testid="panel-error-wrapper-toggle">
          <LegacyWrapperTogglePanel />
        </PanelErrorBoundary>
      </div>

      <SectionDivider
        title="Crypto Lane"
        icon={CurrencyBtc}
        sub="Kraken-routed crypto execution. Seat assignment lives in Quick Seat Switches above. All crypto seats empty by default — operator must assign before any crypto trade can fire."
        testid="intents-section-crypto"
        rightSlot={<LaneRoutingPill lane="crypto" />}
      />
      <KrakenBrokerTile />
      <div className="mt-3">
        <BrokerSelectionMenu />
      </div>
      <div className="mt-3">
        <ParabolicPhaseStrip />
      </div>

      {/* Live exposure caps — fetched from /api/config/exposure-caps so
          UI never drifts from the doctrine surface. */}
      {caps && (
        <div
          className="mb-4 border border-rd-border bg-rd-bg px-3 py-2 flex flex-wrap items-center gap-x-4 gap-y-1"
          data-testid="exposure-caps-strip"
        >
          <span className="text-[10px] uppercase tracking-widest text-rd-dim">Live caps</span>
          <span className="font-mono text-[11px] text-rd-text">
            EQUITY <span className="text-rd-dim">per-order</span>{" "}
            <span className="text-rd-accent">${Number(caps.per_order_usd).toLocaleString()}</span>
          </span>
          {Object.entries(caps.per_order_by_lane_usd || {}).map(([lane, cap]) => (
            <span key={lane} className="font-mono text-[11px] text-rd-text">
              {lane.toUpperCase()} <span className="text-rd-dim">per-order</span>{" "}
              <span className="text-rd-accent">${Number(cap).toLocaleString()}</span>
            </span>
          ))}
          <span className="font-mono text-[10px] text-rd-dim ml-auto">
            day ${Number(caps.per_day_usd).toLocaleString()} · open ${Number(caps.open_notional_usd).toLocaleString()}
          </span>
        </div>
      )}

      {/* Stats */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-6 gap-2 mb-6" data-testid="intents-stats">
          <StatTile label="Total" value={stats.total} testid="stat-total" />
          {STACKS.filter((s) => s !== "all").map((s) => (
            <StatTile
              key={s}
              label={s.toUpperCase()}
              value={stats.byStack[s] || 0}
              color={BRAIN_META[s]?.color}
              testid={`stat-stack-${s}`}
            />
          ))}
          <StatTile
            label="Pending"
            value={stats.byGate.pending || 0}
            color="#A1A1AA"
            testid="stat-pending"
          />
        </div>
      )}

      {/* Filters */}
      <Card className="mb-4" testid="intents-filters">
        <div className="flex items-center gap-3 mb-3">
          <Funnel size={13} weight="bold" className="text-rd-dim" />
          <span className="label-eyebrow">Filters</span>
        </div>
        <div className="flex flex-wrap gap-4">
          <FilterPill label="Lane" value={lane} options={LANES} onChange={setLane} testid="filter-lane" />
          <FilterPill label="Stack" value={stack} options={STACKS} onChange={setStack} testid="filter-stack" />
          <FilterPill label="Action" value={action} options={ACTIONS} onChange={setAction} testid="filter-action" />
          <FilterPill label="Gate" value={gateState} options={GATE_STATES} onChange={setGateState} testid="filter-gate" />
        </div>
      </Card>

      {/* Seat-doctrinal auto-retire suggestions. Lane-scoped so the
          operator only sees suggestions for the lane they're filtering. */}
      <PanelErrorBoundary panelName="Auto-Retire Strip" testid="panel-error-autoretire">
        <AutoRetireStrip lane={lane} />
      </PanelErrorBoundary>

      {/* Live doctrine-health summary. Compact mode keeps it scannable
          alongside the auto-retire strip; the full /admin/doctrine page
          has the deep view with ideal-snapshot + blockers + rejections. */}
      <PanelErrorBoundary panelName="Doctrine Health" testid="panel-error-doctrine-health">
        <DoctrineHealthPanel mode="compact" lane={lane} />
      </PanelErrorBoundary>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono" data-testid="intents-error">
          {err}
        </div>
      )}

      {/* Table */}
      <Card className="p-0" testid="intents-table-card">
        {filtered === null ? (
          <LoadingRow />
        ) : filtered.length === 0 ? (
          <EmptyState message="No intents match these filters." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left" data-testid="intents-table">
              <thead className="bg-rd-bg border-b border-rd-border">
                <tr className="text-[10px] font-mono uppercase tracking-widest text-rd-dim">
                  <th className="px-3 py-2 font-normal">When</th>
                  <th className="px-3 py-2 font-normal">Stack</th>
                  <th className="px-3 py-2 font-normal">Symbol</th>
                  <th className="px-3 py-2 font-normal">Lane</th>
                  <th className="px-3 py-2 font-normal">Action</th>
                  <th className="px-3 py-2 font-normal">Conf</th>
                  <th className="px-3 py-2 font-normal">R·Mult</th>
                  <th className="px-3 py-2 font-normal">Gate</th>
                  <th className="px-3 py-2 font-normal text-right">—</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((it) => (
                  <IntentRow
                    key={it.intent_id}
                    intent={it}
                    expanded={expanded === it.intent_id}
                    onToggle={() => setExpanded((e) => (e === it.intent_id ? null : it.intent_id))}
                    onDryRun={() => runDryRun(it.intent_id)}
                    onSubmit={() => runSubmit(it.intent_id, it.lane)}
                    dryRunResult={dryRunByIntent[it.intent_id]}
                    submitResult={submitByIntent[it.intent_id]}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Submit confirmation modal — replaces the legacy
          window.prompt/confirm pair. Carries notional, BUY/SELL
          toggle, and operator override flag/reason. */}
      <SubmitOrderModal
        open={submitModal.open}
        intent={submitModal.intent}
        capUsd={
          submitModal.intent ? capForLane(submitModal.intent.lane) : null
        }
        onConfirm={performSubmit}
        onClose={() => setSubmitModal({ open: false, intent: null })}
      />
    </div>
  );
}
