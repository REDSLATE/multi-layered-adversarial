import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { WaveSquare, Warning } from "@phosphor-icons/react";

const FIELDS = [
  ["min_bars", "min bars", "tape-knob-min-bars", 1],
  ["min_completeness", "min completeness", "tape-knob-completeness", 0.01],
  ["max_gap_bars", "max gap (bars)", "tape-knob-max-gap", 1],
  ["max_staleness_tf_mult", "staleness ×tf", "tape-knob-staleness", 0.5],
];

/** Tape Quality knobs — thresholds for the stale/gappy-bar gate that
 *  protects the momentum scanner, entry gate, and re-arm watcher. */
export const TapeQualityPanel = () => {
  const [cfg, setCfg] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  useEffect(() => {
    api.get("/admin/tape-quality").then(({ data }) => setCfg(data.config)).catch(() => {});
  }, []);

  const save = async (extra = {}) => {
    if (!cfg) return;
    setBusy(true); setMsg(null);
    try {
      const { data } = await api.post("/admin/tape-quality", {
        enabled: cfg.enabled,
        min_bars: Number(cfg.min_bars),
        min_completeness: Number(cfg.min_completeness),
        max_gap_bars: Number(cfg.max_gap_bars),
        max_staleness_tf_mult: Number(cfg.max_staleness_tf_mult),
        ...extra,
      });
      setCfg(data.config);
      setMsg({ ok: true, text: "tape quality knobs saved" });
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally { setBusy(false); }
  };

  return (
    <div className="border border-rd-border bg-rd-panel p-3 mb-4" data-testid="tape-quality-panel">
      <div className="flex items-center justify-between mb-1.5">
        <div className="flex items-center gap-2">
          <WaveSquare size={14} weight="bold" className="text-sky-400" />
          <span className="text-xs font-mono font-bold uppercase tracking-widest text-rd-text">
            Tape Quality Gate
          </span>
          <span
            className={`px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border ${
              cfg?.enabled ? "border-rd-success text-rd-success" : "border-amber-500 text-amber-500"
            }`}
            data-testid="tape-quality-status"
          >
            {cfg ? (cfg.enabled ? "ENFORCING" : "DISABLED") : "…"}
          </span>
        </div>
        <button
          onClick={() => save({ enabled: !cfg?.enabled })}
          disabled={busy || !cfg}
          className="px-2 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-muted hover:text-rd-text hover:border-rd-text transition-colors"
          data-testid="tape-quality-toggle"
        >
          {cfg?.enabled ? "disable" : "enable"}
        </button>
      </div>
      <div className="text-[10px] font-mono text-rd-dim mb-2">
        Stale or gappy bars are rejected before they distort momentum scores — protects the scanner, entry gate, and re-arm watcher. Timeframe auto-inferred.
      </div>
      {cfg && (
        <div className="flex flex-wrap items-end gap-3">
          {FIELDS.map(([key, label, tid, step]) => (
            <label key={key} className="flex flex-col gap-0.5">
              <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">{label}</span>
              <input
                type="number"
                step={step}
                value={cfg[key] ?? ""}
                onChange={(e) => setCfg({ ...cfg, [key]: e.target.value })}
                className="w-24 bg-rd-bg border border-rd-border px-2 py-1 text-xs font-mono text-rd-text focus:outline-none focus:border-rd-text"
                data-testid={tid}
              />
            </label>
          ))}
          <button
            onClick={() => save()}
            disabled={busy}
            className="px-3 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-success text-rd-success hover:bg-rd-success/10 transition-colors disabled:opacity-40"
            data-testid="tape-quality-save"
          >
            {busy ? "saving…" : "save"}
          </button>
        </div>
      )}
      {msg && (
        <div
          className={`mt-1.5 text-[10px] font-mono flex items-center gap-1 ${msg.ok ? "text-rd-success" : "text-red-500"}`}
          data-testid="tape-quality-msg"
        >
          {!msg.ok && <Warning size={10} weight="bold" />} {msg.text}
        </div>
      )}
    </div>
  );
};

export default TapeQualityPanel;
