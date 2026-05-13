import React, { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { api, RUNTIME_META, fmtTime, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";
import SovereignTile from "@/components/SovereignTile";
import LivePulse from "@/components/LivePulse";

const SUB_ENDPOINT = {
  alpha: { url: "/runtime/alpha/decisions", title: "alpha_decision_log", cols: ["timestamp", "decision", "symbol", "score"] },
  camaro: { url: "/runtime/camaro/shadow-rows", title: "camaro_shadow_rows", cols: ["timestamp", "shadow", "symbol", "side", "size"] },
  chevelle: { url: "/runtime/chevelle/memory-labels", title: "chevelle_memory_labels", cols: ["timestamp", "authority_call", "symbol", "horizon"] },
};

export default function RuntimeDetail() {
  const { runtime } = useParams();
  const meta = RUNTIME_META[runtime];
  const sub = SUB_ENDPOINT[runtime];
  const [status, setStatus] = useState(null);
  const [rows, setRows] = useState(null);
  const [calibs, setCalibs] = useState(null);
  const [artifacts, setArtifacts] = useState(null);

  useEffect(() => {
    if (!meta) return;
    setStatus(null);
    setRows(null);
    (async () => {
      const [s, r, c, a] = await Promise.all([
        api.get(`/runtime/${runtime}/status`),
        api.get(sub.url),
        api.get(`/shared/calibrators?runtime=${runtime}`),
        api.get(`/shared/artifacts?runtime=${runtime}`),
      ]);
      setStatus(s.data);
      setRows(r.data);
      setCalibs(c.data);
      setArtifacts(a.data);
    })();
  }, [runtime, meta, sub]);

  if (!meta) {
    return (
      <div className="p-10 text-center text-rd-danger" data-testid="runtime-unknown">
        Unknown runtime: {runtime}.{" "}
        <Link to="/admin" className="underline">
          Back to overview
        </Link>
      </div>
    );
  }

  return (
    <div className="reveal" data-testid={`runtime-page-${runtime}`}>
      <PageHeader
        eyebrow={`Runtime · ${meta.project}`}
        title={meta.label}
        sub={`${meta.note}. Decision authority is isolated to this runtime — no cross-runtime reads.`}
        right={
          <div className="flex items-center gap-3">
            <LivePulse runtime={runtime} />
            <Badge color={meta.color}>{meta.label}</Badge>
          </div>
        }
        testid={`runtime-header-${runtime}`}
      />

      {!status && <LoadingRow />}

      {status && (
        <>
          {/* Status strip */}
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4 md:gap-6 mb-6" data-testid={`runtime-status-${runtime}`}>
            <Card accentColor={meta.color}>
              <div className="label-eyebrow mb-2">Mode</div>
              <Badge color="#FBBF24">{status.mode}</Badge>
            </Card>
            <Card accentColor={meta.color}>
              <div className="label-eyebrow mb-2">Enforce flag</div>
              <div className="font-mono text-[11px] text-rd-text mb-1">{meta.enforceLabel}</div>
              <Badge color={
                status.phase6_enforce_enabled ?? status.executor_enforce_enabled ?? status.authority_enabled
                  ? "#10B981" : "#71717A"
              }>
                {(status.phase6_enforce_enabled ?? status.executor_enforce_enabled ?? status.authority_enabled)
                  ? "ENABLED" : "DISABLED"}
              </Badge>
            </Card>
            <Card accentColor={meta.color}>
              <div className="label-eyebrow mb-2">Records</div>
              <div className="font-display text-2xl font-bold tracking-tight" style={{ color: meta.color }}>
                {status.decision_log_count ?? status.shadow_rows_count ?? status.memory_labels_count ?? 0}
              </div>
              <div className="text-[10px] text-rd-dim font-mono mt-1">{sub.title}</div>
            </Card>
            <Card accentColor={meta.color}>
              <div className="label-eyebrow mb-2">Doctrine</div>
              <div className="text-[11px] text-rd-muted leading-relaxed font-mono">
                {status.doctrine}
              </div>
            </Card>
          </div>

          {/* Sovereign state — periodic snapshot from the brain's deterministic core */}
          <div className="mb-6">
            <SovereignTile runtime={runtime} accent={meta.color} />
          </div>

          {/* Decision log */}
          <Card className="p-0 overflow-hidden mb-6" testid={`runtime-rows-${runtime}`}>
            <div className="px-4 py-3 border-b border-rd-border flex items-center justify-between">
              <div>
                <div className="label-eyebrow">Isolated decision store</div>
                <div className="font-mono text-sm">{sub.title}</div>
              </div>
              <div className="text-[10px] text-rd-dim uppercase tracking-widest">
                {rows?.count || 0} records
              </div>
            </div>
            {!rows && <LoadingRow />}
            {rows && rows.items.length === 0 && <EmptyState message="No records in this runtime's log." />}
            {rows && rows.items.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-xs font-mono">
                  <thead>
                    <tr className="bg-rd-bg3 text-rd-dim uppercase tracking-widest">
                      {sub.cols.map((c) => (
                        <th key={c} className="text-left px-4 py-3 border-b border-rd-border">{c}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.items.map((row, i) => (
                      <tr key={row.id || i} className="border-b border-rd-border last:border-b-0 hover:bg-rd-bg3">
                        {sub.cols.map((c) => (
                          <td key={c} className="px-4 py-2.5">
                            {c === "timestamp"
                              ? `${fmtTime(row[c])} (${relTime(row[c])})`
                              : row[c] != null
                              ? String(row[c])
                              : "—"}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          {/* Calibrators + Artifacts side-by-side */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 md:gap-6">
            <Card testid={`runtime-calibrators-${runtime}`}>
              <div className="label-eyebrow mb-3">Calibrators (this runtime only)</div>
              <div className="space-y-2">
                {(calibs?.items || []).map((c) => (
                  <div key={c.name} className="flex items-center justify-between py-1 border-b border-rd-border last:border-b-0">
                    <span className="font-mono text-xs">{c.name}</span>
                    <span className="font-mono text-[10px] text-rd-muted">{c.method} · {c.version}</span>
                  </div>
                ))}
              </div>
            </Card>
            <Card testid={`runtime-artifacts-${runtime}`}>
              <div className="label-eyebrow mb-3">Artifacts (this runtime only)</div>
              <div className="space-y-2">
                {(artifacts?.items || []).map((a) => (
                  <div key={a.artifact} className="flex items-center justify-between py-1 border-b border-rd-border last:border-b-0">
                    <span className="font-mono text-xs">{a.artifact}</span>
                    <span className="font-mono text-[10px]" style={{ color: meta.color }}>
                      {a.version} · {a.sha}
                    </span>
                  </div>
                ))}
              </div>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
