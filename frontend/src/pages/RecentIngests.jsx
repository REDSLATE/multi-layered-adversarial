import React, { useEffect, useState, useRef, useCallback } from "react";
import { api, RUNTIME_META, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, LoadingRow } from "@/components/ui-bits";

export default function RecentIngests() {
  const [data, setData] = useState(null);
  const [paused, setPaused] = useState(false);
  const [err, setErr] = useState("");
  const intervalRef = useRef(null);

  const tick = useCallback(async () => {
    try {
      const { data } = await api.get("/shared/recent-ingests?limit=80");
      setData(data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => {
    tick();
    intervalRef.current = setInterval(() => {
      if (!paused) tick();
    }, 2000);
    return () => clearInterval(intervalRef.current);
  }, [paused, tick]);

  const items = data?.items || [];
  const counts = items.reduce((acc, e) => {
    acc[e.kind] = (acc[e.kind] || 0) + 1;
    return acc;
  }, {});

  return (
    <div className="reveal" data-testid="recent-ingests-page">
      <PageHeader
        eyebrow="Live · Recent Ingests"
        title="The wire"
        sub="Live stream of receipts, memory labels, and promotion artifacts as they hit the shared infrastructure. Polls every 2 seconds. Visibility only — no state changes from this view."
        right={
          <button
            onClick={() => setPaused((p) => !p)}
            className={`btn-sharp px-3 py-2 border ${
              paused
                ? "border-rd-warn text-rd-warn"
                : "border-rd-chevelle text-rd-chevelle"
            }`}
            data-testid="recent-pause-toggle"
          >
            {paused ? "PAUSED · resume" : "● LIVE · pause"}
          </button>
        }
        testid="recent-ingests-header"
      />

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono">
          {err}
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5" data-testid="ingest-counters">
        <Counter label="Total events" value={items.length} />
        <Counter label="Receipts" value={counts.receipt || 0} />
        <Counter label="Memory labels" value={counts.memory_label || 0} />
        <Counter label="Promotion artifacts" value={counts.promotion_artifact || 0} />
      </div>

      {!data && <LoadingRow />}

      {data && items.length === 0 && (
        <Card className="px-6 py-10 text-center text-xs text-rd-dim uppercase tracking-widest">
          No events on the wire yet. Heartbeats and receipts will appear here as they arrive.
        </Card>
      )}

      {data && items.length > 0 && (
        <Card className="p-0 overflow-hidden" testid="tail-card">
          <div className="font-mono text-xs">
            {items.map((e, i) => {
              const meta = RUNTIME_META[e.runtime];
              const color = meta?.color || "#A1A1AA";
              const tone = e.kind === "promotion_artifact"
                ? "#FBBF24"
                : e.executed
                ? "#10B981"
                : "#A1A1AA";
              const key = `${e.kind}-${e.id || e.artifact_id || i}-${e.ts}`;
              return (
                <div
                  key={key}
                  className="grid grid-cols-12 gap-3 px-4 py-2 border-b border-rd-border last:border-b-0 hover:bg-rd-bg3"
                  data-testid={`tail-row-${i}`}
                >
                  <div className="col-span-2 text-rd-muted whitespace-nowrap">
                    {relTime(e.ts)}
                  </div>
                  <div className="col-span-1">
                    <Badge color={color}>{meta?.label || e.runtime}</Badge>
                  </div>
                  <div className="col-span-2">
                    <Badge color={tone}>
                      {e.kind.replace("_", " ").toUpperCase()}
                    </Badge>
                  </div>
                  <div className="col-span-7 text-rd-text truncate">
                    {e.kind === "receipt" && (
                      <>
                        <span className="text-rd-muted">{e.action}</span>
                        <span className="text-rd-dim"> · </span>
                        <span className="text-rd-dim">
                          {JSON.stringify(e.intent || {})}
                        </span>
                        {e.executed && (
                          <span className="text-rd-chevelle ml-2">
                            EXECUTED
                          </span>
                        )}
                      </>
                    )}
                    {e.kind === "memory_label" && (
                      <>
                        <span
                          className="font-bold"
                          style={{
                            color:
                              e.label === "safe"
                                ? "#10B981"
                                : e.label === "review"
                                ? "#FBBF24"
                                : "#EF4444",
                          }}
                        >
                          {e.label}
                        </span>
                        <span className="text-rd-dim"> — </span>
                        <span className="text-rd-muted">{e.reason}</span>
                      </>
                    )}
                    {e.kind === "promotion_artifact" && (
                      <>
                        <span className="text-rd-warn">
                          → {e.target_authority}
                        </span>
                        <span className="text-rd-dim"> · </span>
                        <span className="text-rd-muted">
                          ECE {e.metrics?.ece ?? "—"} · Brier{" "}
                          {e.metrics?.brier ?? "—"} · rows{" "}
                          {e.metrics?.resolved_rows ?? "—"}
                        </span>
                      </>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      )}
    </div>
  );
}

function Counter({ label, value }) {
  return (
    <div className="border border-rd-border bg-rd-bg2 p-3" data-testid={`counter-${label.toLowerCase().replace(/\s+/g, "-")}`}>
      <div className="text-[10px] text-rd-dim uppercase tracking-widest">
        {label}
      </div>
      <div className="font-display text-2xl font-bold tracking-tight text-rd-text mt-1">
        {value}
      </div>
    </div>
  );
}
