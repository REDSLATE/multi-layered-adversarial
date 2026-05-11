import React, { useEffect, useState, useCallback } from "react";
import { api, relTime, fmtTime } from "@/lib/api";
import { Card, Badge, LoadingRow, EmptyState } from "@/components/ui-bits";
import { ChartLine, Pulse, ArrowsClockwise } from "@phosphor-icons/react";

const SOURCE_META = {
  kraken_pro:  { label: "KRAKEN PRO",  color: "#7B5CFF", short: "KRKN" },
  thinkorswim: { label: "THINKORSWIM", color: "#22C55E", short: "TOS"  },
  manual:      { label: "MANUAL",      color: "#A1A1AA", short: "MAN"  },
};

const TF_ORDER = ["1m", "5m", "15m", "1h", "4h", "1d"];

/**
 * Shared Technical Feed — same OHLCV bars + indicator snapshot every brain
 * reads. Mission-page panel (no dedicated route). Click a row to expand
 * the indicator readout. Polls every 20s.
 */
export default function TechnicalsPanel() {
  const [universe, setUniverse] = useState([]);
  const [expanded, setExpanded] = useState(null); // key = `${source}|${symbol}|${tf}`
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    try {
      const { data } = await api.get("/shared/technical/symbols");
      setUniverse(data.items || []);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    const id = setInterval(refresh, 20000);
    return () => clearInterval(id);
  }, [refresh]);

  // Sort: most recently updated first, with stable tf ordering inside same symbol.
  const sorted = [...universe].sort((a, b) => {
    const t = (b.last_bar_ts || "").localeCompare(a.last_bar_ts || "");
    if (t !== 0) return t;
    return TF_ORDER.indexOf(a.tf) - TF_ORDER.indexOf(b.tf);
  });

  const toggle = async (row) => {
    const key = `${row.source}|${row.symbol}|${row.tf}`;
    if (expanded === key) {
      setExpanded(null);
      setDetail(null);
      return;
    }
    setExpanded(key);
    setDetail(null);
    try {
      const { data } = await api.get(
        `/shared/technical/${encodeURIComponent(row.symbol)}`,
        { params: { tf: row.tf, source: row.source, bars: 50 } },
      );
      setDetail(data);
    } catch (e) {
      setDetail({ _err: e?.response?.data?.detail || e.message });
    }
  };

  return (
    <Card className="p-0 overflow-hidden" testid="technicals-panel">
      <div className="px-4 py-3 border-b border-rd-border flex items-baseline justify-between">
        <div className="flex items-baseline gap-3">
          <ChartLine size={14} weight="bold" className="text-rd-text" />
          <span className="label-eyebrow">Shared technical feed</span>
          <Badge color="#A1A1AA">PULL-ONLY</Badge>
          <Badge color="#A1A1AA">{universe.length} streams</Badge>
        </div>
        <div className="text-[10px] text-rd-dim uppercase tracking-widest">
          OHLCV → indicators → Alpha · Camaro · Chevelle · REDEYE
        </div>
      </div>

      {err && (
        <div className="border-b border-rd-danger text-rd-danger px-3 py-2 text-xs font-mono">
          {err}
        </div>
      )}

      {loading && <LoadingRow testid="technicals-loading" />}

      {!loading && sorted.length === 0 && (
        <div className="px-4 py-8">
          <EmptyState
            message="No bars ingested yet. Wire your Kraken Pro / ThinkOrSwim feeder to POST /api/ingest/ohlcv with X-Feeder-Token. See /app/runtime_patch_kit/technicals/."
          />
        </div>
      )}

      {!loading && sorted.length > 0 && (
        <div className="divide-y divide-rd-border" data-testid="technicals-rows">
          {sorted.map((row) => {
            const key = `${row.source}|${row.symbol}|${row.tf}`;
            const meta = SOURCE_META[row.source] || SOURCE_META.manual;
            const isOpen = expanded === key;
            return (
              <div key={key}>
                <button
                  type="button"
                  onClick={() => toggle(row)}
                  className="w-full px-4 py-2.5 flex items-center gap-3 hover:bg-rd-bg2 text-left"
                  data-testid={`tech-row-${row.source}-${row.symbol}-${row.tf}`}
                >
                  <Badge color={meta.color}>{meta.short}</Badge>
                  <span className="font-mono text-sm text-rd-text w-32 truncate">
                    {row.symbol}
                  </span>
                  <span className="font-mono text-[11px] text-rd-muted w-12">
                    {row.tf}
                  </span>
                  <span className="text-[11px] text-rd-dim flex-1">
                    {row.bars} bars · last {row.last_bar_ts ? relTime(row.last_bar_ts) : "—"}
                  </span>
                  <span className="text-[10px] text-rd-dim uppercase tracking-widest">
                    {isOpen ? "▾ snapshot" : "▸ expand"}
                  </span>
                </button>

                {isOpen && (
                  <div className="px-4 py-3 bg-rd-bg2 border-t border-rd-border">
                    {!detail && <LoadingRow />}
                    {detail?._err && (
                      <div className="text-rd-danger text-xs font-mono">{detail._err}</div>
                    )}
                    {detail && !detail._err && (
                      <SnapshotReadout detail={detail} accent={meta.color} />
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className="px-4 py-2.5 bg-rd-bg2 border-t border-rd-border text-[10px] text-rd-dim uppercase tracking-widest leading-relaxed flex items-center gap-2">
        <Pulse size={10} weight="bold" />
        Shared evidence · same bars, four brains, four interpretations · no execution authority
        <ArrowsClockwise size={10} weight="bold" className="ml-auto" />
        polled 20s
      </div>
    </Card>
  );
}

function SnapshotReadout({ detail, accent }) {
  const ind = detail?.snapshot?.indicators || {};
  const ready = ind.ready;
  if (!ready) {
    return (
      <div className="text-[11px] font-mono text-rd-dim">
        Snapshot warming up — {ind.bars_seen || 0} bars seen.
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-[11px] font-mono" data-testid="tech-snapshot">
      <Cell label="Close" value={fmtNum(ind.last_close)} accent={accent} />
      <Cell label="RSI(14)" value={fmtNum(ind.rsi14, 1)} hint={rsiHint(ind.rsi14)} />
      <Cell
        label="MACD hist"
        value={fmtSigned(ind?.macd?.hist)}
        hint={macdHint(ind?.macd)}
      />
      <Cell
        label="BB position"
        value={ind?.bbands?.position != null ? `${(ind.bbands.position * 100).toFixed(0)}%` : "—"}
        hint={bbHint(ind?.bbands?.position)}
      />
      <Cell label="SMA 20"  value={fmtNum(ind?.sma?.["20"])} />
      <Cell label="SMA 50"  value={fmtNum(ind?.sma?.["50"])} />
      <Cell label="SMA 200" value={fmtNum(ind?.sma?.["200"])} />
      <Cell
        label="ATR%"
        value={ind?.atr14_pct != null ? `${ind.atr14_pct.toFixed(2)}%` : "—"}
      />
      <div className="col-span-2 md:col-span-4 text-[10px] text-rd-dim uppercase tracking-widest pt-1 border-t border-rd-border">
        snapshot · {detail.snapshot?.computed_at ? fmtTime(detail.snapshot.computed_at) : "—"}
        {" · "}via {detail.source}
        {" · "}{ind.bars_seen} bars in window
      </div>
    </div>
  );
}

function Cell({ label, value, accent, hint }) {
  return (
    <div className="border border-rd-border bg-rd-bg3 px-2 py-1.5">
      <div className="text-[10px] text-rd-dim uppercase tracking-widest">{label}</div>
      <div className="text-sm" style={accent ? { color: accent } : undefined}>
        {value}
      </div>
      {hint && <div className="text-[10px] text-rd-muted mt-0.5">{hint}</div>}
    </div>
  );
}

function fmtNum(v, dp = 2) {
  if (v == null || isNaN(v)) return "—";
  const n = Number(v);
  if (Math.abs(n) >= 10000) return n.toFixed(0);
  if (Math.abs(n) >= 100) return n.toFixed(1);
  return n.toFixed(dp);
}

function fmtSigned(v) {
  if (v == null || isNaN(v)) return "—";
  const n = Number(v);
  const s = n >= 0 ? "+" : "";
  return `${s}${fmtNum(n, 4)}`;
}

function rsiHint(v) {
  if (v == null) return null;
  if (v >= 70) return "overbought";
  if (v <= 30) return "oversold";
  return "neutral";
}

function macdHint(m) {
  if (!m || m.hist == null || m.macd == null || m.signal == null) return null;
  if (m.hist > 0 && m.macd > 0) return "bull · above zero";
  if (m.hist < 0 && m.macd < 0) return "bear · below zero";
  if (m.hist > 0) return "bull crossover";
  return "bear crossover";
}

function bbHint(p) {
  if (p == null) return null;
  if (p >= 1) return "above upper";
  if (p <= 0) return "below lower";
  if (p >= 0.8) return "upper band";
  if (p <= 0.2) return "lower band";
  return "mid range";
}
