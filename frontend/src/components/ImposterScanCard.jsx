import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Detective, ArrowsClockwise, Warning, CheckCircle } from "@phosphor-icons/react";

/**
 * ImposterScanCard — fires GET /admin/runtime/sidecar-imposter-scan
 * and renders the per-runtime divergence report. Surfaces any runtime
 * that has shown TWO+ distinct identities in the recent window
 * (different env_name, different pip_freeze_sha256, different
 * process_identity, or an UNKNOWN brain).
 *
 * Read-only. The endpoint never acts on the result.
 */
const WINDOW_OPTIONS = [1, 6, 24, 72, 168];
// 2026-02-XX: preview + prod share Mongo, so both pods' check-ins
// land in the same audit. Default to `prod` on the prod dashboard
// so legitimate preview check-ins don't trip imposter flags.
// Toggle to `all` to debug cross-environment confusion.
const ENV_OPTIONS = ["prod", "preview", "all"];


export default function ImposterScanCard() {
  const [windowHours, setWindowHours] = useState(24);
  const [envFilter, setEnvFilter] = useState("prod");
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = async (hrs, env) => {
    setBusy(true);
    setErr("");
    try {
      const r = await api.get(
        `/admin/runtime/sidecar-imposter-scan?window_hours=${hrs}&env=${env}`,
      );
      setData(r.data);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
      setData(null);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load(windowHours, envFilter);
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div
      className="border border-rd-border bg-rd-bg p-4 mb-6 font-mono"
      data-testid="imposter-scan-card"
    >
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <div className="flex items-center gap-2 text-rd-warning">
          <Detective size={14} weight="bold" />
          <span className="label-eyebrow text-rd-warning">Sidecar imposter scan</span>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <div className="flex items-center gap-1">
            <span className="text-[10px] text-rd-dim mr-1">env:</span>
            {ENV_OPTIONS.map((e) => (
              <button
                key={e}
                onClick={() => { setEnvFilter(e); load(windowHours, e); }}
                disabled={busy}
                data-testid={`imposter-scan-env-${e}`}
                className={
                  "px-2 py-0.5 text-[10px] uppercase border " +
                  (envFilter === e
                    ? "border-rd-warning text-rd-warning"
                    : "border-rd-border text-rd-dim hover:text-rd-text")
                }
              >
                {e}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-1">
            <span className="text-[10px] text-rd-dim mr-1">window:</span>
            {WINDOW_OPTIONS.map((h) => (
              <button
                key={h}
                onClick={() => { setWindowHours(h); load(h, envFilter); }}
                disabled={busy}
                data-testid={`imposter-scan-window-${h}`}
                className={
                  "px-2 py-0.5 text-[10px] uppercase border " +
                  (windowHours === h
                    ? "border-rd-warning text-rd-warning"
                    : "border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text")
                }
              >
                {h}h
              </button>
            ))}
            <button
              onClick={() => load(windowHours, envFilter)}
              disabled={busy}
              data-testid="imposter-scan-refresh"
              className="ml-2 text-rd-dim hover:text-rd-text"
              title="refresh"
            >
              <ArrowsClockwise size={12} weight="bold" className={busy ? "animate-spin" : ""} />
            </button>
          </div>
        </div>
      </div>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-2 text-[11px]">
          {err}
        </div>
      )}

      {data && (
        <>
          <div
            className={
              "border px-3 py-2 mb-3 text-[11px] flex items-center gap-2 " +
              (data.any_imposter_suspected
                ? "border-rd-danger text-rd-danger bg-rd-danger/5"
                : "border-rd-success text-rd-success bg-rd-success/5")
            }
            data-testid="imposter-scan-banner"
          >
            {data.any_imposter_suspected
              ? <><Warning size={12} weight="bold" /> IMPOSTER SIGNAL DETECTED in last {data.window_hours}h</>
              : <><CheckCircle size={12} weight="bold" /> clean — no divergent identities in last {data.window_hours}h</>
            }
          </div>

          {data.by_runtime.length === 0 && (
            <div className="text-rd-dim text-[11px]">
              No check-ins in the audit log for this window.
            </div>
          )}

          <div className="space-y-2">
            {data.by_runtime.map((r) => (
              <div
                key={r.runtime}
                data-testid={`imposter-scan-runtime-${r.runtime}`}
                className={
                  "border bg-rd-bg2 px-3 py-2 text-[11px] " +
                  (r.imposter_suspected
                    ? "border-rd-danger"
                    : "border-rd-border")
                }
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="font-bold uppercase tracking-wider text-rd-text">
                    {r.runtime}
                  </span>
                  <span className="text-rd-dim text-[10px]">
                    {r.checkin_count} check-ins
                  </span>
                </div>
                {r.imposter_suspected && (
                  <div className="text-rd-danger mb-1">
                    {r.reasons.map((reason, idx) => (
                      <div key={idx}>• {reason}</div>
                    ))}
                  </div>
                )}
                <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[10px] text-rd-dim">
                  <div>env: <span className="text-rd-text">{(r.distinct_env_names || []).join(", ") || "—"}</span></div>
                  <div>pip shas: <span className="text-rd-text">{(r.distinct_pip_shas || []).length}</span></div>
                  <div>source IPs: <span className="text-rd-text">{(r.distinct_source_ips || []).length}</span></div>
                  <div>git shas: <span className="text-rd-text">{(r.distinct_git_shas || []).length}</span></div>
                  <div>processes: <span className="text-rd-text">{(r.distinct_process_identities || []).length}</span></div>
                  <div>verdicts: <span className="text-rd-text">{(r.distinct_verdicts || []).join(", ") || "—"}</span></div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
