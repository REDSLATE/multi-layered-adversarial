import React, { useEffect, useState } from "react";
import { api, RUNTIME_META, fmtTime } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";

const RT = ["all", "alpha", "camaro", "chevelle"];
const LABELS = ["all", "safe", "review", "quarantine"];
const LABEL_COLORS = { safe: "#10B981", review: "#FBBF24", quarantine: "#EF4444" };

export default function MemoryFirewall() {
  const [rt, setRt] = useState("all");
  const [lbl, setLbl] = useState("all");
  const [data, setData] = useState(null);

  useEffect(() => {
    setData(null);
    (async () => {
      const params = [];
      if (rt !== "all") params.push(`runtime=${rt}`);
      if (lbl !== "all") params.push(`label=${lbl}`);
      params.push("limit=300");
      const { data } = await api.get(`/shared/memory-labels?${params.join("&")}`);
      setData(data);
    })();
  }, [rt, lbl]);

  return (
    <div className="reveal" data-testid="memory-page">
      <PageHeader
        eyebrow="Shared · Memory Labeling Firewall"
        title="shared_labeled_memories"
        sub="All runtimes write feature/memory payloads through the firewall. Every payload gets labeled safe / review / quarantine before any downstream component can consume it."
        testid="memory-header"
      />

      <div className="flex flex-wrap items-center gap-3 mb-5" data-testid="memory-filter-bar">
        <div className="flex gap-2">
          <span className="label-eyebrow self-center mr-1">Runtime</span>
          {RT.map((x) => (
            <button
              key={x}
              onClick={() => setRt(x)}
              data-testid={`memory-filter-rt-${x}`}
              className={`btn-sharp px-3 py-1.5 border ${
                rt === x ? "bg-zinc-100 text-zinc-900 border-zinc-100" : "border-rd-border text-rd-muted hover:text-rd-text"
              }`}
            >
              {x}
            </button>
          ))}
        </div>
        <div className="flex gap-2">
          <span className="label-eyebrow self-center mr-1">Label</span>
          {LABELS.map((x) => (
            <button
              key={x}
              onClick={() => setLbl(x)}
              data-testid={`memory-filter-lbl-${x}`}
              className={`btn-sharp px-3 py-1.5 border ${
                lbl === x ? "bg-zinc-100 text-zinc-900 border-zinc-100" : "border-rd-border text-rd-muted hover:text-rd-text"
              }`}
            >
              {x}
            </button>
          ))}
        </div>
      </div>

      {!data && <LoadingRow />}
      {data && data.items.length === 0 && <EmptyState testid="memory-empty" />}
      {data && data.items.length > 0 && (
        <Card className="p-0 overflow-hidden">
          <div className="font-mono text-xs">
            {data.items.map((m, i) => (
              <div
                key={m.id}
                className="grid grid-cols-12 gap-3 px-4 py-2.5 border-b border-rd-border last:border-b-0 hover:bg-rd-bg3"
                data-testid={`memory-row-${i}`}
              >
                <div className="col-span-3 text-rd-muted">{fmtTime(m.timestamp)}</div>
                <div className="col-span-2">
                  <span style={{ color: RUNTIME_META[m.runtime]?.color }} className="font-bold">
                    {RUNTIME_META[m.runtime]?.label || m.runtime}
                  </span>
                </div>
                <div className="col-span-2">
                  <Badge color={LABEL_COLORS[m.label] || "#71717A"}>{m.label}</Badge>
                </div>
                <div className="col-span-5 text-rd-text truncate">
                  <span className="text-rd-dim">{m.payload_summary}</span>
                  <span className="text-rd-muted"> — {m.reason}</span>
                </div>
              </div>
            ))}
          </div>
          <div className="px-4 py-3 border-t border-rd-border text-[10px] text-rd-dim uppercase tracking-widest">
            {data.count} records
          </div>
        </Card>
      )}
    </div>
  );
}
