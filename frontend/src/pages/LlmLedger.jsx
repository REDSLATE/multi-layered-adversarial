import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import { ArrowClockwise, ThumbsUp, ThumbsDown, Minus, Brain, X as CloseIcon } from "@phosphor-icons/react";

/**
 * LlmLedger — operator view of the RISE_AI LLM Kernel decision trace.
 *
 * Doctrine:
 *   This panel reads `/api/admin/llm/ledger` (preview rows) and
 *   `/api/admin/llm/ledger/{call_id}` (full detail). Grading writes
 *   into preference_log and (for score ≥ +1) auto-enqueues the row
 *   into the distillation queue. Grades DO NOT affect execution
 *   or provider promotion — that's a separate operator action.
 *   ADVISORY_ONLY stamp is preserved across the surface.
 */

const PROVIDER_COLOR = {
  openai:        "#10A37F",
  anthropic:     "#C77B5C",
  gemini:        "#4285F4",
  local:         "#A855F7",
  self_trained:  "#F59E0B",
};

const ROLE_COLOR = {
  strategist: "#3B82F6",
  governor:   "#10B981",
  opponent:   "#DC2626",
  memory:     "#A855F7",
  auditor:    "#06B6D4",
  executor:   "#F59E0B",
};

function relTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function fmtScore(s) {
  if (s == null) return null;
  if (s > 0) return `+${s}`;
  return String(s);
}

function scoreColor(s) {
  if (s == null) return "#A1A1AA";
  if (s >= 1) return "#22C55E";
  if (s <= -1) return "#EF4444";
  return "#A1A1AA";
}

export default function LlmLedger() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [hours, setHours] = useState(24);
  const [role, setRole] = useState("");
  const [provider, setProvider] = useState("");
  const [onlyUngraded, setOnlyUngraded] = useState(false);
  const [detail, setDetail] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const params = { hours, limit: 100 };
      if (role) params.role = role;
      if (provider) params.provider = provider;
      if (onlyUngraded) params.only_ungraded = true;
      const r = await api.get("/admin/llm/ledger", { params });
      setData(r.data);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, [hours, role, provider, onlyUngraded]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <div className="space-y-4 p-4" data-testid="llm-ledger-page">
      <div className="flex items-end justify-between gap-4 flex-wrap">
        <div>
          <div className="label-eyebrow">RISE_AI · LLM kernel</div>
          <div className="font-display text-2xl font-black tracking-tight flex items-center gap-2">
            <Brain size={22} weight="bold" />
            decision-trace ledger
          </div>
          <div className="text-[11px] font-mono text-rd-muted pt-1">
            Every brain LLM call lands here. Grade rows to feed the
            training pipeline. Grades are ADVISORY_ONLY and never
            affect execution.
          </div>
        </div>
        <div className="flex items-center gap-2 text-xs font-mono">
          <FilterSelect
            value={String(hours)}
            options={[
              ["1", "1h"], ["6", "6h"], ["24", "24h"],
              ["72", "3d"], ["168", "7d"],
            ]}
            onChange={(v) => setHours(parseInt(v, 10))}
            label="window"
            testid="ledger-hours"
          />
          <FilterSelect
            value={role}
            options={[
              ["", "all roles"],
              ["strategist", "strategist"],
              ["governor", "governor"],
              ["opponent", "opponent"],
              ["memory", "memory"],
              ["auditor", "auditor"],
              ["executor", "executor"],
            ]}
            onChange={setRole}
            label="role"
            testid="ledger-role"
          />
          <FilterSelect
            value={provider}
            options={[
              ["", "all providers"],
              ["openai", "openai"],
              ["anthropic", "anthropic"],
              ["gemini", "gemini"],
              ["local", "local"],
              ["self_trained", "self_trained"],
            ]}
            onChange={setProvider}
            label="provider"
            testid="ledger-provider"
          />
          <label className="flex items-center gap-1 text-rd-muted">
            <input
              type="checkbox"
              checked={onlyUngraded}
              onChange={(e) => setOnlyUngraded(e.target.checked)}
              data-testid="ledger-only-ungraded"
            />
            ungraded only
          </label>
          <button
            onClick={refresh}
            disabled={loading}
            className="border border-rd-border px-2 py-1 text-rd-muted hover:text-rd-text disabled:opacity-40"
            data-testid="ledger-refresh"
          >
            <ArrowClockwise size={12} weight="bold" />
          </button>
        </div>
      </div>

      {err && (
        <Card>
          <div className="px-3 py-2 text-xs font-mono text-rd-danger">{err}</div>
        </Card>
      )}

      <Card>
        <div className="px-3 py-2 border-b border-rd-border text-[10px] font-mono text-rd-dim grid grid-cols-12 gap-2 uppercase tracking-widest">
          <div className="col-span-1">when</div>
          <div className="col-span-1">role</div>
          <div className="col-span-2">provider · model</div>
          <div className="col-span-5">prompt → response</div>
          <div className="col-span-1">lat</div>
          <div className="col-span-2 text-right">grade</div>
        </div>

        {data?.items?.length === 0 && (
          <div className="px-3 py-6 text-center text-xs font-mono text-rd-dim">
            no llm calls in this window — brains haven&apos;t called the
            kernel yet, or filters exclude everything.
          </div>
        )}

        <div className="divide-y divide-rd-border">
          {(data?.items || []).map((row) => (
            <LedgerRow
              key={row.call_id}
              row={row}
              onClick={() => setDetail(row.call_id)}
            />
          ))}
        </div>
      </Card>

      {detail && (
        <DetailModal
          callId={detail}
          onClose={() => setDetail(null)}
          onGraded={() => {
            setDetail(null);
            refresh();
          }}
        />
      )}
    </div>
  );
}

