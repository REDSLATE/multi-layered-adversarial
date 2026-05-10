import React, { useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";
import { Trophy, Lock, Target } from "@phosphor-icons/react";

const BRAIN_META = {
  alpha:    { label: "ALPHA",    color: "#3B82F6" },
  camaro:   { label: "CAMARO",   color: "#F59E0B" },
  chevelle: { label: "CHEVELLE", color: "#10B981" },
  redeye:   { label: "REDEYE",   color: "#DC2626" },
};

const LENS_LABEL = {
  longs: "Longs",
  shorts: "Shorts",
  judgement_calls: "Judgement",
  source_reliability: "Source Reliability",
};

export default function Scorecards() {
  const [data, setData] = useState({});
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    setErr("");
    try {
      const results = await Promise.all(
        Object.keys(BRAIN_META).map(async (rt) => {
          const { data } = await api.get(`/shared/scorecard?runtime=${rt}`);
          return [rt, data];
        })
      );
      setData(Object.fromEntries(results));
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    const id = setInterval(refresh, 30000);
    return () => clearInterval(id);
  }, [refresh]);

  return (
    <div className="reveal" data-testid="scorecards-page">
      <PageHeader
        eyebrow="Step 2 · role scoring"
        title="Scorecards"
        sub="Each brain graded only on its own job. Descriptive, not prescriptive — scorecards never gate promotions, and no brain may rewrite another brain's authority based on a scorecard."
        right={
          <div className="flex items-center gap-2">
            <Badge color="#FBBF24">
              <Lock size={11} weight="bold" className="inline mr-1 -mt-0.5" />
              ADMIN ONLY
            </Badge>
            <Badge color="#10B981">DESCRIPTIVE</Badge>
          </div>
        }
        testid="scorecards-header"
      />

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono">
          {err}
        </div>
      )}

      {loading && <LoadingRow />}

      {!loading && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4" data-testid="scorecards-grid">
          {Object.entries(BRAIN_META).map(([rt]) => (
            <ScorecardPanel key={rt} runtime={rt} card={data[rt]} />
          ))}
        </div>
      )}
    </div>
  );
}

