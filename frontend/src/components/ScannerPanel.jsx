import React, { useCallback, useEffect, useState } from "react";
import { api, relTime } from "@/lib/api";
import { Crosshair } from "@phosphor-icons/react";

const aff = (v) => (v >= 0.65 ? "text-emerald-500" : v >= 0.4 ? "text-amber-500" : "text-rd-dim");

export default function ScannerPanel() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data: d } = await api.get("/admin/scanner");
      setData(d);
    } catch (e) {
      setMsg(e?.response?.data?.detail || String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 45000);
    return () => clearInterval(t);
  }, [load]);

  const scanNow = async () => {
    setBusy(true); setMsg(null);
    try {
      const { data: r } = await api.post("/admin/scanner/scan-now");
      setMsg(`scanned ${r.result.chunk_scanned ?? 0} · admitted ${r.result.admitted ?? 0} · pool ${r.result.pool_live ?? 0}`);
      await load();
    } catch (e) {
      setMsg(e?.response?.data?.detail || String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!data) return null;
  const s = data.status || {};
  const cands = data.candidates || [];
  const rejects = s.last_scan?.rejects || {};

  return (
    <div className="border border-rd-border p-3 mb-5" data-testid="scanner-panel">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono flex items-center gap-1">
          <Crosshair size={11} />
          RTH Opportunity Scanner · discovery universe {data.universe_size} · advisory only
        </div>
        <div className="flex items-center gap-2 text-[9px] font-mono">
          <span className={s.rth_now ? "text-emerald-500" : "text-rd-dim"} data-testid="scanner-rth-state">
            {s.rth_now ? "RTH OPEN" : "RTH CLOSED"}
          </span>
          <span className={s.running ? "text-emerald-500" : "text-rd-danger"} data-testid="scanner-running">
            {s.running ? `loop ${Math.round(s.interval_sec)}s` : "LOOP OFF"}
          </span>
          <button onClick={scanNow} disabled={busy} data-testid="scanner-scan-now"
            className="uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-0.5 disabled:opacity-40">
            scan now
          </button>
        </div>
      </div>

      <div className="text-[10px] font-mono text-rd-dim mb-2" data-testid="scanner-stats">
        pool {s.store?.live ?? 0} live · last scan {s.last_scan_at ? relTime(s.last_scan_at) : "—"} ·
        chunk {s.last_scan?.chunk_scanned ?? "—"}/{data.universe_size} · admitted {s.last_scan?.admitted ?? "—"}
        {Object.keys(rejects).length > 0 && (
          <span className="text-rd-muted"> · rejects: {Object.entries(rejects).map(([k, v]) => `${k}:${v}`).join(" ")}</span>
        )}
      </div>

      {cands.length > 0 ? (
        <div data-testid="scanner-candidates">
          <div className="grid grid-cols-[64px_46px_86px_60px_60px_1fr] gap-1 text-[9px] uppercase tracking-widest text-rd-dim font-mono pb-0.5">
            <span>symbol</span><span>score</span><span>class</span><span>mom%</span><span>age</span><span>brain affinity</span>
          </div>
          {cands.slice(0, 15).map((c) => (
            <div key={c.symbol} className="grid grid-cols-[64px_46px_86px_60px_60px_1fr] gap-1 text-[10px] font-mono py-0.5" data-testid={`scanner-cand-${c.symbol}`}>
              <span className="font-bold text-rd-text">{c.symbol}</span>
              <span className="text-emerald-500 font-bold">{c.opportunity_score?.toFixed(2)}</span>
              <span className="text-rd-dim">{c.classification}</span>
              <span className={c.momentum_pct >= 0 ? "text-emerald-500" : "text-rd-danger"}>
                {c.momentum_pct > 0 ? "+" : ""}{c.momentum_pct?.toFixed(2)}
              </span>
              <span className="text-rd-dim">{c.bar_age_min}m</span>
              <span className="flex gap-2">
                {Object.entries(c.brain_affinity || {}).map(([b, v]) => (
                  <span key={b} className={aff(v)}>{b.slice(0, 4)} {v.toFixed(2)}</span>
                ))}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-[10px] font-mono text-rd-muted" data-testid="scanner-empty">
          No live candidates — pool fills during RTH (or hit SCAN NOW to force a cycle).
        </div>
      )}

      {msg && <div className="text-[10px] font-mono text-amber-500 mt-1" data-testid="scanner-msg">{msg}</div>}

      <div className="text-[10px] text-rd-muted mt-2 font-mono leading-relaxed">
        Scanner nominates; brains decide; Seat executes; Risk sizes. Top discovery names merge into the
        live universe each cycle alongside pins + core list. Candidates expire after {s.candidate_ttl_min}m.
      </div>
    </div>
  );
}
