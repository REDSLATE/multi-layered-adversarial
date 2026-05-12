import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";

/**
 * Public Traffic — operator-only verification page.
 *
 * Shows live traffic against `/api/public/*` during the risedual.ai
 * cutover. Two blocks:
 *   1. Summary tiles (last N hours): total count, by-tier, by-status, p50/p95/p99 latency.
 *   2. Tail table (last 200 rows): endpoint, status, tier, latency_ms, ts.
 *
 * Polls every 5 seconds while the page is mounted.
 */
const STATUS_COLORS = {
  200: "#10B981",
  401: "#FBBF24",
  403: "#FBBF24",
  404: "#71717A",
  422: "#F97316",
  500: "#DC2626",
  502: "#DC2626",
  503: "#DC2626",
};

const TIER_COLORS = {
  free: "#71717A",
  starter: "#71717A",
  pro: "#3B82F6",
  pro_max: "#A855F7",
  "(unset)": "#52525B",
};

function statusColor(s) {
  return STATUS_COLORS[s] || "#52525B";
}

function tierColor(t) {
  return TIER_COLORS[t] || "#52525B";
}

function fmtRelative(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  const dt = (Date.now() - t) / 1000;
  if (dt < 5) return "just now";
  if (dt < 60) return `${Math.round(dt)}s ago`;
  if (dt < 3600) return `${Math.round(dt / 60)}m ago`;
  return `${Math.round(dt / 3600)}h ago`;
}

