import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, BACKEND_URL } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState } from "@/components/ui-bits";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toast } from "sonner";
import {
  Brain, Download, ArrowsClockwise, Funnel, CheckCircle,
  XCircle, Lightning, Rocket, ChartBar, Archive,
} from "@phosphor-icons/react";

const POSITIONS = ["DEC", "EXE", "GOV", "ADV", "OPP", "AUD", "NONE"];
const POSITION_LABEL = {
  DEC: "Decider", EXE: "Executor", GOV: "Governor",
  ADV: "Advisor", OPP: "Opponent", AUD: "Auditor", NONE: "Unassigned",
};
const POSITION_COLOR = {
  DEC: "#3B82F6", EXE: "#F59E0B", GOV: "#10B981",
  ADV: "#A78BFA", OPP: "#DC2626", AUD: "#EC4899", NONE: "#71717A",
};
const EVENT_TYPES = [
  "intent_ingested", "gate_pass", "gate_fail",
  "order_routed", "order_rejected",
  "position_opened", "position_closed",
  "hypothesis_request", "rotation",
];
const EVENT_COLOR = {
  intent_ingested: "#A78BFA", gate_pass: "#10B981", gate_fail: "#DC2626",
  order_routed: "#F59E0B", order_rejected: "#DC2626",
  position_opened: "#3B82F6", position_closed: "#EC4899",
  hypothesis_request: "#22D3EE", rotation: "#A1A1AA",
};
const BRAIN_COLOR = {
  camino: "#3B82F6", barracuda: "#F59E0B",
  hellcat: "#10B981", gto: "#DC2626",
};

// Static option lists for the McShelly filter pickers. Hoisted to
// module scope so the same array reference is reused across renders,
// preserving any downstream memoization in <FilterPicker>.
const EVENT_FILTER_OPTIONS = ["", ...EVENT_TYPES];
const POSITION_FILTER_OPTIONS = ["", ...POSITIONS];
const BRAIN_FILTER_OPTIONS = ["", "camino", "barracuda", "hellcat", "gto"];
const OUTCOME_FILTER_OPTIONS = ["", "pending", "executed", "win", "loss", "pass", "fail", "blocked", "rejected"];
const WINDOW_FILTER_OPTIONS = ["1", "6", "24", "72", "168", "720"];
const WINDOW_FILTER_LABELS = { "1": "1h", "6": "6h", "24": "24h", "72": "3d", "168": "7d", "720": "30d" };