function FilterSelect({ value, options, onChange, label, testid }) {
  return (
    <label className="flex items-center gap-1 text-rd-muted">
      <span className="uppercase tracking-widest text-[10px]">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-black border border-rd-border text-rd-text px-1 py-0.5 focus:outline-none focus:border-rd-text"
        data-testid={testid}
      >
        {options.map(([v, l]) => (
          <option key={v} value={v}>{l}</option>
        ))}
      </select>
    </label>
  );
}

function LedgerRow({ row, onClick }) {
  const providerColor = PROVIDER_COLOR[row.provider] || "#A1A1AA";
  const roleColor = ROLE_COLOR[row.role] || "#A1A1AA";
  const latest = row.latest_grade;

  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full text-left px-3 py-2 hover:bg-white/[0.02] grid grid-cols-12 gap-2 items-start text-[11px] font-mono"
      data-testid={`ledger-row-${row.call_id}`}
    >
      <div className="col-span-1 text-rd-dim">{relTime(row.created_at)}</div>
      <div className="col-span-1 font-bold tracking-widest" style={{ color: roleColor }}>
        {(row.role || "?").toUpperCase()}
      </div>
      <div className="col-span-2 truncate">
        <span className="font-bold tracking-widest" style={{ color: providerColor }}>
          {row.provider}
        </span>
        <span className="text-rd-dim"> · {row.model}</span>
      </div>
      <div className="col-span-5 text-rd-text leading-snug">
        <div className="text-rd-muted truncate">
          <span className="text-rd-dim">P:</span> {row.prompt || "—"}
        </div>
        <div className="truncate">
          <span className="text-rd-dim">R:</span> {row.response || "—"}
        </div>
      </div>
      <div className="col-span-1 text-rd-dim">
        {row.latency_ms ? `${row.latency_ms}ms` : "—"}
      </div>
      <div className="col-span-2 flex items-center justify-end gap-2">
        {row.ok === false && <Badge color="#EF4444">ERROR</Badge>}
        {latest ? (
          <Badge color={scoreColor(latest.score)}>
            {fmtScore(latest.score)} · {latest.outcome}
          </Badge>
        ) : (
          <span className="text-rd-dim italic">ungraded</span>
        )}
      </div>
    </button>
  );
}

// ─────────────────────── DetailModal ─────────────────────────────────

