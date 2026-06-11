import React, { useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import { Button } from "@/components/ui/button";
import { ArrowsClockwise, CheckCircle, XCircle, Lightning } from "@phosphor-icons/react";

/**
 * Webull Open API entitlements tile.
 *
 * Shows which Webull data classes the app key is subscribed to so the
 * operator can see in real-time when an Advanced-Quotes subscription
 * propagates after a developer-portal click-through. Live equity
 * quotes feed the equity doctrine enricher; OPRA gates options-aware
 * brain logic (deferred).
 *
 * Backs onto:
 *   GET /api/admin/webull/entitlements
 *   GET /api/admin/webull/snapshot/{symbol}
 */
export default function WebullEntitlementsCard() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const res = await api.get("/admin/webull/entitlements");
      setData(res.data);
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || "fetch failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const tick = () => {
      if (alive) load();
    };
    tick();
    const t = setInterval(tick, 60_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [load]);

  const cls = data?.data_classes || {};
  const rows = [
    { key: "us_stock_quotes", label: "US Equity (Nasdaq Basic L1)", critical: true },
    { key: "us_crypto", label: "US Crypto Spot", critical: true },
    { key: "us_option_quotes", label: "OPRA Options", critical: false },
  ];

  return (
    <Card data-testid="webull-entitlements-card" className="p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Lightning size={18} weight="duotone" className="text-amber-400" />
          <h3 className="text-sm font-semibold tracking-wide">Webull Open API Entitlements</h3>
        </div>
        <Button
          data-testid="webull-entitlements-refresh"
          size="sm"
          variant="ghost"
          onClick={load}
          disabled={loading}
          className="h-7 px-2"
        >
          <ArrowsClockwise size={14} className={loading ? "animate-spin" : ""} />
        </Button>
      </div>

      {err && (
        <div className="text-xs text-rose-400 mb-2" data-testid="webull-entitlements-error">
          {err}
        </div>
      )}

      {!data && !err && (
        <div className="text-xs opacity-60">Probing…</div>
      )}

      {data && (
        <>
          <div className="text-[10px] uppercase tracking-widest opacity-50 mb-2">
            App key {data.configured ? "configured" : "missing"} · base{" "}
            {data.base_subscription ? "ok" : "—"}
          </div>
          <div className="space-y-1.5">
            {rows.map((r) => {
              const live = !!cls[r.key];
              return (
                <div
                  key={r.key}
                  data-testid={`webull-entitlement-${r.key}`}
                  className="flex items-center justify-between text-xs"
                >
                  <span className="opacity-90">{r.label}</span>
                  {live ? (
                    /* 2026-02-19 — Brighter LIVE pill so it pops next
                       to the EXEC ENABLED badges. The old 15% opacity
                       background was washing out against the dark
                       theme and the operator couldn't tell at a
                       glance whether the entitlement was on. */
                    <Badge
                      data-testid={`webull-entitlement-${r.key}-live`}
                      className="bg-emerald-400/30 text-emerald-100 border-emerald-300/60 gap-1 font-semibold shadow-[0_0_8px_rgba(16,185,129,0.35)]"
                    >
                      <CheckCircle size={11} weight="fill" /> LIVE
                    </Badge>
                  ) : (
                    <Badge
                      className={
                        r.critical
                          ? "bg-rose-500/25 text-rose-200 border-rose-400/50 gap-1"
                          : "bg-zinc-500/20 text-zinc-300 border-zinc-500/40 gap-1"
                      }
                    >
                      <XCircle size={11} weight="fill" />{" "}
                      {r.critical ? "not subscribed" : "off"}
                    </Badge>
                  )}
                </div>
              );
            })}
          </div>
          <div className="text-[10px] opacity-40 mt-3 leading-relaxed">
            Stream capacity: {data?.stream_capacity?.max_conns} concurrent MQTT conns ·{" "}
            {data?.stream_capacity?.msg_rate_per_sec} msg/sec push.
            Subscriptions flip on at developer.webull.com → Subscribe Advanced Quotes.
          </div>
        </>
      )}
    </Card>
  );
}
