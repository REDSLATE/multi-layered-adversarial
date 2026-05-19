import React, { useCallback, useEffect, useState } from "react";
import { api, RUNTIME_META, fmtTime, relTime } from "@/lib/api";
import { Card, LoadingRow } from "@/components/ui-bits";

/**
 * SidecarCheckinPanel — live "who's PROD vs preview" view across every
 * known sidecar (alpha, camaro, chevelle, redeye).
 *
 * Calls GET /api/admin/runtime/sidecar-checkin (admin JWT).
 *
 * Per-brain row surfaces:
 *   - Verdict chip: prod / preview / policy_drift / invalid / never
 *   - Freshness band: fresh (<5m) / stale (<30m) / dead / never
 *   - Stamp fields the operator wants at a glance: env_name,
 *     mc_url, db_name, git_sha, broker_mode, sidecar_version,
 *     local_execution_authority
 *   - policy_hash match indicator (prevents stale-doctrine sidecars
 *     from silently shipping)
 *
 * Doctrine: this panel is OBSERVABILITY ONLY. It does not gate
 * execution — the broker still verifies MC receipts independently.
 * A sidecar showing 'preview' here is operator-visible drift; the
 * MC-receipt seal is what actually blocks bad orders.
 */

const VERDICT_META = {
  prod:          { color: "#10B981", label: "PROD" },
  preview:       { color: "#F59E0B", label: "PREVIEW" },
  policy_drift:  { color: "#EAB308", label: "POLICY DRIFT" },
  invalid:       { color: "#DC2626", label: "INVALID" },
  never:         { color: "#71717A", label: "NEVER" },
};

const FRESHNESS_META = {
  fresh: { color: "#10B981", label: "fresh" },
  stale: { color: "#F59E0B", label: "stale" },
  dead:  { color: "#DC2626", label: "dead" },
  never: { color: "#71717A", label: "never" },
};

function VerdictChip({ verdict, testid }) {
  const meta = VERDICT_META[verdict] || VERDICT_META.invalid;
  return (
    <span
      data-testid={testid}
      className="inline-block px-2 py-0.5 text-[10px] font-mono uppercase tracking-widest border"
      style={{ color: meta.color, borderColor: meta.color }}
    >
      {meta.label}
    </span>
  );
}

function FreshnessChip({ freshness }) {
  const meta = FRESHNESS_META[freshness] || FRESHNESS_META.never;
  return (
    <span
      className="inline-block px-1.5 py-0.5 text-[9px] font-mono uppercase tracking-widest"
      style={{ color: meta.color }}
    >
      ● {meta.label}
    </span>
  );
}

function StampField({ label, value, testid, mono = true }) {
  return (
    <div className="flex items-baseline gap-2" data-testid={testid}>
      <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim w-24 flex-shrink-0">
        {label}
      </span>
      <span
        className={
          "text-xs text-rd-text break-all " +
          (mono ? "font-mono" : "")
        }
      >
        {value ?? "—"}
      </span>
    </div>
  );
}

