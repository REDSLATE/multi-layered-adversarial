import React, { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { api, RUNTIME_META, fmtTime, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";
import SovereignTile from "@/components/SovereignTile";
import LivePulse from "@/components/LivePulse";
import BrainProxiedStatusTile from "@/components/BrainProxiedStatusTile";

const SUB_ENDPOINT = {
  alpha: { url: "/runtime/alpha/decisions", title: "alpha_decision_log", cols: ["timestamp", "decision", "symbol", "score"] },
  camaro: { url: "/runtime/camaro/shadow-rows", title: "camaro_shadow_rows", cols: ["timestamp", "shadow", "symbol", "side", "size"] },
  chevelle: { url: "/runtime/chevelle/memory-labels", title: "chevelle_memory_labels", cols: ["timestamp", "authority_call", "symbol", "horizon"] },
};

export default function RuntimeDetail() {
  const { runtime } = useParams();
  const meta = RUNTIME_META[runtime];
  const sub = SUB_ENDPOINT[runtime];
  const [status, setStatus] = useState(null);
  const [rows, setRows] = useState(null);
  const [calibs, setCalibs] = useState(null);
  const [artifacts, setArtifacts] = useState(null);
  // Pass #19 (2026-05-28) — seat-as-authority doctrine. The page
  // header badge shows the BRAIN'S CURRENT SEAT (or VACANT) instead
  // of just repeating the brain's identity. The seat IS the
  // restriction; the brain itself carries no separate authority.
  const [roster, setRoster] = useState(null);
  // MC-side proxy of the brain's own `/status` payload (when the
  // operator has wired `<BRAIN>_STATUS_URL` in MC's env). Surfaces
  // brain-internal telemetry — checkin, heartbeat, governor emitter,
  // data-keys, neuro engine, intents — inside the MC dashboard
  // without cross-origin pain. Wrapper shape: `{brain, ok, payload?,
  // error?, _proxy_duration_ms?, _proxied_from?}`.
  const [proxied, setProxied] = useState(null);
  // `loaded` flips true after the parallel fetches complete (success
  // or failure). Lets us distinguish "still loading" (show spinner)
  // from "fetched but no status endpoint" (show graceful unavailable
  // message instead of an eternal LoadingRow).
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!meta) return;
    setStatus(null);
    setRows(null);
    setProxied(null);
    setLoaded(false);
    (async () => {
      // `sub` is undefined for brains without a curated sub-endpoint
      // (e.g. redeye, whose decision log lives elsewhere). Skip the
      // sub-rows fetch in that case so the page still renders the
      // status / calibrators / artifacts panels instead of crashing
      // on `sub.url`. Each fetch is independently catch-shielded so
      // a single 404 (e.g. `/runtime/redeye/status` not present on
      // the backend) cannot tank the whole page.
      //
      // Added 2026-02-17: ALSO fetch MC's status PROXY for this brain
      // (`/api/admin/runtime/{brain}/status`). The proxy returns
      // either `{ok: true, payload: <brain's full /status payload>}`
      // OR `{ok: false, error}`. We render the payload as a composite
      // tile below the standard status card when present.
      const subPromise = sub
        ? api.get(sub.url).catch(() => ({ data: null }))
        : Promise.resolve({ data: null });
      const [s, r, c, a, rs, prx] = await Promise.all([
        api.get(`/runtime/${runtime}/status`).catch(() => ({ data: null })),
        subPromise,
        api.get(`/shared/calibrators?runtime=${runtime}`).catch(() => ({ data: null })),
        api.get(`/shared/artifacts?runtime=${runtime}`).catch(() => ({ data: null })),
        api.get("/admin/roster").catch(() => ({ data: null })),
        api.get(`/admin/runtime/${runtime}/status`).catch(() => ({ data: null })),
      ]);
      setStatus(s.data);
      setRows(r.data);
      setCalibs(c.data);
      setArtifacts(a.data);
      setRoster(rs.data);
      setProxied(prx.data);
      setLoaded(true);
    })();
  }, [runtime, meta, sub]);

  // Compute the brain's current seat(s) from the roster.
  const seatLabel = React.useMemo(() => {
    const assignments = roster?.assignments || {};
    const seatsHeld = Object.entries(assignments)
      .filter(([_, b]) => b === runtime)
      .map(([seat, _]) => seat);
    if (seatsHeld.length === 0) return "VACANT";
    const friendly = {
      strategist: "STRATEGIST",
      executor: "EXECUTOR",
      governor: "GOVERNOR",
      auditor: "AUDITOR",
      crypto: "CRYPTO EXECUTOR",
      crypto_strategist: "CRYPTO STRATEGIST",
      crypto_governor: "CRYPTO GOVERNOR",
      crypto_auditor: "CRYPTO AUDITOR",
    };
    const preferenceOrder = [
      "strategist", "executor", "governor", "auditor",
      "crypto_strategist", "crypto", "crypto_governor", "crypto_auditor",
    ];
    seatsHeld.sort(
      (a, b) => preferenceOrder.indexOf(a) - preferenceOrder.indexOf(b),
    );
    return seatsHeld.map((s) => friendly[s] || s.toUpperCase()).join(" · ");
  }, [roster, runtime]);

  const hasSeat = seatLabel !== "VACANT";

  if (!meta) {
    return (
      <div className="p-10 text-center text-rd-danger" data-testid="runtime-unknown">
        Unknown runtime: {runtime}.{" "}
        <Link to="/admin" className="underline">
          Back to overview
        </Link>
      </div>
    );
  }

  return (
    <div className="reveal" data-testid={`runtime-page-${runtime}`}>
      <PageHeader
        eyebrow={`Runtime · ${meta.project}`}
        title={meta.label}
        sub={`${meta.note}. Decision authority is isolated to this runtime — no cross-runtime reads.`}
        right={
          <div className="flex items-center gap-3">
            <LivePulse runtime={runtime} />
            <Badge
              color={hasSeat ? meta.color : "#6B7280"}
              data-testid="runtime-header-seat-badge"
            >
              {seatLabel}
            </Badge>
          </div>
        }
        testid={`runtime-header-${runtime}`}
      />

      {!loaded && !status && <LoadingRow />}

      {/* MC-proxied brain `/status` payload — composite view of the
          brain's own telemetry endpoint (when wired via the
          `<BRAIN>_STATUS_URL` env var on MC). Renders ABOVE the
          MC-side status card so the operator sees brain-internal
          state first when troubleshooting. Read-only; pings MC's
          proxy which writes one audit row per call. */}
      {loaded && proxied && (
        <BrainProxiedStatusTile brain={runtime} proxied={proxied} />
      )}

      {loaded && !status && (
        <Card testid={`runtime-status-unavailable-${runtime}`}>
          <div className="label-eyebrow mb-2">Status</div>
          <div className="text-sm font-mono text-rd-muted">
            No per-runtime status endpoint is wired for{" "}
            <span className="text-rd-text">{meta.label}</span>. This brain
            reports its telemetry through the shared sovereign-audit /
            opinion / sidecar-check-in surfaces — see the Diagnostics
            page for live signals.
          </div>
        </Card>
      )}

      {status && (
        <>
          {/* Status strip */}
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4 md:gap-6 mb-6" data-testid={`runtime-status-${runtime}`}>
            <Card accentColor={meta.color}>
              <div className="label-eyebrow mb-2">Mode</div>
              <Badge color="#FBBF24">{status.mode}</Badge>
            </Card>
            <Card accentColor={meta.color}>
              <div className="label-eyebrow mb-2">Enforce flag</div>
              <div className="font-mono text-[11px] text-rd-text mb-1">{meta.enforceLabel}</div>
              <Badge color={
                status.phase6_enforce_enabled ?? status.executor_enforce_enabled ?? status.authority_enabled
                  ? "#10B981" : "#71717A"
              }>
                {(status.phase6_enforce_enabled ?? status.executor_enforce_enabled ?? status.authority_enabled)
                  ? "ENABLED" : "DISABLED"}
              </Badge>
            </Card>
            <Card accentColor={meta.color}>
              <div className="label-eyebrow mb-2">Records</div>
              <div className="font-display text-2xl font-bold tracking-tight" style={{ color: meta.color }}>
                {status.decision_log_count ?? status.shadow_rows_count ?? status.memory_labels_count ?? 0}
              </div>
              <div className="text-[10px] text-rd-dim font-mono mt-1">{sub?.title || "—"}</div>
            </Card>
            <Card accentColor={meta.color}>
              <div className="label-eyebrow mb-2">Doctrine</div>
              <div className="text-[11px] text-rd-muted leading-relaxed font-mono">
                {status.doctrine}
              </div>
            </Card>
          </div>

          {/* Sovereign state — periodic snapshot from the brain's deterministic core */}
          <div className="mb-6">
            <SovereignTile runtime={runtime} accent={meta.color} />
          </div>

          {/* Decision log — only rendered when this runtime has a
              curated sub-endpoint. Brains without one (e.g. redeye)
              show their telemetry elsewhere; skipping the card keeps
              the page from crashing on `sub.title`. */}
          {sub && (
            <Card className="p-0 overflow-hidden mb-6" testid={`runtime-rows-${runtime}`}>
              <div className="px-4 py-3 border-b border-rd-border flex items-center justify-between">
                <div>
                  <div className="label-eyebrow">Isolated decision store</div>
                  <div className="font-mono text-sm">{sub.title}</div>
                </div>
                <div className="text-[10px] text-rd-dim uppercase tracking-widest">
                  {rows?.count || 0} records
                </div>
              </div>
              {!rows && <LoadingRow />}
              {rows && rows.items.length === 0 && <EmptyState message="No records in this runtime's log." />}
              {rows && rows.items.length > 0 && (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs font-mono">
                    <thead>
                      <tr className="bg-rd-bg3 text-rd-dim uppercase tracking-widest">
                        {sub.cols.map((c) => (
                          <th key={c} className="text-left px-4 py-3 border-b border-rd-border">{c}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.items.map((row, i) => (
                        <tr key={row.id || i} className="border-b border-rd-border last:border-b-0 hover:bg-rd-bg3">
                          {sub.cols.map((c) => (
                            <td key={c} className="px-4 py-2.5">
                              {c === "timestamp"
                                ? `${fmtTime(row[c])} (${relTime(row[c])})`
                                : row[c] != null
                                ? String(row[c])
                                : "—"}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          )}

          {/* Calibrators + Artifacts side-by-side */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 md:gap-6">
            <Card testid={`runtime-calibrators-${runtime}`}>
              <div className="label-eyebrow mb-3">Calibrators (this runtime only)</div>
              <div className="space-y-2">
                {(calibs?.items || []).map((c) => (
                  <div key={c.name} className="flex items-center justify-between py-1 border-b border-rd-border last:border-b-0">
                    <span className="font-mono text-xs">{c.name}</span>
                    <span className="font-mono text-[10px] text-rd-muted">{c.method} · {c.version}</span>
                  </div>
                ))}
              </div>
            </Card>
            <Card testid={`runtime-artifacts-${runtime}`}>
              <div className="label-eyebrow mb-3">Artifacts (this runtime only)</div>
              <div className="space-y-2">
                {(artifacts?.items || []).map((a) => (
                  <div key={a.artifact} className="flex items-center justify-between py-1 border-b border-rd-border last:border-b-0">
                    <span className="font-mono text-xs">{a.artifact}</span>
                    <span className="font-mono text-[10px]" style={{ color: meta.color }}>
                      {a.version} · {a.sha}
                    </span>
                  </div>
                ))}
              </div>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
