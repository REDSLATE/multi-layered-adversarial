import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import { ArrowsClockwise } from "@phosphor-icons/react";
import { toast } from "sonner";

/**
 * RosterSeatTile — generic single-role rotation tile.
 *
 * Reads/writes one role in the multi-seat roster via:
 *   GET  /api/admin/roster
 *   POST /api/admin/roster/assign   body: { role, brain }
 *   GET  /api/admin/roster/audit    items filtered to this role
 *
 * Used by Intents page to render Crypto Executor and Crypto Auditor
 * seats with the same visual treatment as the legacy single-row
 * ExecutorSeatTile/AuditorSeatTile, just without their dedicated
 * /executor and /auditor endpoints.
 *
 * Props:
 *   role            roster role name ("crypto", "crypto_auditor", ...)
 *   title           Display title (e.g. "Crypto Executor Seat")
 *   description     Help text shown under the title
 *   laneBadgeColor  Hex for the small lane badge ("#F97316" for crypto)
 *   laneBadgeText   Lane label ("CRYPTO LANE")
 *   icon            Phosphor icon component
 *   testid          Base test-id (defaults to `roster-seat-${role}`)
 */

const BRAINS = ["alpha", "camaro", "chevelle", "redeye"];
const BRAIN_COLOR = {
  alpha: "#3B82F6",
  camaro: "#F59E0B",
  chevelle: "#10B981",
  redeye: "#DC2626",
};

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

export default function RosterSeatTile({
  role,
  title,
  description,
  laneBadgeColor,
  laneBadgeText,
  icon: IconComponent,
  testid,
}) {
  const baseTestId = testid || `roster-seat-${role}`;
  const [roster, setRoster] = useState(null);
  const [audit, setAudit] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      const [r, a] = await Promise.all([
        api.get("/admin/roster"),
        api.get("/admin/roster/audit", { params: { limit: 20 } }),
      ]);
      setRoster(r.data);
      const filtered = (a.data?.items || []).filter(
        (row) => (row.payload?.role || row.role) === role,
      );
      setAudit(filtered.slice(0, 5));
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, [role]);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const assign = async (brain) => {
    setBusy(true);
    try {
      await api.post("/admin/roster/assign", { role, brain });
      toast.success(
        brain
          ? `${title} → ${brain.toUpperCase()}`
          : `${title} CLEARED`,
      );
      await load();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  };

  const holder = roster?.assignments?.[role] || null;
  const eligibility = roster?.eligibility || {};
  const isEligible = (brain) => {
    const row = eligibility?.[brain] || {};
    return row[role] !== false;
  };

  return (
    <Card className="p-4 mb-4" testid={baseTestId}>
      <div className="flex items-baseline justify-between gap-3 flex-wrap mb-3">
        <div className="flex items-baseline gap-2 flex-wrap">
          {IconComponent && (
            <IconComponent size={14} weight="bold" className="text-rd-text" />
          )}
          <span className="label-eyebrow">{title}</span>
          {laneBadgeText && (
            <Badge color={laneBadgeColor || "#A1A1AA"}>{laneBadgeText}</Badge>
          )}
          {holder ? (
            <Badge color={BRAIN_COLOR[holder] || "#A1A1AA"}>{holder.toUpperCase()}</Badge>
          ) : (
            <Badge color="#A1A1AA">EMPTY</Badge>
          )}
        </div>
        <button
          type="button"
          onClick={load}
          className="text-rd-dim hover:text-rd-text"
          aria-label="Refresh"
          data-testid={`${baseTestId}-refresh`}
        >
          <ArrowsClockwise size={11} weight="bold" />
        </button>
      </div>

      {description && (
        <div className="text-[11px] font-mono text-rd-dim mb-3 leading-relaxed">
          {description}
        </div>
      )}

      {err && (
        <div className="text-[11px] font-mono text-rd-danger mb-3" data-testid={`${baseTestId}-err`}>
          {err}
        </div>
      )}

      <div className="flex items-baseline gap-2 flex-wrap">
        <span className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
          rotate to
        </span>
        {BRAINS.map((b) => {
          const isCurrent = b === holder;
          const allowed = isEligible(b);
          return (
            <button
              key={b}
              type="button"
              onClick={() => assign(b)}
              disabled={busy || isCurrent || !allowed}
              className={`px-2 py-1 border text-[11px] uppercase tracking-widest font-mono ${
                isCurrent
                  ? "border-rd-text text-rd-text bg-rd-bg3"
                  : allowed
                  ? "border-rd-border text-rd-text hover:bg-rd-bg3"
                  : "border-rd-border text-rd-muted opacity-40 cursor-not-allowed"
              }`}
              title={allowed ? `Assign ${b} to ${role}` : `${b} not eligible for ${role}`}
              data-testid={`${baseTestId}-assign-${b}`}
              style={isCurrent ? { color: BRAIN_COLOR[b], borderColor: BRAIN_COLOR[b] } : undefined}
            >
              {b}
            </button>
          );
        })}
        {holder && (
          <button
            type="button"
            onClick={() => assign(null)}
            disabled={busy}
            className="px-2 py-1 border border-rd-warning text-rd-warning text-[11px] uppercase tracking-widest font-mono hover:bg-rd-bg3 ml-auto"
            data-testid={`${baseTestId}-clear`}
          >
            clear seat
          </button>
        )}
      </div>

      {audit.length > 0 && (
        <div className="mt-4 border-t border-rd-border pt-3">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono mb-2">
            recent rotations
          </div>
          <div className="divide-y divide-rd-border">
            {audit.map((r) => {
              const prev = r.payload?.from || null;
              const next = r.payload?.to || null;
              const reason = r.payload?.reason || "";
              return (
                <div
                  key={r.audit_id || `${r.ts}-${prev}-${next}`}
                  className="py-2 flex items-start justify-between gap-3"
                  data-testid={`${baseTestId}-audit-${r.audit_id || r.ts}`}
                >
                  <div className="flex-1 min-w-0">
                    <div className="font-mono text-[11px] text-rd-text">
                      <span style={{ color: BRAIN_COLOR[prev] || "#A1A1AA" }}>
                        {prev || "empty"}
                      </span>
                      <span className="mx-1.5 text-rd-dim">→</span>
                      <span style={{ color: BRAIN_COLOR[next] || "#A1A1AA" }}>
                        {next || "empty"}
                      </span>
                    </div>
                    {reason && (
                      <div className="text-[10px] text-rd-muted mt-0.5 truncate">
                        {reason}
                      </div>
                    )}
                  </div>
                  <div className="text-right text-[10px] font-mono text-rd-muted shrink-0">
                    <div>{relTime(r.ts)}</div>
                    {r.actor && (
                      <div className="text-rd-dim truncate max-w-[140px]">{r.actor}</div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {audit.length === 0 && roster && (
        <div className="mt-3 text-[10px] font-mono text-rd-dim">
          No rotations recorded yet for this seat.
        </div>
      )}
    </Card>
  );
}
