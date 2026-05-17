import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";

const KRAKEN_STATE_COLOR = {
  ok: "#10B981",
  no_credentials: "#F59E0B",
  missing_field: "#F59E0B",
  decrypt_failed: "#DC2626",
};

function GateRow({ gate }) {
  const passed = gate.passed;
  return (
    <div
      className="flex items-start gap-2 py-1.5 px-2 border-b border-rd-border last:border-0"
      data-testid={`live-trade-gate-${gate.name}`}
    >
      <span
        className="font-mono text-[10px] mt-0.5"
        style={{ color: passed ? "#10B981" : "#DC2626" }}
      >
        {passed ? "PASS" : "BLOCK"}
      </span>
      <div className="flex-1 min-w-0">
        <div className="font-mono text-[11px] text-rd-text uppercase tracking-wide">
          {gate.name}
        </div>
        <div className="font-mono text-[10px] text-rd-dim break-words">
          {gate.reason}
        </div>
      </div>
    </div>
  );
}

function LaneCard({ lane, notional }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get("/admin/execution/diagnose", {
        params: { lane, notional_usd: notional },
      });
      setData(data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, [lane, notional]);

  useEffect(() => { load(); }, [load]);

  const broker = data?.broker || {};
  const kraken = broker.kraken_credentials;
  const alpaca = broker.alpaca_credentials;
  const verdictColor = data?.verdict === "would_pass" ? "#10B981" : "#DC2626";

  return (
    <Card testid={`live-trade-diagnose-${lane}`}>
      <div className="flex items-center justify-between mb-3">
        <div>
          <div className="label-eyebrow text-rd-dim">{lane === "crypto" ? "Crypto · Kraken" : "Equity · Alpaca"}</div>
          <div className="font-display text-lg font-black tracking-tight uppercase">
            Live trade: {data?.verdict === "would_pass" ? "READY" : "BLOCKED"}
          </div>
        </div>
        <button
          onClick={load}
          disabled={loading}
          data-testid={`live-trade-diagnose-refresh-${lane}`}
          className="px-2 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text disabled:opacity-50"
        >
          {loading ? "…" : "REFRESH"}
        </button>
      </div>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-3 text-xs font-mono">
          {err}
        </div>
      )}

      {data && (
        <>
          {data.first_blocker && (
            <div
              className="mb-3 px-3 py-2 border border-rd-danger bg-rd-bg3"
              data-testid={`live-trade-first-blocker-${lane}`}
            >
              <div className="label-eyebrow text-rd-danger mb-1">First blocker</div>
              <div className="font-mono text-[11px] text-rd-text">{data.first_blocker.name}</div>
              <div className="font-mono text-[10px] text-rd-dim mt-0.5">{data.first_blocker.reason}</div>
            </div>
          )}

          {/* Broker / credentials status */}
          <div className="mb-3 px-3 py-2 border border-rd-border bg-rd-bg3">
            <div className="label-eyebrow text-rd-dim mb-1">Broker adapter</div>
            <div className="flex items-center gap-2 text-[11px] font-mono">
              <Badge color={broker.adapter_loaded ? "#10B981" : "#F59E0B"}>
                {broker.adapter_loaded ? "LOADED" : "NOT LOADED"}
              </Badge>
              <span className="text-rd-dim">{broker.adapter_name || "—"}</span>
            </div>
            {kraken && (
              <div className="mt-2 text-[10px] font-mono">
                <div className="flex items-center gap-2">
                  <span className="text-rd-dim">kraken credentials:</span>
                  <Badge color={KRAKEN_STATE_COLOR[kraken.state] || "#A1A1AA"}>
                    {(kraken.state || "unknown").toUpperCase().replace(/_/g, " ")}
                  </Badge>
                </div>
                <div className="text-rd-dim mt-1 break-words">{kraken.detail}</div>
                {kraken.public_key_preview && (
                  <div className="text-rd-dim mt-0.5">key preview: {kraken.public_key_preview}</div>
                )}
              </div>
            )}
            {alpaca !== undefined && (
              <div className="mt-2 text-[10px] font-mono">
                <span className="text-rd-dim">alpaca singleton: </span>
                {alpaca === null ? (
                  <Badge color="#F59E0B">NOT CONNECTED</Badge>
                ) : (
                  <span className="text-rd-text">
                    paper={String(alpaca.paper)} exec={String(alpaca.execution_enabled)}
                  </span>
                )}
              </div>
            )}
          </div>

          {/* Gate-chain table */}
          <div className="border border-rd-border bg-rd-bg3" data-testid={`live-trade-gates-${lane}`}>
            <div className="px-2 py-1 border-b border-rd-border bg-rd-bg2">
              <span className="label-eyebrow text-rd-dim">Gate chain · synthetic {data.sample_symbol} BUY ${data.synthetic_notional_usd}</span>
              <span className="ml-2 font-mono text-[10px]" style={{ color: verdictColor }}>
                {data.verdict?.toUpperCase()}
              </span>
            </div>
            <div className="max-h-80 overflow-auto">
              {data.gates?.map((g) => <GateRow key={g.name} gate={g} />)}
            </div>
          </div>

          <div className="mt-2 text-[9px] font-mono text-rd-dim">
            Checked: {data.checked_at}
          </div>
        </>
      )}
    </Card>
  );
}

export default function LiveTradeDiagnose() {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 lg:gap-6" data-testid="live-trade-diagnose">
      <LaneCard lane="crypto" notional={25} />
      <LaneCard lane="equity" notional={100} />
    </div>
  );
}
