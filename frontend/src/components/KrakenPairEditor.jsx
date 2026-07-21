import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Warning, X, Plus } from "@phosphor-icons/react";

/** Pair Map Editor — add Kraken pairs at runtime. Every add is
 *  validated against Kraken's public AssetPairs before it saves. */
export default function KrakenPairEditor() {
  const [data, setData] = useState(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data: d } = await api.get("/admin/kraken-pairs");
      setData(d);
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const add = async (symbol) => {
    const sym = (symbol || input).trim();
    if (!sym) return;
    setBusy(true);
    setMsg(null);
    try {
      const { data: r } = await api.post("/admin/kraken-pairs", { symbol: sym });
      setMsg({ ok: true, text: `${r.canonical} → ${r.kraken_pair} (Kraken min ${r.ordermin ?? "?"}) — live within 60s` });
      setInput("");
      await load();
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally {
      setBusy(false);
    }
  };

  const remove = async (canonical) => {
    const base = canonical.replace("CRYPTO:", "").split("-")[0];
    setBusy(true);
    try {
      await api.delete(`/admin/kraken-pairs/${base}`);
      setMsg({ ok: true, text: `${canonical} removed` });
      await load();
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally {
      setBusy(false);
    }
  };

  if (!data) return null;

  return (
    <div className="border border-rd-border p-3 mb-5" data-testid="kraken-pair-editor">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
          Kraken Pair Map · {data.static_count} static + {data.overrides.length} added
        </div>
      </div>

      <div className="flex items-center gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !busy && add()}
          placeholder="e.g. YGG or YGG/USD"
          className="flex-1 bg-transparent border border-rd-border px-2 py-1 text-xs font-mono text-rd-text placeholder:text-rd-dim focus:outline-none focus:border-rd-text"
          data-testid="kraken-pair-input"
        />
        <button
          onClick={() => add()}
          disabled={busy || !input.trim()}
          data-testid="kraken-pair-add"
          className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-1 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
        >
          <Plus size={10} />{busy ? "validating…" : "add pair"}
        </button>
      </div>

      {msg && (
        <div
          className="mt-2 px-2 py-1 text-[10px] font-mono border"
          style={{ color: msg.ok ? "#10B981" : "#EF4444", borderColor: msg.ok ? "#10B981" : "#EF4444" }}
          data-testid="kraken-pair-msg"
        >
          {!msg.ok && <Warning size={10} className="inline mr-1" />}{msg.text}
        </div>
      )}

      {data.suggestions.length > 0 && (
        <div className="mt-2" data-testid="kraken-pair-suggestions">
          <span className="text-[9px] uppercase tracking-widest text-rd-dim font-mono mr-2">
            Recently rejected:
          </span>
          {data.suggestions.map((s) => (
            <button
              key={s.symbol}
              onClick={() => add(s.symbol)}
              disabled={busy}
              className="text-[10px] font-mono border border-rd-border hover:border-rd-text px-1.5 py-0.5 mr-1 mb-1 text-rd-text"
              title={`${s.rejected_count} rejected emissions — click to validate & add`}
            >
              {s.symbol} <span className="text-rd-dim">×{s.rejected_count}</span>
            </button>
          ))}
        </div>
      )}

      {data.overrides.length > 0 && (
        <div className="mt-2 border-t border-rd-border/50 pt-2" data-testid="kraken-pair-overrides">
          {data.overrides.map((o) => (
            <div key={o.canonical} className="flex items-center gap-2 text-[10px] font-mono py-0.5">
              <span className="font-bold text-rd-text w-24 shrink-0">{o.symbol}</span>
              <span className="text-rd-dim">→ {o.kraken_pair}</span>
              <span className="text-rd-dim">min {o.ordermin ?? "?"}</span>
              <span className="text-rd-dim truncate flex-1">{(o.ts || "").slice(0, 16).replace("T", " ")} · {o.added_by}</span>
              <button
                onClick={() => remove(o.canonical)}
                disabled={busy}
                data-testid={`kraken-pair-remove-${o.symbol?.split("/")[0]}`}
                className="text-rd-dim hover:text-rd-danger"
                title="Remove override"
              >
                <X size={11} />
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="text-[10px] text-rd-muted mt-2 font-mono leading-relaxed">
        Adds are validated live against Kraken AssetPairs before saving — typos can't route orders.
        New pairs become emit-able and tradable within 60s (no deploy). Static top-30 map stays in code.
      </div>
    </div>
  );
}
