import React, { useEffect, useState } from "react";
import { api, RUNTIME_META, fmtTime, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, LoadingRow } from "@/components/ui-bits";

export default function Diagnostics() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const { data } = await api.get("/admin/diagnostics");
        setData(data);
      } catch (e) {
        setErr(e?.response?.data?.detail || e.message);
      }
    })();
  }, []);

  return (
    <div className="reveal" data-testid="diagnostics-page">
      <PageHeader
        eyebrow="Shared · Diagnostics"
        title="Health & liveness"
        sub="System health, MongoDB connectivity, and per-runtime liveness signals."
        testid="diagnostics-header"
      />

      {err && <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono">{err}</div>}
      {!data && <LoadingRow />}

      {data && (
        <>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 md:gap-6 mb-6">
            <Card testid="diag-mongo">
              <div className="label-eyebrow mb-2">MongoDB</div>
              <div className="flex items-center gap-2">
                <span
                  className={`inline-block w-2 h-2 ${data.mongo.ok ? "bg-rd-chevelle pulse-dot" : "bg-rd-danger"}`}
                />
                <span className="font-display text-2xl font-bold tracking-tight">
                  {data.mongo.ok ? "ONLINE" : "OFFLINE"}
                </span>
              </div>
              {data.mongo.error && (
                <div className="text-xs font-mono text-rd-danger mt-2">{data.mongo.error}</div>
              )}
            </Card>
            <Card testid="diag-mode">
              <div className="label-eyebrow mb-2">Deploy mode</div>
              <div className="font-display text-2xl font-bold tracking-tight text-rd-warn uppercase">
                {data.deploy_mode}
              </div>
              <div className="text-xs text-rd-muted mt-2 font-mono">execution disabled</div>
            </Card>
            <Card testid="diag-now">
              <div className="label-eyebrow mb-2">Server time</div>
              <div className="font-mono text-sm text-rd-text">{fmtTime(data.now)}</div>
            </Card>
          </div>

          <Card className="p-0 overflow-hidden" testid="diag-runtimes">
            <table className="w-full text-xs font-mono">
              <thead>
                <tr className="bg-rd-bg3 text-rd-dim uppercase tracking-widest">
                  <th className="text-left px-4 py-3 border-b border-rd-border">Runtime</th>
                  <th className="text-right px-4 py-3 border-b border-rd-border">Decision log</th>
                  <th className="text-right px-4 py-3 border-b border-rd-border">Memory labels</th>
                  <th className="text-left px-4 py-3 border-b border-rd-border">Last receipt</th>
                  <th className="text-left px-4 py-3 border-b border-rd-border">Status</th>
                </tr>
              </thead>
              <tbody>
                {data.runtimes.map((r) => {
                  const meta = RUNTIME_META[r.runtime];
                  return (
                    <tr key={r.runtime} className="border-b border-rd-border last:border-b-0">
                      <td className="px-4 py-2.5">
                        <span style={{ color: meta.color }} className="font-bold">
                          {meta.label}
                        </span>
                        <span className="text-rd-dim ml-2">· {meta.project}</span>
                      </td>
                      <td className="px-4 py-2.5 text-right">{r.log_count}</td>
                      <td className="px-4 py-2.5 text-right">{r.memory_labels_count}</td>
                      <td className="px-4 py-2.5">
                        {r.last_receipt_ts ? `${fmtTime(r.last_receipt_ts)} (${relTime(r.last_receipt_ts)})` : "—"}
                      </td>
                      <td className="px-4 py-2.5">
                        <Badge color={r.heartbeat_stale ? "#EF4444" : "#FBBF24"}>
                          {r.heartbeat_stale ? "STALE" : "OBSERVING"}
                        </Badge>
                        {r.heartbeat_age_seconds != null && (
                          <span className="ml-2 text-rd-dim">
                            {Math.floor(r.heartbeat_age_seconds)}s
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>
        </>
      )}
    </div>
  );
}