function ScorecardPanel({ runtime, card }) {
  const meta = BRAIN_META[runtime];
  if (!card) return null;
  const summary = card.summary || {};
  const lens = card.lens;
  const empty = (summary.total_resolved || 0) === 0;

  return (
    <Card className="p-0 overflow-hidden" testid={`scorecard-${runtime}`}>
      <div
        className="px-4 py-3 border-b border-rd-border flex items-baseline justify-between"
        style={{ borderTop: `2px solid ${meta.color}` }}
      >
        <div className="flex items-baseline gap-3">
          <Trophy size={14} weight="bold" style={{ color: meta.color }} />
          <span className="font-mono font-bold text-sm" style={{ color: meta.color }}>
            {meta.label}
          </span>
          <Badge color="#A1A1AA">{LENS_LABEL[lens] || lens}</Badge>
        </div>
        <div className="text-[10px] text-rd-dim uppercase tracking-widest">
          stances: {(card.role_stances || []).join(" · ")}
        </div>
      </div>

      <div className="px-4 py-3 border-b border-rd-border">
        <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-1">
          Question answered
        </div>
        <div className="text-xs font-mono text-rd-text">
          {card.question_answered || "—"}
        </div>
      </div>

      {empty ? (
        <div className="px-4 py-8">
          <EmptyState
            message="No resolved opinions yet for this brain. Resolve some via /api/admin/outcome to populate."
          />
        </div>
      ) : (
        <>
          {/* Summary stats */}
          <div className="px-4 py-3 border-b border-rd-border grid grid-cols-2 sm:grid-cols-4 gap-3">
            <Stat label="Hit rate" value={fmtPct(summary.hit_rate)} accent={meta.color} />
            <Stat label="Decisive" value={`${summary.wins ?? 0}/${summary.decisive ?? 0}`} />
            <Stat label="Brier" value={card.brier == null ? "—" : Number(card.brier).toFixed(4)} />
            <Stat label="Resolved total" value={summary.total_resolved ?? 0} />
          </div>

          {/* Calibration bands */}
          {(card.calibration_bands || []).length > 0 && (
            <div className="px-4 py-3 border-b border-rd-border">
              <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-2">
                Calibration · confidence band → actual win rate
              </div>
              <div className="space-y-1">
                {card.calibration_bands.map((b) => (
                  <div key={b.confidence_band} className="flex items-center gap-3 text-[11px] font-mono">
                    <span className="text-rd-muted w-16">{b.confidence_band}</span>
                    <div className="flex-1 bg-rd-bg2 h-3 relative overflow-hidden border border-rd-border">
                      <div
                        className="absolute inset-y-0 left-0"
                        style={{ width: `${b.win_rate * 100}%`, background: meta.color }}
                      />
                    </div>
                    <span className="text-rd-text w-12 text-right">{fmtPct(b.win_rate)}</span>
                    <span className="text-rd-dim text-[10px]">n={b.n}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Role-specific extra panel */}
          {runtime === "redeye" && card.alpha_alignment_breakdown && (
            <div className="px-4 py-3 border-b border-rd-border">
              <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-2">
                alpha_alignment breakdown · contradicts vs divergent vs aligned
              </div>
              <div className="border border-rd-border">
                <table className="w-full text-[11px] font-mono">
                  <thead>
                    <tr className="bg-rd-bg3 text-rd-dim uppercase tracking-widest">
                      <th className="text-left px-2 py-1.5">Hint</th>
                      <th className="text-right px-2 py-1.5">Hit rate</th>
                      <th className="text-right px-2 py-1.5">W</th>
                      <th className="text-right px-2 py-1.5">L</th>
                      <th className="text-right px-2 py-1.5">No-evt</th>
                      <th className="text-right px-2 py-1.5">Amb</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(card.alpha_alignment_breakdown).map(([k, v]) => (
                      <tr key={k} className="border-t border-rd-border">
                        <td className="px-2 py-1.5 text-rd-text">{k}</td>
                        <td className="px-2 py-1.5 text-right" style={{ color: meta.color }}>
                          {fmtPct(v.hit_rate)}
                        </td>
                        <td className="px-2 py-1.5 text-right text-rd-text">{v.wins}</td>
                        <td className="px-2 py-1.5 text-right text-rd-text">{v.losses}</td>
                        <td className="px-2 py-1.5 text-right text-rd-muted">{v.no_event}</td>
                        <td className="px-2 py-1.5 text-right text-rd-muted">{v.ambiguous}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {runtime === "camaro" && card.per_stance && (
            <div className="px-4 py-3 border-b border-rd-border">
              <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-2">
                Per-stance · endorse / veto / observation
              </div>
              <div className="space-y-1.5">
                {Object.entries(card.per_stance).map(([s, m]) => (
                  <div key={s} className="flex items-baseline gap-3 text-[11px] font-mono">
                    <span className="w-24 text-rd-muted uppercase">{s}</span>
                    <span style={{ color: meta.color }} className="w-16">{fmtPct(m.hit_rate)}</span>
                    <span className="text-rd-dim">decisive={m.decisive}</span>
                    <span className="text-rd-dim">total={m.total_resolved}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {runtime === "camaro" && card.regime_breakdown && (
            <div className="px-4 py-3 border-b border-rd-border" data-testid="camaro-regime-breakdown">
              <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-2">
                Endorse hit rate by regime · "which stack do I trust now?"
              </div>
              {(card.regime_breakdown.endorse_only || []).length === 0 ? (
                <div className="text-[11px] font-mono text-rd-dim">
                  No endorse-tagged outcomes yet. Post opinions with{" "}
                  <code className="text-rd-text">regime</code> and stance=endorse.
                </div>
              ) : (
                <div className="border border-rd-border">
                  <table className="w-full text-[11px] font-mono">
                    <thead>
                      <tr className="bg-rd-bg3 text-rd-dim uppercase tracking-widest">
                        <th className="text-left px-2 py-1.5">Regime</th>
                        <th className="text-right px-2 py-1.5">Endorse hit</th>
                        <th className="text-right px-2 py-1.5">W</th>
                        <th className="text-right px-2 py-1.5">L</th>
                        <th className="text-right px-2 py-1.5">n</th>
                      </tr>
                    </thead>
                    <tbody>
                      {card.regime_breakdown.endorse_only.map((row) => (
                        <tr key={row.regime} className="border-t border-rd-border">
                          <td className="px-2 py-1.5 text-rd-text">
                            {row.regime === "_untagged" ? (
                              <span className="text-rd-dim italic">_untagged</span>
                            ) : row.regime}
                          </td>
                          <td className="px-2 py-1.5 text-right" style={{ color: meta.color }}>
                            {fmtPct(row.hit_rate)}
                          </td>
                          <td className="px-2 py-1.5 text-right text-rd-text">{row.wins}</td>
                          <td className="px-2 py-1.5 text-right text-rd-text">{row.losses}</td>
                          <td className="px-2 py-1.5 text-right text-rd-dim">{row.n}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {runtime === "chevelle" && (card.topic_breakdown || []).length > 0 && (
            <div className="px-4 py-3 border-b border-rd-border">
              <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-2">
                Topic reliability · top {card.topic_breakdown.length}
              </div>
              <div className="space-y-1">
                {card.topic_breakdown.slice(0, 8).map((t) => (
                  <div key={t.topic} className="flex items-baseline gap-3 text-[11px] font-mono">
                    <span className="text-rd-text flex-1 truncate" title={t.topic}>{t.topic}</span>
                    <span style={{ color: meta.color }}>{fmtPct(t.hit_rate)}</span>
                    <span className="text-rd-dim">n={t.n}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {runtime === "chevelle" && (card.source_breakdown || []).length > 0 && (
            <div className="px-4 py-3 border-b border-rd-border" data-testid="chevelle-source-breakdown">
              <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-2">
                Source reliability · evidence.source
              </div>
              <div className="border border-rd-border">
                <table className="w-full text-[11px] font-mono">
                  <thead>
                    <tr className="bg-rd-bg3 text-rd-dim uppercase tracking-widest">
                      <th className="text-left px-2 py-1.5">Source</th>
                      <th className="text-right px-2 py-1.5">Hit rate</th>
                      <th className="text-right px-2 py-1.5">W</th>
                      <th className="text-right px-2 py-1.5">L</th>
                      <th className="text-right px-2 py-1.5">n</th>
                    </tr>
                  </thead>
                  <tbody>
                    {card.source_breakdown.slice(0, 12).map((row) => (
                      <tr key={row.source} className="border-t border-rd-border">
                        <td className="px-2 py-1.5 text-rd-text truncate max-w-[180px]" title={row.source}>
                          {row.source === "_unsourced" ? (
                            <span className="text-rd-dim italic">_unsourced</span>
                          ) : row.source}
                        </td>
                        <td className="px-2 py-1.5 text-right" style={{ color: meta.color }}>
                          {fmtPct(row.hit_rate)}
                        </td>
                        <td className="px-2 py-1.5 text-right text-rd-text">{row.wins}</td>
                        <td className="px-2 py-1.5 text-right text-rd-text">{row.losses}</td>
                        <td className="px-2 py-1.5 text-right text-rd-dim">{row.n}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}

      <div className="px-4 py-2.5 bg-rd-bg2 border-t border-rd-border text-[10px] text-rd-dim uppercase tracking-widest leading-relaxed">
        <Target size={10} weight="bold" className="inline mr-1 -mt-0.5" />
        {card.doctrine}
      </div>
    </Card>
  );
}

function Stat({ label, value, accent }) {
  return (
    <div>
      <div className="text-[10px] text-rd-dim uppercase tracking-widest">{label}</div>
      <div className="font-mono text-base" style={accent ? { color: accent } : undefined}>{value}</div>
    </div>
  );
}

function fmtPct(v) {
  if (v == null || isNaN(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}