export default function PublicTraffic() {
  const [summary, setSummary] = useState(null);
  const [rows, setRows] = useState([]);
  const [hours, setHours] = useState(24);
  const [pathFilter, setPathFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [tierFilter, setTierFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [clearing, setClearing] = useState(false);

  async function load() {
    setError(null);
    try {
      const params = new URLSearchParams({ limit: "200" });
      if (pathFilter) params.set("path_contains", pathFilter);
      if (statusFilter) params.set("status", statusFilter);
      if (tierFilter) params.set("tier", tierFilter);
      const [sumRes, listRes] = await Promise.all([
        api.get(`/admin/public-traffic/summary?hours=${hours}`),
        api.get(`/admin/public-traffic?${params.toString()}`),
      ]);
      setSummary(sumRes.data);
      setRows(listRes.data.items || []);
    } catch (e) {
      setError(e?.response?.data?.detail || "Failed to load public traffic");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hours, pathFilter, statusFilter, tierFilter]);

  async function handleClear() {
    if (!window.confirm("Clear ALL public-traffic logs?")) return;
    setClearing(true);
    try {
      await api.delete("/admin/public-traffic");
      await load();
    } finally {
      setClearing(false);
    }
  }

  const totals = summary?.total || 0;

  return (
    <div className="space-y-6" data-testid="page-public-traffic">
      <PageHeader
        title="Public Traffic"
        subtitle="Live verification of /api/public/* during the risedual.ai cutover. Auto-refreshes every 5s."
      />

      {error && (
        <Card>
          <div className="text-rd-danger text-sm font-mono">{error}</div>
        </Card>
      )}

      {/* Controls */}
      <Card>
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <label className="text-rd-dim text-xs uppercase tracking-widest">Window</label>
            <select
              data-testid="public-traffic-hours"
              value={hours}
              onChange={(e) => setHours(parseInt(e.target.value, 10))}
              className="bg-rd-bg3 border border-rd-border text-rd-text text-xs px-2 py-1 rounded font-mono"
            >
              <option value="1">1h</option>
              <option value="6">6h</option>
              <option value="24">24h</option>
              <option value="72">72h</option>
              <option value="168">7d</option>
            </select>
          </div>

          <div className="flex items-center gap-2">
            <label className="text-rd-dim text-xs uppercase tracking-widest">Path</label>
            <input
              data-testid="public-traffic-path-filter"
              value={pathFilter}
              onChange={(e) => setPathFilter(e.target.value)}
              placeholder="signals, digest, chat..."
              className="bg-rd-bg3 border border-rd-border text-rd-text text-xs px-2 py-1 rounded font-mono w-40"
            />
          </div>

          <div className="flex items-center gap-2">
            <label className="text-rd-dim text-xs uppercase tracking-widest">Status</label>
            <select
              data-testid="public-traffic-status-filter"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="bg-rd-bg3 border border-rd-border text-rd-text text-xs px-2 py-1 rounded font-mono"
            >
              <option value="">all</option>
              <option value="200">200</option>
              <option value="401">401</option>
              <option value="403">403</option>
              <option value="422">422</option>
              <option value="502">502</option>
              <option value="503">503</option>
            </select>
          </div>

          <div className="flex items-center gap-2">
            <label className="text-rd-dim text-xs uppercase tracking-widest">Tier</label>
            <select
              data-testid="public-traffic-tier-filter"
              value={tierFilter}
              onChange={(e) => setTierFilter(e.target.value)}
              className="bg-rd-bg3 border border-rd-border text-rd-text text-xs px-2 py-1 rounded font-mono"
            >
              <option value="">all</option>
              <option value="free">free</option>
              <option value="starter">starter</option>
              <option value="pro">pro</option>
              <option value="pro_max">pro_max</option>
              <option value="(unset)">(unset)</option>
            </select>
          </div>

          <button
            data-testid="public-traffic-clear"
            onClick={handleClear}
            disabled={clearing}
            className="ml-auto px-3 py-1 text-xs font-mono uppercase tracking-widest text-rd-dim hover:text-rd-danger border border-rd-border hover:border-rd-danger rounded transition-colors"
          >
            {clearing ? "clearing..." : "clear log"}
          </button>
        </div>
      </Card>

      {/* Summary tiles */}
      {summary && (
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
          <Card testid="public-traffic-total">
            <div className="label-eyebrow mb-1">Total</div>
            <div className="text-rd-text text-3xl font-mono">{totals}</div>
            <div className="text-rd-dim text-[10px] font-mono mt-1">
              last {summary.hours}h
            </div>
          </Card>

          <Card testid="public-traffic-latency">
            <div className="label-eyebrow mb-1">Latency</div>
            <div className="space-y-1 text-xs font-mono">
              <div className="flex justify-between">
                <span className="text-rd-dim">p50</span>
                <span className="text-rd-text">{summary.latency_p50_ms ?? "—"} ms</span>
              </div>
              <div className="flex justify-between">
                <span className="text-rd-dim">p95</span>
                <span className="text-rd-text">{summary.latency_p95_ms ?? "—"} ms</span>
              </div>
              <div className="flex justify-between">
                <span className="text-rd-dim">p99</span>
                <span className="text-rd-text">{summary.latency_p99_ms ?? "—"} ms</span>
              </div>
            </div>
          </Card>

          <Card testid="public-traffic-by-tier">
            <div className="label-eyebrow mb-2">By Tier</div>
            {summary.by_tier.length === 0 ? (
              <div className="text-rd-dim text-xs font-mono">—</div>
            ) : (
              <div className="space-y-1">
                {summary.by_tier.map((r) => (
                  <div
                    key={r.tier}
                    className="flex justify-between items-center text-[11px] font-mono"
                  >
                    <Badge color={tierColor(r.tier)}>{r.tier}</Badge>
                    <span className="text-rd-text">{r.count}</span>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card testid="public-traffic-by-status">
            <div className="label-eyebrow mb-2">By Status</div>
            {summary.by_status.length === 0 ? (
              <div className="text-rd-dim text-xs font-mono">—</div>
            ) : (
              <div className="space-y-1">
                {summary.by_status.map((r) => (
                  <div
                    key={r.status}
                    className="flex justify-between items-center text-[11px] font-mono"
                  >
                    <Badge color={statusColor(r.status)}>{r.status}</Badge>
                    <span className="text-rd-text">{r.count}</span>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>
      )}

      {/* By Endpoint */}
      {summary?.by_endpoint?.length > 0 && (
        <Card testid="public-traffic-by-endpoint">
          <div className="label-eyebrow mb-3">By Endpoint</div>
          <div className="space-y-1.5">
            {summary.by_endpoint.map((r) => {
              const pct = totals ? (r.count / totals) * 100 : 0;
              return (
                <div key={r.endpoint} className="flex items-center gap-3 text-[11px] font-mono">
                  <span className="text-rd-text w-64 truncate">{r.endpoint}</span>
                  <div className="flex-1 h-1.5 bg-rd-bg3 relative rounded">
                    <div
                      className="absolute top-0 left-0 h-1.5 rounded bg-rd-text"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className="text-rd-text w-12 text-right">{r.count}</span>
                  <span className="text-rd-dim w-16 text-right">{pct.toFixed(1)}%</span>
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {/* Tail table */}
      <Card testid="public-traffic-tail">
        <div className="flex items-center justify-between mb-3">
          <div className="label-eyebrow">Tail · last {rows.length}</div>
          <div className="text-rd-dim text-[10px] font-mono">auto-refresh 5s</div>
        </div>
        {loading && rows.length === 0 ? (
          <LoadingRow />
        ) : rows.length === 0 ? (
          <EmptyState message="No public-API requests have been logged yet. Once risedual.ai starts calling, traffic appears here within 5 seconds." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-mono">
              <thead>
                <tr className="text-rd-dim uppercase tracking-widest border-b border-rd-border">
                  <th className="text-left py-2 pr-3">ts</th>
                  <th className="text-left pr-3">method</th>
                  <th className="text-left pr-3">path</th>
                  <th className="text-left pr-3">status</th>
                  <th className="text-left pr-3">tier</th>
                  <th className="text-right pr-3">latency</th>
                  <th className="text-left pr-3">caller_ip</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr
                    key={`${r.ts}-${i}`}
                    className="border-b border-rd-border/50 hover:bg-rd-bg3/40"
                    data-testid="public-traffic-row"
                  >
                    <td className="py-1.5 pr-3 text-rd-dim">{fmtRelative(r.ts)}</td>
                    <td className="pr-3 text-rd-text">{r.method}</td>
                    <td className="pr-3 text-rd-text truncate max-w-md">
                      {r.path}
                      {r.query ? <span className="text-rd-dim">?{r.query}</span> : null}
                    </td>
                    <td className="pr-3">
                      <Badge color={statusColor(r.status)}>{r.status}</Badge>
                    </td>
                    <td className="pr-3">
                      <Badge color={tierColor(r.tier || "(unset)")}>
                        {r.tier || "(unset)"}
                      </Badge>
                    </td>
                    <td className="pr-3 text-right text-rd-text">
                      {r.latency_ms} ms
                    </td>
                    <td className="pr-3 text-rd-dim">{r.caller_ip || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
