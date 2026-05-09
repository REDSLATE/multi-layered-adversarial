import React, { useEffect, useState } from "react";
import { api, RUNTIME_META, fmtTime } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";

const RT_FILTERS = ["all", "alpha", "camaro", "chevelle"];

export default function Receipts() {
  const [filter, setFilter] = useState("all");
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    setData(null);
    (async () => {
      try {
        const params = new URLSearchParams({ limit: "200" });
        if (filter !== "all") params.set("runtime", filter);
        const { data } = await api.get(`/shared/receipts?${params.toString()}`);
        setData(data);
      } catch (e) {
        setErr(e?.response?.data?.detail || e.message);
      }
    })();
  }, [filter]);

  return (
    <div className="reveal" data-testid="receipts-page">
      <PageHeader
        eyebrow="Shared · ADL Receipts"
        title="Append-only decision ledger"
        sub="Every runtime writes intent here. observed=true / executed=false in observation mode. Receipts are tagged by runtime but stored in a shared collection."
        right={
          <div className="flex gap-2" data-testid="receipts-filter-bar">
            {RT_FILTERS.map((rt) => {
              const active = filter === rt;
              const color = rt === "all" ? "#FBBF24" : RUNTIME_META[rt]?.color;
              return (
                <button
                  key={rt}
                  onClick={() => setFilter(rt)}
                  data-testid={`receipts-filter-${rt}`}
                  className={`btn-sharp px-3 py-2 border ${
                    active
                      ? "bg-zinc-100 text-zinc-900 border-zinc-100"
                      : "border-rd-border text-rd-muted hover:text-rd-text hover:border-rd-borderStrong"
                  }`}
                  style={active ? undefined : { borderLeftColor: color, borderLeftWidth: 2 }}
                >
                  {rt}
                </button>
              );
            })}
          </div>
        }
        testid="receipts-header"
      />

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono">{err}</div>
      )}

      {!data && <LoadingRow />}

      {data && data.items.length === 0 && <EmptyState message="No receipts for this filter" testid="receipts-empty" />}

      {data && data.items.length > 0 && (
        <Card className="p-0 overflow-hidden" testid="receipts-table-card">
          <div className="overflow-x-auto">
            <table className="w-full text-xs font-mono" data-testid="receipts-table">
              <thead>
                <tr className="bg-rd-bg3 text-rd-dim uppercase tracking-widest">
                  <th className="text-left px-4 py-3 border-b border-rd-border">Time (UTC)</th>
                  <th className="text-left px-4 py-3 border-b border-rd-border">Runtime</th>
                  <th className="text-left px-4 py-3 border-b border-rd-border">Action</th>
                  <th className="text-left px-4 py-3 border-b border-rd-border">Symbol</th>
                  <th className="text-right px-4 py-3 border-b border-rd-border">Qty</th>
                  <th className="text-right px-4 py-3 border-b border-rd-border">Conf.</th>
                  <th className="text-left px-4 py-3 border-b border-rd-border">Status</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((r, i) => {
                  const meta = RUNTIME_META[r.runtime];
                  return (
                    <tr
                      key={r.id}
                      className="hover:bg-rd-bg3 border-b border-rd-border last:border-b-0"
                      data-testid={`receipt-row-${i}`}
                    >
                      <td className="px-4 py-2.5 text-rd-muted whitespace-nowrap">{fmtTime(r.timestamp)}</td>
                      <td className="px-4 py-2.5">
                        <span style={{ color: meta?.color }} className="font-bold">
                          {meta?.label || r.runtime}
                        </span>
                      </td>
                      <td className="px-4 py-2.5 uppercase">{r.action}</td>
                      <td className="px-4 py-2.5">{r.intent?.symbol || "—"}</td>
                      <td className="px-4 py-2.5 text-right">{r.intent?.qty ?? "—"}</td>
                      <td className="px-4 py-2.5 text-right">
                        {r.intent?.confidence != null ? r.intent.confidence.toFixed(3) : "—"}
                      </td>
                      <td className="px-4 py-2.5">
                        <Badge color={r.executed ? "#EF4444" : "#71717A"}>
                          {r.executed ? "EXECUTED" : "OBSERVED"}
                        </Badge>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="px-4 py-3 border-t border-rd-border text-[10px] text-rd-dim uppercase tracking-widest">
            {data.count} records · shared_adl_receipts
          </div>
        </Card>
      )}
    </div>
  );
}
