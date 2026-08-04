import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Gauge } from "@phosphor-icons/react";

/** Momentum Scanner tile — 5th signal source status + arm/disarm.
 *  Entries ride the full gate chain; exits +tp/−sl from broker basis. */
export const MomentumScannerTile = () => {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = () =>
    api.get("/admin/momentum-scanner").then(({ data: d }) => setData(d)).catch(() => {});

  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, []);

  const toggle = async () => {
    if (!data || busy) return;
    setBusy(true);
    try {
      await api.post("/admin/momentum-scanner", { enabled: !data.config.enabled });
      await load();
    } finally {
      setBusy(false);
    }
  };

  const cfg = data?.config, st = data?.state;
  const rejections = Object.entries(st?.rejections || {}).sort((a, b) => b[1] - a[1]).slice(0, 4);
  const candidates = (st?.candidates || []).slice(0, 5);
  return (
    <div className="border border-rd-border bg-rd-panel p-3" data-testid="momentum-scanner-tile">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <Gauge size={14} weight="bold" className="text-sky-400" />
          <span className="text-xs font-mono font-bold uppercase tracking-widest text-rd-text">
            Momentum Scanner
          </span>
        </div>
        <button
          onClick={toggle}
          disabled={!cfg || busy}
          className={`px-2 py-0.5 text-[10px] font-mono uppercase border transition-colors ${
            cfg?.enabled
              ? "border-rd-success text-rd-success"
              : "border-rd-border text-rd-dim hover:text-rd-muted"
          }`}
          data-testid="momentum-scanner-toggle"
        >
          {cfg ? (cfg.enabled ? "armed" : "disarmed — arm") : "…"}
        </button>
      </div>
      {!data ? (
        <div className="text-[10px] font-mono text-rd-dim">loading…</div>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2 mb-2">
            <div className="border border-rd-border px-3 py-1.5" data-testid="ms-evaluated">
              <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">evaluated</div>
              <div className="font-display text-lg font-bold text-rd-text">{st?.evaluated ?? 0}</div>
            </div>
            <div className="border border-rd-border px-3 py-1.5" data-testid="ms-emitted">
              <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">emitted (cycle)</div>
              <div className="font-display text-lg font-bold text-rd-success">{st?.emitted ?? 0}</div>
            </div>
            <div className="border border-rd-border px-3 py-1.5" data-testid="ms-total">
              <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">total intents</div>
              <div className="font-display text-lg font-bold text-rd-text">{data.total_momentum_intents ?? 0}</div>
            </div>
          </div>
          {candidates.length > 0 && (
            <div className="mb-2 space-y-0.5" data-testid="ms-candidates">
              {candidates.map((c) => (
                <div key={c.symbol} className="flex gap-2 text-[10px] font-mono items-center">
                  <span className="w-24 shrink-0 text-rd-text">{c.symbol}</span>
                  <span className="w-10 shrink-0 text-rd-dim uppercase">{c.lane || ""}</span>
                  {c.origin === "ignition" && (
                    <span
                      className="px-1 border border-amber-500 text-amber-500 text-[8px] font-bold uppercase tracking-wider"
                      data-testid={`ms-ignition-badge-${c.symbol.replace("/", "-")}`}
                    >
                      ign
                    </span>
                  )}
                  <span className={c.allowed ? "text-rd-success" : "text-rd-dim"}>
                    {c.allowed ? "ENTRY" : c.reason}
                  </span>
                  <span className="ml-auto text-rd-muted">
                    {c.prev_score}→{c.score}
                  </span>
                </div>
              ))}
            </div>
          )}
          {(st?.ignition || []).length > 0 && (
            <div className="text-[9px] font-mono text-amber-500 mb-1" data-testid="ms-ignition-sweep">
              ignition sweep:{" "}
              {(st.ignition || [])
                .map((i) => `${i.symbol} $${Math.round((i.vol_rate_usd_min || 0) / 1000)}k/min +${i.price_change_pct}%`)
                .join(" · ")}
            </div>
          )}
          {rejections.length > 0 && (
            <div className="text-[9px] font-mono text-rd-dim mb-1" data-testid="ms-rejections">
              blocks: {rejections.map(([k, v]) => `${k}:${v}`).join(" · ")}
            </div>
          )}
          <div className="text-[9px] font-mono text-rd-dim" data-testid="ms-footer">
            last scan {st?.last_run ? st.last_run.slice(11, 19) + "Z" : "never"} · every{" "}
            {cfg?.interval_sec}s · lanes {(cfg?.lanes || []).join("+")} · exits +{cfg?.tp_pct}% / −{cfg?.sl_pct}% from broker basis ·
            entries pass all gates · ignition {cfg?.ignition_enabled ? "ON" : "off"}
          </div>
        </>
      )}
    </div>
  );
};

export default MomentumScannerTile;