function DetailModal({ callId, onClose, onGraded }) {
  const [detail, setDetail] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState("helpful");
  const [note, setNote] = useState("");

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const r = await api.get(`/admin/llm/ledger/${callId}`);
        if (active) setDetail(r.data);
      } catch (e) {
        if (active) setErr(e?.response?.data?.detail || e.message);
      }
    })();
    return () => { active = false; };
  }, [callId]);

  const submitGrade = async (score) => {
    setBusy(true);
    setErr("");
    try {
      await api.post(`/admin/llm/ledger/${callId}/grade`, {
        score,
        outcome: outcome.trim() || (score >= 1 ? "helpful" : score <= -1 ? "wrong" : "neutral"),
        note: note.trim() || null,
      });
      onGraded();
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/85 p-4 overflow-y-auto"
      data-testid="ledger-detail-modal"
      onClick={onClose}
    >
      <div
        className="bg-rd-bg border border-rd-border w-full max-w-3xl font-mono my-8"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-3 py-2 border-b border-rd-border flex items-center justify-between text-xs">
          <div className="flex items-center gap-2 text-rd-muted">
            <Brain size={14} weight="bold" />
            <span className="font-bold text-rd-text tracking-widest">CALL</span>
            <span className="text-rd-dim">{callId.slice(0, 8)}</span>
          </div>
          <button onClick={onClose} className="text-rd-muted hover:text-rd-text" data-testid="ledger-detail-close">
            <CloseIcon size={14} weight="bold" />
          </button>
        </div>

        {!detail && !err && (
          <div className="px-3 py-6 text-xs text-rd-dim text-center">loading…</div>
        )}
        {err && (
          <div className="px-3 py-3 text-xs text-rd-danger" data-testid="ledger-detail-error">
            ✕ {err}
          </div>
        )}
        {detail && (
          <div className="p-3 space-y-3 text-xs">
            <div className="grid grid-cols-4 gap-2 text-[10px] uppercase tracking-widest">
              <Stat label="provider" value={detail.call.provider} color={PROVIDER_COLOR[detail.call.provider]} />
              <Stat label="role" value={detail.call.role} color={ROLE_COLOR[detail.call.role]} />
              <Stat label="latency" value={`${detail.call.latency_ms || 0}ms`} />
              <Stat label="authority" value={detail.call.llm_authority} color="#22C55E" />
            </div>
            <div>
              <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-1">prompt</div>
              <pre className="bg-black border border-rd-border p-2 max-h-48 overflow-auto whitespace-pre-wrap text-rd-text" data-testid="ledger-detail-prompt">
                {detail.call.prompt}
              </pre>
            </div>
            <div>
              <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-1">response</div>
              <pre className="bg-black border border-rd-border p-2 max-h-48 overflow-auto whitespace-pre-wrap text-rd-text" data-testid="ledger-detail-response">
                {detail.call.response}
              </pre>
            </div>

            {detail.grades?.length > 0 && (
              <div>
                <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-1">
                  prior grades ({detail.grades.length})
                </div>
                <div className="space-y-1">
                  {detail.grades.map((g) => (
                    <div key={`${g.created_at}-${g.outcome}`} className="border border-rd-border px-2 py-1 flex items-center gap-2 text-[11px]">
                      <Badge color={scoreColor(g.score)}>{fmtScore(g.score)}</Badge>
                      <span className="text-rd-text">{g.outcome}</span>
                      {g.note && <span className="text-rd-dim">— {g.note}</span>}
                      <span className="ml-auto text-rd-dim">{relTime(g.created_at)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="border-t border-rd-border pt-3 space-y-2">
              <div className="text-[10px] text-rd-dim uppercase tracking-widest">grade this call</div>
              <div className="flex items-center gap-2">
                <label className="flex-1">
                  <span className="text-[10px] text-rd-dim uppercase tracking-widest block">outcome</span>
                  <input
                    type="text"
                    value={outcome}
                    onChange={(e) => setOutcome(e.target.value)}
                    placeholder="helpful / wrong / neutral"
                    maxLength={64}
                    className="w-full bg-black border border-rd-border px-2 py-1 text-rd-text focus:outline-none focus:border-rd-text"
                    data-testid="ledger-grade-outcome"
                  />
                </label>
                <label className="flex-[2]">
                  <span className="text-[10px] text-rd-dim uppercase tracking-widest block">note (optional)</span>
                  <input
                    type="text"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="why this score?"
                    maxLength={1000}
                    className="w-full bg-black border border-rd-border px-2 py-1 text-rd-text focus:outline-none focus:border-rd-text"
                    data-testid="ledger-grade-note"
                  />
                </label>
              </div>
              <div className="flex items-center gap-2 pt-1">
                <GradeButton
                  color="#22C55E"
                  icon={ThumbsUp}
                  label="+1 helpful"
                  busy={busy}
                  onClick={() => submitGrade(1)}
                  testid="ledger-grade-plus"
                />
                <GradeButton
                  color="#A1A1AA"
                  icon={Minus}
                  label="0 neutral"
                  busy={busy}
                  onClick={() => submitGrade(0)}
                  testid="ledger-grade-zero"
                />
                <GradeButton
                  color="#EF4444"
                  icon={ThumbsDown}
                  label="-1 wrong"
                  busy={busy}
                  onClick={() => submitGrade(-1)}
                  testid="ledger-grade-minus"
                />
                <div className="ml-auto text-[10px] text-rd-dim">
                  +1 → enqueued for distillation
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, color }) {
  return (
    <div className="border border-rd-border px-2 py-1">
      <div className="text-rd-dim">{label}</div>
      <div className="font-bold" style={{ color: color || "#E4E4E7" }}>{value || "—"}</div>
    </div>
  );
}

function GradeButton({ color, icon: Icon, label, busy, onClick, testid }) {
  return (
    <button
      onClick={onClick}
      disabled={busy}
      className="px-3 py-1.5 border tracking-widest font-bold flex items-center gap-1 hover:opacity-100 opacity-90 disabled:opacity-30 disabled:cursor-not-allowed"
      style={{ borderColor: color, color }}
      onMouseEnter={(e) => { e.currentTarget.style.backgroundColor = color; e.currentTarget.style.color = "#000"; }}
      onMouseLeave={(e) => { e.currentTarget.style.backgroundColor = "transparent"; e.currentTarget.style.color = color; }}
      data-testid={testid}
    >
      <Icon size={12} weight="bold" />
      {label}
    </button>
  );
}
