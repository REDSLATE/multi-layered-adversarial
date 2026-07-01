import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge, EmptyState } from "@/components/ui-bits";
import { ArrowsClockwise, Crown, ArrowsCounterClockwise } from "@phosphor-icons/react";
import { toast } from "sonner";

const BRAIN_COLOR = {
  camino: "#3B82F6",
  barracuda: "#F59E0B",
  hellcat: "#10B981",
  gto: "#DC2626",
};

const LANES = ["equity", "crypto"];
const ROLES = ["strategist", "governor", "executor", "auditor"];

// Angel-name overlay for each (lane, role). Doctrine constants
// (matches trader/state.py::DEFAULT_SEATS commentary).
const ANGELS = {
  equity: {
    strategist: "Raziel",
    governor:   "Nuriel",
    executor:   "Paschar",
    auditor:    "Sariel",
  },
  crypto: {
    strategist: "Remiel",
    governor:   "Cassiel",
    executor:   "Israfel",
    auditor:    "Zadkiel",
  },
};

/**
 * TraderSeatViewer — visualizes who holds each of the 8 seats
 * (2 lanes × 4 roles) in the Sidecar Trader's in-memory cache.
 *
 * Reads: GET /api/admin/trader/status  → state.seats
 * Writes: POST /api/admin/trader/seed-seats (idempotent, reseeds
 *         Mongo with the operator-canonical pairings)
 *         POST /api/admin/trader/reload-caches (poke the state
 *         refresher without waiting for the 60s interval)
 *
 * This is READ-ONLY per lane×role rotation. To change a seat's
 * holder, operators must still edit `seat_registry` in Mongo — the
 * trader picks that up on the next refresh (or immediately after
 * `reload-caches`).
 */
export default function TraderSeatViewer() {
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      const s = await api.get("/admin/trader/status");
      setState(s.data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const seedSeats = async () => {
    if (!confirm("Reseed operator-canonical angel↔brain pairings into seat_registry?")) return;
    setBusy(true);
    try {
      const res = await api.post("/admin/trader/seed-seats");
      toast.success(`Seeded ${res?.data?.count ?? 8} seats`);
      // Force the cache to refresh right away.
      await api.post("/admin/trader/reload-caches").catch(() => {});
      await load();
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      toast.error(`Seed failed: ${d}`);
      setErr(d);
    } finally {
      setBusy(false);
    }
  };

  const reload = async () => {
    setBusy(true);
    try {
      await api.post("/admin/trader/reload-caches");
      await load();
      toast.success("Cache refresh queued");
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      toast.error(`Reload failed: ${d}`);
      setErr(d);
    } finally {
      setBusy(false);
    }
  };

  const seats = state?.state?.seats || {};
  const govMult = state?.state?.governor_multiplier || {};
  const lastRefresh = state?.state?.last_refresh_ok_ts;
  const refreshErr = state?.state?.last_refresh_error;

  return (
    <Card className="mb-6" testid="trader-seat-viewer">
      {/* Header */}
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 text-rd-dim">
            <Crown size={16} weight="duotone" />
          </div>
          <div>
            <div className="font-display text-base font-bold text-rd-text leading-none">
              Trader Seats
            </div>
            <div className="text-[11px] text-rd-muted mt-1 font-mono leading-relaxed">
              4 roles × 2 lanes. The Executor holds broker authority. Angels are constants; brains rotate.
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={reload}
            disabled={busy}
            className="p-1.5 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text disabled:opacity-40"
            title="Force cache refresh"
            data-testid="trader-seat-reload-caches"
          >
            <ArrowsCounterClockwise size={12} weight="bold" />
          </button>
          <button
            onClick={load}
            disabled={busy}
            className="p-1.5 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text disabled:opacity-40"
            title="Reload view"
            data-testid="trader-seat-refresh"
          >
            <ArrowsClockwise size={12} weight="bold" className={busy ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {/* Cache health strip */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2 mb-4" data-testid="trader-seat-cache-health">
        <div className="border border-rd-border bg-rd-bg px-2 py-1.5">
          <div className="text-[9px] uppercase tracking-widest text-rd-dim">Last mongo refresh</div>
          <div className="font-mono text-[11px] text-rd-text mt-0.5">
            {lastRefresh ? new Date(lastRefresh).toLocaleTimeString() : (
              <span className="text-rd-muted">never (using sqlite/defaults)</span>
            )}
          </div>
        </div>
        <div className="border border-rd-border bg-rd-bg px-2 py-1.5">
          <div className="text-[9px] uppercase tracking-widest text-rd-dim">Refresh error</div>
          <div className="font-mono text-[11px] mt-0.5 truncate" title={refreshErr || ""}>
            {refreshErr ? (
              <span className="text-rd-warn">{refreshErr}</span>
            ) : (
              <span className="text-rd-success">clean</span>
            )}
          </div>
        </div>
      </div>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-3 text-xs font-mono" data-testid="trader-seat-error">
          {err}
        </div>
      )}

      {/* Grid: 2 lanes × 4 roles */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3" data-testid="trader-seat-grid">
        {LANES.map((lane) => (
          <div
            key={lane}
            className="border border-rd-border"
            data-testid={`trader-seat-lane-${lane}`}
          >
            <div className="px-2 py-1.5 bg-rd-bg border-b border-rd-border flex items-center justify-between">
              <span className="text-[10px] uppercase tracking-widest text-rd-text font-mono">
                {lane}
              </span>
              {govMult[lane] !== undefined && (
                <span className="text-[10px] font-mono text-rd-muted">
                  gov ×{Number(govMult[lane]).toFixed(2)}
                </span>
              )}
            </div>
            <div className="divide-y divide-rd-border">
              {ROLES.map((role) => {
                const holder = seats[lane]?.[role];
                const angel = ANGELS[lane]?.[role];
                const isExecutor = role === "executor";
                return (
                  <SeatRow
                    key={`${lane}-${role}`}
                    lane={lane}
                    role={role}
                    angel={angel}
                    holder={holder}
                    isExecutor={isExecutor}
                  />
                );
              })}
            </div>
          </div>
        ))}
      </div>

      {/* Seed button */}
      <div className="flex items-center justify-end mt-4">
        <button
          onClick={seedSeats}
          disabled={busy}
          data-testid="trader-seat-seed"
          className="px-3 py-1.5 text-[11px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text disabled:opacity-40"
        >
          {busy ? "Working…" : "Reseed canonical pairings"}
        </button>
      </div>
    </Card>
  );
}

function SeatRow({ lane, role, angel, holder, isExecutor }) {
  return (
    <div
      className={
        "px-2 py-1.5 grid grid-cols-3 gap-2 items-center text-[11px] font-mono " +
        (isExecutor ? "bg-rd-accent/5" : "")
      }
      data-testid={`trader-seat-cell-${lane}-${role}`}
    >
      <div className="col-span-1">
        <div className="text-rd-text uppercase tracking-wider text-[10px]">{role}</div>
        <div className="text-rd-dim text-[9px]">{angel}</div>
      </div>
      <div className="col-span-1 text-center">
        {holder ? (
          <span
            className="uppercase font-bold tracking-wide"
            style={{ color: BRAIN_COLOR[holder] || "#A1A1AA" }}
            data-testid={`trader-seat-holder-${lane}-${role}`}
          >
            {holder}
          </span>
        ) : (
          <span className="text-rd-warn">vacant</span>
        )}
      </div>
      <div className="col-span-1 text-right">
        {isExecutor && (
          <Badge color="#DC2626">EXEC</Badge>
        )}
      </div>
    </div>
  );
}
