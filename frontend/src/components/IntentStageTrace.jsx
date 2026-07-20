import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";

/**
 * Per-intent stage trace — pipeline seams made visible.
 * Renders inside the expanded intent row; fetches on mount.
 */
const STAGE_DEFS = [
  { key: "stage_arbiter", label: "ARBITER" },
  { key: "stage_intent", label: "INTENT" },
  { key: "stage_gates", label: "GATES" },
  { key: "stage_broker", label: "BROKER" },
  { key: "stage_fills", label: "FILLS" },
];

function stageStatus(key, trace) {
  const v = trace[key];
  const intent = trace.stage_intent;
  switch (key) {
    case "stage_arbiter":
      if (!v) return { state: "none", note: "no decision record" };
      if (v.suppressed_duplicate) return { state: "block", note: v.suppression_reason };
      if (v.emit_error) return { state: "block", note: "emit_error" };
      return { state: "pass", note: `${v.brain} · size ×${(v.size_multiplier ?? 1).toFixed(2)}` };
    case "stage_intent":
      if (!v) return { state: "block", note: "not found" };
      if (v.gate_state === "rejected_at_ingest") return { state: "block", note: "firewall reject" };
      return { state: "pass", note: v.gate_state };
    case "stage_gates": {
      if (!intent) return { state: "none", note: "—" };
      if (intent.gate_state === "blocked")
        return { state: "block", note: intent.blocked_by || "blocked" };
      if (intent.expire_reason) return { state: "block", note: intent.expire_reason };
      if (intent.gate_state === "pending") return { state: "wait", note: "awaiting router" };
      return { state: "pass", note: `${(v || []).length} checks` };
    }
    case "stage_broker": {
      if (!v || v.length === 0)
        return intent?.executed
          ? { state: "pass", note: intent.broker_status || "accepted" }
          : { state: "none", note: "no submit" };
      const ok = v.some((r) => r.ok);
      return ok
        ? { state: "pass", note: v[v.length - 1].broker_status || "accepted" }
        : { state: "block", note: v[v.length - 1].broker_status || "rejected" };
    }
    case "stage_fills":
      if (!v || v.length === 0) return { state: "none", note: "no fill matched" };
      return { state: "pass", note: `${v.length} fill(s)` };
    default:
      return { state: "none", note: "" };
  }
}

const COLORS = {
  pass: "#10B981",
  block: "#EF4444",
  wait: "#FBBF24",
  none: "#6B7280",
};

export default function IntentStageTrace({ intentId }) {
  const [trace, setTrace] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    api.get(`/admin/intent-trace/${encodeURIComponent(intentId)}`)
      .then((r) => { if (alive) setTrace(r.data); })
      .catch((e) => { if (alive) setErr(e?.response?.data?.detail || e.message); });
    return () => { alive = false; };
  }, [intentId]);

  if (err) {
    return (
      <div className="text-[11px] font-mono text-rd-danger" data-testid={`intent-trace-error-${intentId}`}>
        trace failed: {err}
      </div>
    );
  }
  if (!trace) {
    return (
      <div className="text-[11px] font-mono text-rd-dim" data-testid={`intent-trace-loading-${intentId}`}>
        tracing…
      </div>
    );
  }

  const dies = trace.verdict?.startsWith("DIED") || trace.verdict?.startsWith("NOT FOUND");
  const filled = trace.verdict?.startsWith("FILLED") || trace.verdict?.startsWith("EXECUTED");
  const verdictColor = dies ? "#EF4444" : filled ? "#10B981" : "#FBBF24";

  return (
    <div data-testid={`intent-trace-${intentId}`}>
      <div className="label-eyebrow mt-4 mb-2">Stage trace</div>
      <div
        className="text-[11px] font-mono px-3 py-2 mb-3 border"
        style={{ color: verdictColor, borderColor: `${verdictColor}55`, background: `${verdictColor}0d` }}
        data-testid={`intent-trace-verdict-${intentId}`}
      >
        {trace.verdict}
      </div>
      <div className="flex items-stretch gap-0 overflow-x-auto">
        {STAGE_DEFS.map((s, k) => {
          const st = stageStatus(s.key, trace);
          const color = COLORS[st.state];
          return (
            <React.Fragment key={s.key}>
              {k > 0 && <div className="flex items-center px-1 text-rd-dim text-xs font-mono shrink-0">→</div>}
              <div
                className="border px-2.5 py-1.5 min-w-[104px] shrink-0"
                style={{ borderColor: st.state === "block" ? color : "var(--rd-border, #27272A)" }}
                data-testid={`intent-trace-stage-${s.label.toLowerCase()}-${intentId}`}
              >
                <div className="text-[9px] uppercase tracking-widest" style={{ color }}>
                  {s.label} · {st.state}
                </div>
                <div className="text-[10px] font-mono text-rd-dim truncate mt-0.5" title={st.note}>
                  {st.note}
                </div>
              </div>
            </React.Fragment>
          );
        })}
      </div>
      {(trace.stage_gates || []).length > 0 && (
        <div className="mt-3 border border-rd-border">
          {trace.stage_gates.map((g, k) => (
            <div
              key={`${g.kind}-${g.ts}-${k}`}
              className="px-3 py-1 flex items-center gap-3 text-[10px] font-mono border-b border-rd-border last:border-b-0"
            >
              <span className="text-rd-dim w-28 shrink-0 truncate">{g.kind || g.by || "gate"}</span>
              <span className="text-rd-text truncate" title={g.reason}>{g.reason || "—"}</span>
              <span className="text-rd-dim ml-auto shrink-0">{(g.ts || "").slice(11, 19)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