function relTime(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export default function McShelly() {
  const [events, setEvents] = useState([]);
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [filters, setFilters] = useState({
    event_type: "",
    position: "",
    brain: "",
    symbol: "",
    outcome: "",
    since_hours: 168,
    limit: 100,
  });
  const [backfillBusy, setBackfillBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = {};
      Object.entries(filters).forEach(([k, v]) => {
        if (v !== "" && v != null) params[k] = v;
      });
      const [evs, st] = await Promise.all([
        api.get("/mc/shelly/", { params }),
        api.get("/mc/shelly/stats", { params: { since_hours: filters.since_hours } }),
      ]);
      setEvents(evs.data?.items || []);
      setStats(st.data);
      setErr("");
    } catch (e) {
      setErr(e?.message || "Failed to load");
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const runBackfill = async () => {
    if (!window.confirm("Replay all existing intents/gates/receipts into MC Shelly? Idempotent — safe to re-run.")) return;
    setBackfillBusy(true);
    try {
      const res = await api.post("/mc/shelly/backfill");
      const s = res.data?.stats || {};
      toast.success(
        `Backfilled · ${s.intents_ingested || 0} intents · ${s.gate_passes || 0}P / ${s.gate_fails || 0}F · ${s.orders_routed || 0} orders · ${s.outcomes_recorded || 0} outcomes · skipped ${s.skipped_already_present || 0}`
      );
      load();
    } catch (e) {
      toast.error(e?.message || "Backfill failed");
    } finally {
      setBackfillBusy(false);
    }
  };

  const downloadExport = () => {
    const qs = new URLSearchParams();
    if (filters.event_type) qs.set("event_type", filters.event_type);
    if (filters.position) qs.set("position", filters.position);
    if (filters.since_hours) qs.set("since_hours", filters.since_hours);
    const url = `${BACKEND_URL}/api/mc/shelly/export.jsonl?${qs.toString()}`;
    const tok = localStorage.getItem("risedual_access_token");
    // Use fetch + blob so we can attach the Bearer header (download
    // attribute on <a> doesn't support headers).
    fetch(url, { headers: { Authorization: `Bearer ${tok}` } })
      .then((r) => r.blob())
      .then((blob) => {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `mc_shelly_${new Date().toISOString().replace(/[:.]/g, "-")}.jsonl`;
        document.body.appendChild(a);
        a.click();
        a.remove();
      })
      .catch((e) => toast.error(`Export failed · ${e.message}`));
  };

  const setF = (k, v) => setFilters((f) => ({ ...f, [k]: v }));

  return (
    <div className="space-y-6" data-testid="mc-shelly-page">
      <PageHeader
        eyebrow="Memory · MC Shelly"
        title="MC Memory Store"
        sub="Mission Control's labeled memory of every intent, gate decision, and broker outcome — tagged with the position each brain held at the moment. Mongo-backed, file-exportable, training-ready."
        right={
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={runBackfill}
              disabled={backfillBusy}
              data-testid="shelly-backfill-btn"
            >
              <Archive size={12} weight="bold" className="mr-1.5" />
              {backfillBusy ? "Replaying…" : "Replay Backfill"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={load}
              disabled={loading}
              data-testid="shelly-reload-btn"
            >
              <ArrowsClockwise size={12} weight="bold" className={`mr-1.5 ${loading ? "animate-spin" : ""}`} />
              Reload
            </Button>
            <Button
              size="sm"
              onClick={downloadExport}
              data-testid="shelly-export-btn"
              className="bg-emerald-500 hover:bg-emerald-400 text-black"
            >
              <Download size={12} weight="bold" className="mr-1.5" />
              Export JSONL
            </Button>
          </div>
        }
        testid="mc-shelly-header"
      />

      {/* Stats Row */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3" data-testid="shelly-stats">
          <Stat label="Total Events" value={stats.total_events_all_time?.toLocaleString() || "0"} icon={Brain} testid="stat-total" />
          <Stat label={`Last ${stats.window_hours}h`} value={stats.events_in_window?.toLocaleString() || "0"} icon={ChartBar} testid="stat-window" />
          <Stat
            label="Top event"
            value={(stats.by_event_type?.[0]?.event_type || "—").replace(/_/g, " ")}
            sub={`${stats.by_event_type?.[0]?.count || 0}× in window`}
            color={EVENT_COLOR[stats.by_event_type?.[0]?.event_type] || "#A1A1AA"}
            testid="stat-top-event"
          />
          <Stat
            label="Brain track record"
            value={
              stats.win_loss_by_brain?.length
                ? stats.win_loss_by_brain.slice(0, 1).map(b => `${b.brain}: ${b.wins}W/${b.losses}L`).join(" · ")
                : "no resolved outcomes yet"
            }
            testid="stat-wl"
          />
        </div>
      )}

      {/* By-position pass rate */}
      {stats?.by_position?.length > 0 && (
        <Card testid="shelly-by-position-card">
          <div className="label-eyebrow mb-3">Gate pass rate · by position held</div>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            {stats.by_position.map((p) => (
              <div key={p.position} className="border border-rd-border bg-rd-bg p-3" data-testid={`pos-${p.position}`}>
                <div className="flex items-baseline justify-between mb-1">
                  <span className="text-[10px] uppercase tracking-widest font-mono" style={{ color: POSITION_COLOR[p.position] }}>
                    {p.position}
                  </span>
                  <span className="text-[10px] text-rd-dim">{POSITION_LABEL[p.position]}</span>
                </div>
                <div className="text-xl font-black tracking-tight text-rd-text">
                  {p.pass_rate_pct != null ? `${p.pass_rate_pct}%` : "—"}
                </div>
                <div className="text-[10px] font-mono text-rd-muted">
                  <span className="text-emerald-400">{p.passes}P</span> · <span className="text-rose-400">{p.fails}F</span>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* Filters */}
      <Card testid="shelly-filters-card">
        <div className="flex items-baseline gap-2 mb-3">
          <Funnel size={12} weight="bold" className="text-rd-dim" />
          <span className="label-eyebrow">Filters</span>
          <button
            onClick={() => setFilters({ event_type: "", position: "", brain: "", symbol: "", outcome: "", since_hours: 168, limit: 100 })}
            className="ml-auto text-[10px] font-mono uppercase tracking-widest text-rd-dim hover:text-rd-text"
            data-testid="shelly-filters-clear"
          >
            clear
          </button>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2 text-xs font-mono">
          <FilterPicker label="event" value={filters.event_type} options={EVENT_FILTER_OPTIONS} onChange={(v) => setF("event_type", v)} testid="f-event" />
          <FilterPicker label="position" value={filters.position} options={POSITION_FILTER_OPTIONS} onChange={(v) => setF("position", v)} testid="f-position" />
          <FilterPicker label="brain" value={filters.brain} options={BRAIN_FILTER_OPTIONS} onChange={(v) => setF("brain", v)} testid="f-brain" />
          <div data-testid="f-symbol">
            <div className="text-[9px] uppercase tracking-widest text-rd-dim mb-1">symbol</div>
            <Input value={filters.symbol} onChange={(e) => setF("symbol", e.target.value.toUpperCase())} placeholder="any" className="bg-rd-bg border-rd-border h-8 text-xs font-mono uppercase" maxLength={10} />
          </div>
          <FilterPicker label="outcome" value={filters.outcome} options={OUTCOME_FILTER_OPTIONS} onChange={(v) => setF("outcome", v)} testid="f-outcome" />
          <FilterPicker
            label="window"
            value={String(filters.since_hours)}
            options={WINDOW_FILTER_OPTIONS}
            optionLabels={WINDOW_FILTER_LABELS}
            onChange={(v) => setF("since_hours", Number(v))}
            testid="f-window"
          />
        </div>
      </Card>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 text-xs font-mono" data-testid="shelly-error">{err}</div>
      )}

      {/* Events Table */}
      <Card testid="shelly-events-card">
        <div className="flex items-baseline justify-between mb-2">
          <span className="label-eyebrow">Events · {events.length}</span>
          <span className="text-[10px] font-mono text-rd-muted">auto-reloads every 30s</span>
        </div>
        {events.length === 0 ? (
          <EmptyState message="No events match these filters. Try widening the window or clearing filters." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-mono">
              <thead>
                <tr className="text-rd-dim border-b border-rd-border">
                  <th className="text-left py-2 px-2 uppercase tracking-widest text-[9px]">When</th>
                  <th className="text-left py-2 px-2 uppercase tracking-widest text-[9px]">Event</th>
                  <th className="text-left py-2 px-2 uppercase tracking-widest text-[9px]">Brain</th>
                  <th className="text-left py-2 px-2 uppercase tracking-widest text-[9px]">Pos</th>
                  <th className="text-left py-2 px-2 uppercase tracking-widest text-[9px]">Symbol</th>
                  <th className="text-left py-2 px-2 uppercase tracking-widest text-[9px]">Action</th>
                  <th className="text-left py-2 px-2 uppercase tracking-widest text-[9px]">Outcome</th>
                  <th className="text-left py-2 px-2 uppercase tracking-widest text-[9px]">Detail</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.event_id} className="border-b border-rd-border hover:bg-rd-bg/40" data-testid={`shelly-row-${e.event_id}`}>
                    <td className="py-2 px-2 text-rd-muted">{relTime(e.ts)}</td>
                    <td className="py-2 px-2">
                      <span style={{ color: EVENT_COLOR[e.event_type] || "#A1A1AA" }}>
                        {(e.event_type || "?").replace(/_/g, " ")}
                      </span>
                    </td>
                    <td className="py-2 px-2" style={{ color: BRAIN_COLOR[e.brain] || "#A1A1AA" }}>
                      {e.brain || "—"}
                    </td>
                    <td className="py-2 px-2">
                      {e.position_at_event && e.position_at_event !== "NONE" ? (
                        <Badge color={POSITION_COLOR[e.position_at_event] || "#A1A1AA"}>
                          {e.position_at_event}
                        </Badge>
                      ) : (
                        <span className="text-rd-dim">—</span>
                      )}
                    </td>
                    <td className="py-2 px-2 text-rd-text">{e.symbol || "—"}</td>
                    <td className="py-2 px-2 text-rd-text">{e.action || "—"}</td>
                    <td className="py-2 px-2">
                      {e.outcome ? (
                        <span style={{
                          color: ["win", "pass", "executed"].includes(e.outcome) ? "#10B981" :
                                 ["loss", "fail", "rejected", "blocked"].includes(e.outcome) ? "#DC2626" : "#A1A1AA",
                        }}>
                          {e.outcome}
                        </span>
                      ) : "—"}
                    </td>
                    <td className="py-2 px-2 text-rd-muted max-w-[400px] truncate" title={e.rationale || e.gate_name || e.error_reason || ""}>
                      {e.gate_name && <span className="text-rd-text mr-2">{e.gate_name}</span>}
                      {e.rationale || e.error_reason || ""}
                    </td>
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

function Stat({ label, value, sub, icon: Icon, color, testid }) {
  return (
    <Card className="!p-3" testid={testid}>
      <div className="flex items-baseline gap-2 mb-1">
        {Icon && <Icon size={11} weight="bold" className="text-rd-dim" />}
        <span className="text-[9px] uppercase tracking-widest text-rd-dim">{label}</span>
      </div>
      <div className="text-lg font-black tracking-tight uppercase truncate" style={color ? { color } : { color: "var(--rd-text, #fff)" }}>
        {value}
      </div>
      {sub && <div className="text-[10px] font-mono text-rd-muted">{sub}</div>}
    </Card>
  );
}

function FilterPicker({ label, value, options, optionLabels, onChange, testid }) {
  return (
    <div data-testid={testid}>
      <div className="text-[9px] uppercase tracking-widest text-rd-dim mb-1">{label}</div>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full bg-rd-bg border border-rd-border h-8 px-2 text-xs font-mono uppercase text-rd-text focus:border-rd-accent focus:outline-none"
      >
        {options.map((o) => (
          <option key={o} value={o}>
            {o === "" ? "any" : (optionLabels?.[o] || o)}
          </option>
        ))}
      </select>
    </div>
  );
}
