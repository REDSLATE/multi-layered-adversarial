/**
 * HealthcheckTile — post-deploy runtime validation, single-glance.
 * (2026-02-26)
 *
 * Doctrine pin (operator + advisor-driven, post-mortem of three failed
 * deploys today):
 *   "Static analyzers can verify the codebase is structurally sound,
 *    but they CANNOT tell us whether the auto-router actually ticks,
 *    whether required indexes exist in the LIVE database, or whether
 *    the pod is genuinely Ready. Hit ONE endpoint after every deploy,
 *    see the truth in one row, no log grepping."
 *
 * Backs onto GET /api/admin/healthcheck/full which returns:
 *   { overall: pass|warn|fail,
 *     failures: [...],
 *     warnings: [...],
 *     checks: { mongo_connected, required_indexes, sample_intent_query,
 *               auto_router_ticking, recent_intents, direct_execute_state } }
 *
 * Auto-refreshes on mount + every 30s. READ-ONLY.
 */
import { useCallback, useEffect, useState } from "react";
import { ArrowsClockwise, CheckCircle, Warning, XCircle, Heartbeat } from "@phosphor-icons/react";
import { api } from "@/lib/api";

const POLL_MS = 30_000;

const STATUS_META = {
  pass: { icon: CheckCircle, label: "PASS", color: "text-emerald-400", bar: "bg-emerald-500" },
  warn: { icon: Warning,     label: "WARN", color: "text-amber-400",   bar: "bg-rd-warn" },
  fail: { icon: XCircle,     label: "FAIL", color: "text-rose-400",    bar: "bg-rd-danger" },
};

// Friendly labels for each check key — shown in the row.
const CHECK_LABELS = {
  mongo_connected:       "Mongo connection",
  required_indexes:      "Required indexes present",
  sample_intent_query:   "Auto-router query (live)",
  auto_router_ticking:   "Auto-router ticking",
  recent_intents:        "Brain emissions in last hour",
  direct_execute_state:  "Direct-execute mode",
};

function relAgo(iso) {
  if (!iso) return "—";
  try {
    const t = new Date(iso).getTime();
    const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (sec < 60) return `${sec}s ago`;
    if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
    return `${Math.round(sec / 3600)}h ago`;
  } catch {
    return "—";
  }
}

export default function HealthcheckTile() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);
  const [lastFetch, setLastFetch] = useState(null);

  const fetchOnce = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const res = await api.get("/admin/healthcheck/full");
      setData(res.data || res);
      setLastFetch(new Date().toISOString());
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || "fetch failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchOnce();
    const id = setInterval(fetchOnce, POLL_MS);
    return () => clearInterval(id);
  }, [fetchOnce]);

  const overall = (data && data.overall) || "pending";
  const meta = STATUS_META[overall] || {
    icon: Heartbeat, label: "LOADING", color: "text-rd-dim", bar: "bg-rd-dim",
  };
  const Icon = meta.icon;

  return (
    <section
      className="border border-rd-border bg-rd-bg p-4"
      data-testid="healthcheck-tile"
    >
      <header className="flex items-center justify-between mb-3">
        <div>
          <div className="flex items-center gap-2">
            <Heartbeat size={18} className="text-rd-accent" />
            <h3 className="text-sm font-mono uppercase tracking-wider text-rd-text">
              Runtime Health · Post-Deploy Validation
            </h3>
          </div>
          <p className="mt-1 text-[11px] text-rd-dim">
            One-shot runtime truth. Static analyzers cannot tell you whether
            the auto-router actually ticks — this can.
          </p>
        </div>
        <button
          onClick={fetchOnce}
          disabled={loading}
          className="flex items-center gap-1 px-2 py-1 text-[10px] uppercase tracking-wider text-rd-dim hover:text-rd-text disabled:opacity-50 border border-rd-border"
          data-testid="healthcheck-refresh-btn"
        >
          <ArrowsClockwise size={12} className={loading ? "animate-spin" : ""} />
          refresh
        </button>
      </header>

      {/* Overall status banner */}
      <div
        className={`flex items-center gap-3 px-3 py-2 border border-rd-border ${
          overall === "pass" ? "bg-emerald-950/30" :
          overall === "warn" ? "bg-amber-950/30" :
          overall === "fail" ? "bg-rose-950/30" : "bg-rd-bg"
        }`}
        data-testid="healthcheck-overall-banner"
      >
        <Icon size={20} className={meta.color} />
        <div className="flex-1">
          <div className={`text-sm font-mono font-bold ${meta.color}`}>
            {meta.label}
            {data && (
              <span className="ml-2 text-[10px] text-rd-dim font-normal">
                {data.total_elapsed_ms}ms · checked {relAgo(lastFetch)}
              </span>
            )}
          </div>
          {data && data.failures && data.failures.length > 0 && (
            <div className="text-[10px] text-rose-300 mt-0.5">
              failing: {data.failures.join(", ")}
            </div>
          )}
          {data && data.warnings && data.warnings.length > 0 && (
            <div className="text-[10px] text-amber-300 mt-0.5">
              warning: {data.warnings.join(", ")}
            </div>
          )}
        </div>
      </div>

      {/* Error state */}
      {err && (
        <div className="mt-3 px-3 py-2 border border-rose-700 bg-rose-950/30 text-[11px] text-rose-300">
          fetch failed: {String(err).slice(0, 200)}
        </div>
      )}

      {/* Per-check rows */}
      {data && data.checks && (
        <div className="mt-3 border border-rd-border" data-testid="healthcheck-checks">
          {Object.entries(data.checks).map(([name, c], idx) => {
            const cmeta = STATUS_META[c.status] || STATUS_META.fail;
            const CIcon = cmeta.icon;
            return (
              <div
                key={name}
                className={`flex items-start gap-3 px-3 py-2 text-[11px] ${
                  idx > 0 ? "border-t border-rd-border" : ""
                }`}
                data-testid={`healthcheck-row-${name}`}
              >
                <CIcon size={14} className={`${cmeta.color} flex-shrink-0 mt-[1px]`} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-rd-text">
                      {CHECK_LABELS[name] || name}
                    </span>
                    <span className="text-[9px] text-rd-dim">
                      {c.elapsed_ms}ms
                    </span>
                  </div>
                  <div className="text-[10px] text-rd-dim mt-0.5 break-words">
                    {c.detail}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
