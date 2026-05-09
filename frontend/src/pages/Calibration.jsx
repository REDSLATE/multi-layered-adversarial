import React, { useEffect, useState } from "react";
import { api, RUNTIME_META, fmtTime } from "@/lib/api";
import { PageHeader, Card, EmptyState, LoadingRow } from "@/components/ui-bits";

export default function Calibration() {
  const [data, setData] = useState(null);
  useEffect(() => {
    (async () => {
      const { data } = await api.get("/shared/calibrators");
      setData(data);
    })();
  }, []);

  const grouped = (data?.items || []).reduce((acc, c) => {
    (acc[c.runtime] ||= []).push(c);
    return acc;
  }, {});

  return (
    <div className="reveal" data-testid="calibration-page">
      <PageHeader
        eyebrow="Shared · Calibration tooling"
        title="Per-runtime calibrators"
        sub="Calibrators live side-by-side in shared tooling for visibility, but they are NEVER mixed at apply-time. Each runtime applies only its own calibrator at inference."
        testid="calibration-header"
      />

      {!data && <LoadingRow />}
      {data && data.items.length === 0 && <EmptyState testid="calibration-empty" />}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 md:gap-6">
        {Object.keys(grouped).map((rt) => {
          const meta = RUNTIME_META[rt];
          return (
            <Card key={rt} accentColor={meta.color} testid={`calibration-group-${rt}`}>
              <div className="flex items-baseline justify-between mb-4">
                <div className="font-display text-xl font-black tracking-tighter" style={{ color: meta.color }}>
                  {meta.label}
                </div>
                <div className="label-eyebrow">{grouped[rt].length} calibrators</div>
              </div>
              <div className="space-y-3">
                {grouped[rt].map((c) => (
                  <div key={c.name} className="border border-rd-border p-3" data-testid={`calibrator-${c.name}`}>
                    <div className="flex items-center justify-between mb-1">
                      <span className="font-mono text-xs text-rd-text">{c.name}</span>
                      <span className="font-mono text-[10px] text-rd-muted">{c.version}</span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] uppercase tracking-widest text-rd-dim">{c.method}</span>
                      <span className="text-[10px] font-mono text-rd-dim">fit {fmtTime(c.fit_at)}</span>
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
