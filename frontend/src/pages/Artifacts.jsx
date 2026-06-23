import React, { useEffect, useState } from "react";
import { api, getRuntimeMeta, fmtTime } from "@/lib/api";
import { PageHeader, Card, EmptyState, LoadingRow } from "@/components/ui-bits";

export default function Artifacts() {
  const [data, setData] = useState(null);
  useEffect(() => {
    (async () => {
      const { data } = await api.get("/shared/artifacts");
      setData(data);
    })();
  }, []);

  const grouped = (data?.items || []).reduce((acc, a) => {
    (acc[a.runtime] ||= []).push(a);
    return acc;
  }, {});

  return (
    <div className="reveal" data-testid="artifacts-page">
      <PageHeader
        eyebrow="Shared · Artifact inventory"
        title="Per-runtime model artifacts"
        sub="Artifacts are catalogued in the shared inventory for visibility, but they remain ISOLATED per runtime. Alpha never loads Camaro's weights — and vice versa."
        testid="artifacts-header"
      />

      {!data && <LoadingRow />}
      {data && data.items.length === 0 && <EmptyState testid="artifacts-empty" />}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 md:gap-6">
        {Object.keys(grouped).map((rt) => {
          const meta = getRuntimeMeta(rt);
          return (
            <Card key={rt} accentColor={meta.color} testid={`artifacts-group-${rt}`}>
              <div className="flex items-baseline justify-between mb-4">
                <div>
                  <div className="font-display text-xl font-black tracking-tighter" style={{ color: meta.color }}>
                    {meta.label}
                  </div>
                  <div className="label-eyebrow mt-1">{meta.project}</div>
                </div>
                <div className="label-eyebrow">{grouped[rt].length} artifacts</div>
              </div>
              <div className="space-y-2">
                {grouped[rt].map((a) => (
                  <div
                    key={`${a.runtime}-${a.artifact}`}
                    className="border border-rd-border p-3"
                    data-testid={`artifact-${a.artifact}`}
                  >
                    <div className="flex items-center justify-between mb-1">
                      <span className="font-mono text-xs text-rd-text">{a.artifact}</span>
                      <span className="font-mono text-[10px]" style={{ color: meta.color }}>{a.version}</span>
                    </div>
                    <div className="flex items-center justify-between text-[10px] font-mono text-rd-dim">
                      <span>sha {a.sha}</span>
                      <span>{fmtTime(a.registered_at)}</span>
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