function SidecarRow({ row }) {
  const meta = RUNTIME_META[row.runtime] || {};
  const stamp = row.stamp || {};
  const hasStamp = !!row.stamp;

  return (
    <div
      className="px-4 py-3 border-b border-rd-border last:border-b-0"
      data-testid={`sidecar-row-${row.runtime}`}
    >
      <div className="flex flex-wrap items-center gap-3 mb-2">
        <span
          className="font-mono text-sm font-bold uppercase"
          style={{ color: meta.color || "#e7e7e7" }}
        >
          {row.runtime}
        </span>
        <VerdictChip
          verdict={row.verdict}
          testid={`sidecar-verdict-${row.runtime}`}
        />
        <FreshnessChip freshness={row.freshness} />
        {!row.policy_hash_match && hasStamp && (
          <span
            className="text-[10px] font-mono uppercase text-rd-danger border border-rd-danger px-1"
            data-testid={`sidecar-hash-mismatch-${row.runtime}`}
          >
            hash mismatch
          </span>
        )}
        <span className="ml-auto text-[10px] font-mono text-rd-dim">
          {row.checkin_count > 0
            ? `${row.checkin_count} check-in${row.checkin_count === 1 ? "" : "s"} · last ${
                row.last_checkin_at ? relTime(row.last_checkin_at) : "—"
              }`
            : "no check-in recorded"}
        </span>
      </div>

      {!hasStamp && (
        <div className="text-[11px] text-rd-muted leading-relaxed">
          No check-in recorded. The sidecar has not yet POSTed its
          RuntimeStamp to{" "}
          <span className="font-mono text-rd-text">
            /api/admin/runtime/sidecar-checkin/{row.runtime}
          </span>
          . Once the Portable Survival Layer is wired into{" "}
          <span className="font-mono">{row.runtime}</span> and the
          sidecar redeploys, its stamp will appear here.
        </div>
      )}

      {hasStamp && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1 mt-1">
          <StampField
            label="env_name"
            value={stamp.env_name}
            testid={`sidecar-env-${row.runtime}`}
          />
          <StampField
            label="mc_url"
            value={stamp.mc_url}
            testid={`sidecar-mc-url-${row.runtime}`}
          />
          <StampField
            label="db_name"
            value={stamp.db_name}
          />
          <StampField
            label="broker_mode"
            value={stamp.broker_mode}
          />
          <StampField
            label="git_sha"
            value={stamp.git_sha}
          />
          <StampField
            label="version"
            value={stamp.sidecar_version}
          />
          <StampField
            label="platform"
            value={stamp.platform}
          />
          <StampField
            label="exec_authority"
            value={
              stamp.local_execution_authority === false
                ? "false (ok)"
                : `${stamp.local_execution_authority} (FORBIDDEN)`
            }
          />
        </div>
      )}

      {row.errors && row.errors.length > 0 && (
        <div
          className="mt-2 text-[11px] font-mono text-rd-danger"
          data-testid={`sidecar-errors-${row.runtime}`}
        >
          errors: {row.errors.join(" · ")}
        </div>
      )}
    </div>
  );
}

export default function SidecarCheckinPanel() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get("/admin/runtime/sidecar-checkin");
      setData(data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const rows = data?.rows || [];
  const prodCount = rows.filter((r) => r.verdict === "prod").length;
  const previewCount = rows.filter((r) => r.verdict === "preview").length;
  const driftCount = rows.filter((r) => r.verdict === "policy_drift").length;
  const neverCount = rows.filter((r) => r.verdict === "never").length;

  return (
    <Card className="p-0 overflow-hidden" testid="sidecar-checkin-panel">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 border-b border-rd-border bg-rd-bg3">
        <div className="label-eyebrow text-rd-dim">Sidecar identity check-ins</div>
        <span className="text-[10px] font-mono text-rd-dim">
          who's PROD vs preview · companion to the Portable Survival Layer
        </span>
        <div className="ml-auto flex items-center gap-2">
          {data && (
            <span className="text-[10px] font-mono text-rd-dim" data-testid="sidecar-checkin-counts">
              <span style={{ color: VERDICT_META.prod.color }}>{prodCount} prod</span>
              {" · "}
              <span style={{ color: VERDICT_META.preview.color }}>{previewCount} preview</span>
              {" · "}
              <span style={{ color: VERDICT_META.policy_drift.color }}>{driftCount} drift</span>
              {" · "}
              <span style={{ color: VERDICT_META.never.color }}>{neverCount} never</span>
            </span>
          )}
          <button
            type="button"
            onClick={load}
            data-testid="sidecar-checkin-reload"
            disabled={loading}
            className="px-2 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text disabled:opacity-40"
          >
            {loading ? "..." : "reload"}
          </button>
        </div>
      </div>

      {data?.mc_policy_hash && (
        <div className="px-4 py-2 border-b border-rd-border bg-rd-bg2">
          <div className="text-[10px] font-mono text-rd-dim flex flex-wrap items-baseline gap-2">
            <span className="uppercase tracking-widest">MC policy hash</span>
            <span
              className="text-rd-text break-all"
              data-testid="sidecar-checkin-mc-hash"
            >
              {data.mc_policy_hash}
            </span>
            <span className="ml-auto">
              checked {data.checked_at ? fmtTime(data.checked_at) : "—"}
            </span>
          </div>
        </div>
      )}

      {err && (
        <div className="px-4 py-2 text-xs font-mono text-rd-danger border-b border-rd-border">
          {err}
        </div>
      )}

      {!data && !err && <LoadingRow />}

      {data && rows.length > 0 && (
        <div data-testid="sidecar-checkin-rows">
          {rows.map((r) => (
            <SidecarRow key={r.runtime} row={r} />
          ))}
        </div>
      )}

      {data && rows.length === 0 && (
        <div className="px-4 py-6 text-center text-rd-dim font-mono text-xs">
          no sidecars registered
        </div>
      )}
    </Card>
  );
}
