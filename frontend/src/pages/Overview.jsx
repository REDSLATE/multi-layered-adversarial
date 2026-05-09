import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, RUNTIME_META, fmtTime, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, LoadingRow } from "@/components/ui-bits";
import { ArrowUpRight } from "@phosphor-icons/react";

export default function Overview() {
  const [overview, setOverview] = useState(null);
  const [flags, setFlags] = useState(null);
  const [diag, setDiag] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const [o, f, d] = await Promise.all([
          api.get("/shared/overview"),
          api.get("/admin/flags"),
          api.get("/admin/diagnostics"),
        ]);
        setOverview(o.data);
        setFlags(f.data);
        setDiag(d.data);
      } catch (e) {
        setErr(e?.response?.data?.detail || e.message);
      }
    })();
  }, []);

  const ready = overview && flags && diag;

  return (
    <div className="reveal" data-testid="overview-page">
      <PageHeader
        eyebrow="Mission Control · Overview"
        title="Three brains. One nervous system."
        sub="Shared infrastructure connects Alpha, Camaro, and Chevelle. Decision authority remains isolated. All runtimes are in observation mode — receipts are recorded, nothing is executed."
        right={
          <div className="hidden md:flex items-center gap-2" data-testid="overview-mode-pill">
            <Badge color="#FBBF24">{flags?.deploy_mode || "—"}</Badge>
          </div>
        }
        testid="overview-header"
      />

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono" data-testid="overview-error">
          {err}
        </div>
      )}

      {!ready && <LoadingRow testid="overview-loading" />}

      {ready && (
        <>
          {/* Runtime cards */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 md:gap-6 mb-6" data-testid="runtime-cards">
            {overview.runtimes.map((rt) => {
              const meta = RUNTIME_META[rt.runtime];
              const enforce = flags.enforce_flags[meta.enforceFlag];
              return (
                <Card
                  key={rt.runtime}
                  accentColor={meta.color}
                  testid={`runtime-card-${rt.runtime}`}
                  className="hover:bg-[#141414] transition-colors group"
                >
                  <div className="flex items-start justify-between mb-4">
                    <div>
                      <div className="label-eyebrow mb-1">{meta.project}</div>
                      <div
                        className="font-display text-2xl font-black tracking-tighter"
                        style={{ color: meta.color }}
                      >
                        {meta.label}
                      </div>
                      <div className="text-[10px] text-rd-dim uppercase tracking-widest mt-1">
                        {meta.note}
                      </div>
                    </div>
                    <Link
                      to={`/runtime/${rt.runtime}`}
                      className="opacity-60 group-hover:opacity-100 text-rd-muted hover:text-rd-text"
                      data-testid={`runtime-card-link-${rt.runtime}`}
                    >
                      <ArrowUpRight size={18} weight="bold" />
                    </Link>
                  </div>

                  <div className="space-y-1.5">
                    <Row label="MODE" value={
                      <Badge color="#FBBF24">{rt.mode}</Badge>
                    } />
                    <Row label="ENFORCE" value={
                      <Badge color={enforce ? "#10B981" : "#71717A"}>
                        {enforce ? "ENABLED" : "DISABLED"}
                      </Badge>
                    } />
                    <Row label="ARTIFACT" value={
                      <span className="font-mono text-xs">
                        {rt.latest_artifact?.version || "—"}
                      </span>
                    } />
                    <Row label="RECEIPTS" value={
                      <span className="font-mono text-sm" style={{ color: meta.color }}>
                        {rt.receipts_count}
                      </span>
                    } />
                    <Row label="MEMORY LABELS" value={
                      <span className="font-mono text-sm">{rt.memory_labels_count}</span>
                    } />
                    <Row label="LAST SIGNAL" value={
                      <span className="font-mono text-xs text-rd-muted">
                        {rt.last_receipt ? relTime(rt.last_receipt.timestamp) : "—"}
                      </span>
                    } />
                  </div>
                </Card>
              );
            })}
          </div>

          {/* Doctrine + Flags strip */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 md:gap-6">
            <Card className="lg:col-span-2" testid="doctrine-card">
              <div className="label-eyebrow mb-3">Doctrine</div>
              <div className="font-display text-xl font-bold tracking-tight leading-snug mb-4">
                Merge the infrastructure.
                <br />
                <span className="text-rd-warn">Do not merge the brains.</span>
              </div>
              <ul className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1 text-xs font-mono text-rd-muted">
                <li>· Shared MongoDB (namespaced collections)</li>
                <li>· Shared memory labeling firewall</li>
                <li>· Shared ADL receipts</li>
                <li>· Shared calibration tooling</li>
                <li>· Shared diagnostics</li>
                <li>· Shared feature builders</li>
                <li>· Shared admin dashboard</li>
                <li className="text-rd-warn">· Separate model artifacts & calibrators</li>
                <li className="text-rd-warn">· Separate runtime flags & promotion gates</li>
                <li className="text-rd-warn">· Separate execution authority & broker controls</li>
              </ul>
            </Card>

            <Card testid="flags-strip">
              <div className="label-eyebrow mb-3">Runtime flags</div>
              <div className="space-y-2">
                <FlagLine name="BROKER_LIVE_ORDER_ENABLED" on={flags.broker_live_order_enabled} />
                <FlagLine name="PHASE6_ENFORCE_ENABLED" on={flags.enforce_flags.alpha_phase6_enforce_enabled} />
                <FlagLine name="CAMARO_EXECUTOR_ENFORCE_ENABLED" on={flags.enforce_flags.camaro_executor_enforce_enabled} />
                <FlagLine name="CHEVELLE_AUTHORITY_ENABLED" on={flags.enforce_flags.chevelle_authority_enabled} />
              </div>
              <div className="text-[10px] text-rd-dim uppercase tracking-widest mt-4">
                Mongo · {diag.mongo.ok ? "online" : "offline"} · last sync {fmtTime(diag.now)}
              </div>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

function Row({ label, value }) {
  return (
    <div className="flex items-center justify-between py-1 border-b border-rd-border last:border-b-0">
      <span className="text-[10px] uppercase tracking-widest text-rd-dim">{label}</span>
      <span>{value}</span>
    </div>
  );
}

function FlagLine({ name, on }) {
  return (
    <div className="flex items-center justify-between" data-testid={`flag-line-${name}`}>
      <span className="font-mono text-[11px] text-rd-muted truncate pr-2">{name}</span>
      <Badge color={on ? "#10B981" : "#71717A"}>{on ? "TRUE" : "FALSE"}</Badge>
    </div>
  );
}
