import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge, EmptyState } from "@/components/ui-bits";

/**
 * SovereignTile — shows the latest sovereign-state snapshot a brain
 * has POSTed to MC via the runtime patch-kit sidecar.
 *
 * Doctrine reminder rendered inline:
 *   - live_trading_enabled is schema-pinned False everywhere
 *   - confidence_delta is bounded ±0.25 server-side; clamp flag shown
 *   - PRD-mode brains cannot ship training_signal=True
 *
 * If the brain has never contributed (404), we show an empty-state
 * pointer to the patch-kit README so the operator knows how to wire
 * the sidecar.
 */
export default function SovereignTile({ runtime, accent = "#71717A" }) {
  const [state, setState] = useState(null);
  const [error, setError] = useState(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoaded(false);
    setError(null);
    setState(null);
    (async () => {
      try {
        const r = await api.get(`/admin/sovereign/state/${runtime}`);
        if (!alive) return;
        setState(r.data);
      } catch (e) {
        if (!alive) return;
        const status = e?.response?.status;
        if (status === 404) setError("no-snapshot");
        else setError("load-failed");
      } finally {
        if (alive) setLoaded(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [runtime]);

  if (!loaded) {
    return (
      <Card testid={`sovereign-tile-${runtime}-loading`}>
        <div className="label-eyebrow mb-3">Sovereign State</div>
        <div className="text-rd-dim text-xs font-mono">loading...</div>
      </Card>
    );
  }

  if (error === "no-snapshot") {
    return (
      <Card testid={`sovereign-tile-${runtime}-empty`}>
        <div className="label-eyebrow mb-3">Sovereign State</div>
        <EmptyState
          message="No sovereign snapshot on file. Wire the sidecar from runtime_patch_kit/sovereign/ to start ingesting."
        />
      </Card>
    );
  }

  if (error) {
    return (
      <Card testid={`sovereign-tile-${runtime}-error`}>
        <div className="label-eyebrow mb-3">Sovereign State</div>
        <div className="text-rd-danger text-xs font-mono">
          failed to load sovereign state
        </div>
      </Card>
    );
  }

  const s = state || {};
  const weights = s.weights || {};
  const outcomes = s.recent_outcomes || [];
  const wins = outcomes.filter((o) => o.outcome === 1).length;
  const losses = outcomes.filter((o) => o.outcome === -1).length;

  return (
    <Card testid={`sovereign-tile-${runtime}`}>
      <div className="flex items-center justify-between mb-3">
        <div className="label-eyebrow">Sovereign State</div>
        <div className="flex gap-2">
          <Badge color={s.mode === "DTD" ? "#3B82F6" : "#10B981"}>
            {s.mode || "—"}
          </Badge>
          {s.training_signal ? (
            <Badge color="#FBBF24">TRAINING</Badge>
          ) : (
            <Badge color="#71717A">OBS</Badge>
          )}
          {s.delta_was_clamped ? (
            <Badge color="#DC2626" testid={`sovereign-clamp-${runtime}`}>
              CLAMPED
            </Badge>
          ) : null}
        </div>
      </div>

      {/* Posted-as seat — the seat policy at contribution time */}
      <div className="grid grid-cols-2 gap-3 mb-4 text-[11px] font-mono">
        <div>
          <div className="text-rd-dim uppercase tracking-widest mb-1">
            posted_as
          </div>
          <div className="text-rd-text" data-testid={`sovereign-posted-as-${runtime}`}>
            {s.posted_as || "—"}
          </div>
        </div>
        <div>
          <div className="text-rd-dim uppercase tracking-widest mb-1">
            learning_rate
          </div>
          <div className="text-rd-text">{Number(s.learning_rate || 0).toFixed(3)}</div>
        </div>
        <div>
          <div className="text-rd-dim uppercase tracking-widest mb-1">
            confidence_Δ
          </div>
          <div
            className="text-rd-text"
            style={{ color: s.delta_was_clamped ? "#DC2626" : undefined }}
          >
            {Number(s.confidence_delta || 0).toFixed(3)}
            {s.delta_was_clamped && s.raw_confidence_delta != null && (
              <span className="text-rd-dim ml-2">
                (raw {Number(s.raw_confidence_delta).toFixed(3)})
              </span>
            )}
          </div>
        </div>
        <div>
          <div className="text-rd-dim uppercase tracking-widest mb-1">live_trading</div>
          <Badge color="#10B981">FALSE</Badge>
        </div>
      </div>

      {/* Weights — the brain's current personality */}
      <div className="mb-4">
        <div className="text-rd-dim uppercase tracking-widest text-[10px] mb-2">
          weights
        </div>
        {Object.keys(weights).length === 0 ? (
          <div className="text-rd-dim text-xs font-mono">—</div>
        ) : (
          <div className="space-y-1.5" data-testid={`sovereign-weights-${runtime}`}>
            {Object.entries(weights).map(([k, v]) => {
              const pct = Math.min(100, Math.abs(Number(v)) / 3.0 * 100);
              const positive = Number(v) >= 0;
              return (
                <div
                  key={k}
                  className="flex items-center gap-3 text-[11px] font-mono"
                >
                  <span className="text-rd-text w-16">{k}</span>
                  <div className="flex-1 h-1.5 bg-rd-bg3 relative rounded">
                    <div
                      className="absolute top-0 h-1.5 rounded"
                      style={{
                        [positive ? "left" : "right"]: "50%",
                        width: `${pct / 2}%`,
                        backgroundColor: positive ? accent : "#DC2626",
                      }}
                    />
                    {/* zero line */}
                    <div
                      className="absolute top-0 h-1.5"
                      style={{ left: "50%", width: 1, backgroundColor: "#444" }}
                    />
                  </div>
                  <span
                    className="w-12 text-right"
                    style={{ color: positive ? accent : "#DC2626" }}
                  >
                    {Number(v).toFixed(2)}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Recent outcomes ribbon */}
      <div>
        <div className="text-rd-dim uppercase tracking-widest text-[10px] mb-2">
          recent outcomes (last {outcomes.length})
        </div>
        {outcomes.length === 0 ? (
          <div className="text-rd-dim text-xs font-mono">—</div>
        ) : (
          <>
            <div
              className="flex gap-0.5 mb-1"
              data-testid={`sovereign-outcomes-${runtime}`}
            >
              {outcomes.slice(-30).map((o, i) => (
                <div
                  key={o.id || `${o.symbol || ""}-${o.ts || i}`}
                  title={`${o.symbol} ${o.action} c=${o.confidence}`}
                  className="h-3 flex-1"
                  style={{
                    backgroundColor:
                      o.outcome === 1
                        ? "#10B981"
                        : o.outcome === -1
                        ? "#DC2626"
                        : "#3F3F46",
                    minWidth: "4px",
                  }}
                />
              ))}
            </div>
            <div className="text-[10px] text-rd-muted font-mono">
              wins {wins} · losses {losses} · flat {outcomes.length - wins - losses}
            </div>
          </>
        )}
      </div>

      {s.notes ? (
        <div className="mt-3 pt-3 border-t border-rd-border text-[10px] text-rd-muted font-mono">
          {s.notes}
        </div>
      ) : null}

      {s.updated_at ? (
        <div className="mt-2 text-[10px] text-rd-dim font-mono">
          updated {s.updated_at}
        </div>
      ) : null}
    </Card>
  );
}
