import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";

const BRAIN_COLOR = {
  camino: "#3B82F6",
  barracuda: "#F59E0B",
  hellcat: "#10B981",
  gto: "#06B6D4",
};

function TokenRow({ row, onReveal, onCopy, onDownload, revealed }) {
  const tone = BRAIN_COLOR[row.runtime] || "#A1A1AA";
  return (
    <tr
      className="border-b border-rd-border hover:bg-rd-bg"
      data-testid={`runtime-token-row-${row.runtime}`}
    >
      <td className="px-3 py-2">
        <Badge color={tone}>{row.runtime.toUpperCase()}</Badge>
      </td>
      <td className="px-3 py-2 font-mono text-[10px] text-rd-dim">{row.env_var}</td>
      <td className="px-3 py-2">
        {row.configured ? (
          <span className="font-mono text-[11px] text-rd-text break-all">
            {revealed?.token || row.token_preview}
          </span>
        ) : (
          <span className="font-mono text-[10px] text-rd-warn">NOT SET ON MC</span>
        )}
      </td>
      <td className="px-3 py-2 text-right whitespace-nowrap">
        {row.configured && (
          <div className="flex gap-1 justify-end">
            <button
              type="button"
              onClick={() => onReveal(row.runtime)}
              data-testid={`runtime-token-reveal-${row.runtime}`}
              className="px-2 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text"
            >
              {revealed?.token ? "HIDE" : "REVEAL"}
            </button>
            {revealed?.token && (
              <button
                type="button"
                onClick={() => onCopy(revealed.token, row.runtime)}
                data-testid={`runtime-token-copy-${row.runtime}`}
                className="px-2 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text"
              >
                COPY
              </button>
            )}
            <button
              type="button"
              onClick={() => onDownload(row.runtime)}
              data-testid={`runtime-token-download-${row.runtime}`}
              className="px-2 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text"
            >
              .ENV
            </button>
          </div>
        )}
      </td>
    </tr>
  );
}

export default function RuntimeTokensPanel() {
  const [items, setItems] = useState([]);
  const [doctrine, setDoctrine] = useState("");
  const [revealed, setRevealed] = useState({}); // {camino: {token}, ...}
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [copyStatus, setCopyStatus] = useState({});

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get("/admin/runtime-tokens");
      setItems(data.items || []);
      setDoctrine(data.doctrine || "");
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const onReveal = useCallback(async (runtime) => {
    if (revealed[runtime]?.token) {
      setRevealed((r) => ({ ...r, [runtime]: undefined }));
      return;
    }
    try {
      const { data } = await api.get("/admin/runtime-tokens", {
        params: { reveal: true, brain: runtime },
      });
      const row = (data.items || [])[0];
      if (row?.token) {
        setRevealed((r) => ({ ...r, [runtime]: { token: row.token } }));
      }
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, [revealed]);

  const onCopy = useCallback(async (token, runtime) => {
    try {
      await navigator.clipboard.writeText(token);
      setCopyStatus((s) => ({ ...s, [runtime]: "copied" }));
      setTimeout(() => {
        setCopyStatus((s) => ({ ...s, [runtime]: undefined }));
      }, 2000);
    } catch {
      setCopyStatus((s) => ({ ...s, [runtime]: "failed" }));
    }
  }, []);

  const onDownload = useCallback(async (runtime) => {
    // Fetch the .env snippet as text + force a download.
    try {
      const resp = await api.get("/admin/runtime-tokens/env-snippet", {
        params: { brain: runtime },
        responseType: "blob",
      });
      const blob = new Blob([resp.data], { type: "text/plain" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${runtime}.env`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, []);

  return (
    <Card testid="runtime-tokens-panel">
      <div className="flex items-center justify-between mb-3">
        <div>
          <div className="label-eyebrow text-rd-dim">Runtime ingest tokens</div>
          <div className="font-display text-lg font-black tracking-tight uppercase">
            Brain authentication
          </div>
        </div>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          data-testid="runtime-tokens-refresh"
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

      <div className="overflow-x-auto border border-rd-border bg-rd-bg3">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-rd-border bg-rd-bg2">
              <th className="px-3 py-2 text-left label-eyebrow text-rd-dim">Brain</th>
              <th className="px-3 py-2 text-left label-eyebrow text-rd-dim">MC env var</th>
              <th className="px-3 py-2 text-left label-eyebrow text-rd-dim">Token</th>
              <th className="px-3 py-2 text-right label-eyebrow text-rd-dim">Actions</th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <TokenRow
                key={row.runtime}
                row={row}
                revealed={revealed[row.runtime]}
                onReveal={onReveal}
                onCopy={onCopy}
                onDownload={onDownload}
              />
            ))}
          </tbody>
        </table>
      </div>

      {/* Inline copy status feedback */}
      {Object.entries(copyStatus).map(([rt, status]) =>
        status === "copied" ? (
          <div
            key={rt}
            className="mt-2 text-[10px] font-mono text-rd-good"
            data-testid={`runtime-token-copy-status-${rt}`}
          >
            ✓ {rt} token copied to clipboard
          </div>
        ) : null,
      )}

      {doctrine && (
        <div className="mt-3 px-3 py-2 border-l-2 border-rd-border text-[10px] font-mono text-rd-dim leading-relaxed">
          {doctrine}
        </div>
      )}

      <div className="mt-3 text-[10px] font-mono text-rd-dim leading-relaxed">
        <span className="text-rd-text">Brain side:</span> set{" "}
        <span className="text-rd-text">MONOREPO_INGEST_TOKEN</span> in each brain's
        local <span className="text-rd-text">.env</span> to the value above.
        The <span className="text-rd-text">.ENV</span> button generates a
        ready-to-drop file with all three required vars.
      </div>
    </Card>
  );
}
