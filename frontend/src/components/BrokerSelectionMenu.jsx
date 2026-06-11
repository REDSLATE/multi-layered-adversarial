import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import { Button } from "@/components/ui/button";
import { Check, List as ListIcon, CaretDown, CircleNotch } from "@phosphor-icons/react";
import { toast } from "sonner";

/**
 * Broker selection hamburger — per-lane account switcher.
 *
 * Operator picks which broker EXECUTES trades for each lane. The
 * brain reads the selection on every tick and stamps `broker_override`
 * on emitted intents. Selecting a non-default broker (e.g. Webull for
 * crypto) hot-fails over from the locked-in path (Kraken) so the
 * lane keeps shipping even if the default is offline.
 *
 * Defaults preserved: equity → public, crypto → kraken. Clearing or
 * resetting the selection returns to lane defaults.
 *
 * Backs onto:
 *   GET  /api/admin/broker-selection
 *   PUT  /api/admin/broker-selection  {equity, crypto}
 */
const BROKER_META = {
  public: { label: "Public.com", color: "sky" },
  webull: { label: "Webull",      color: "amber" },
  kraken: { label: "Kraken Pro",  color: "violet" },
};

function BrokerPicker({ lane, value, options, onChange, defaultBroker }) {
  const [open, setOpen] = useState(false);
  const meta = BROKER_META[value] || { label: value, color: "zinc" };
  const isDefault = value === defaultBroker;

  return (
    <div className="relative" data-testid={`broker-picker-${lane}`}>
      <button
        data-testid={`broker-picker-${lane}-toggle`}
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between gap-2 rounded-md border border-white/10 bg-white/5 hover:bg-white/10 px-3 py-2 transition"
      >
        <div className="flex items-center gap-2">
          <ListIcon size={14} className="opacity-60" />
          <span className="text-xs uppercase tracking-widest opacity-50">{lane}</span>
          <span className="text-sm font-semibold">{meta.label}</span>
          {isDefault && (
            <Badge className="bg-zinc-500/15 text-zinc-300 border-zinc-500/30 text-[10px]">
              default
            </Badge>
          )}
        </div>
        <CaretDown size={12} className={`transition ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div
          data-testid={`broker-picker-${lane}-menu`}
          className="absolute z-30 left-0 right-0 mt-1 rounded-md border border-white/10 bg-zinc-900/95 backdrop-blur shadow-xl overflow-hidden"
        >
          {options.map((opt) => {
            const optMeta = BROKER_META[opt] || { label: opt };
            const active = opt === value;
            return (
              <button
                key={opt}
                data-testid={`broker-picker-${lane}-option-${opt}`}
                onClick={() => {
                  onChange(opt);
                  setOpen(false);
                }}
                className={`w-full flex items-center justify-between gap-2 px-3 py-2 text-sm hover:bg-white/10 transition ${
                  active ? "bg-white/5" : ""
                }`}
              >
                <span>{optMeta.label}</span>
                {active && <Check size={14} className="text-emerald-400" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function BrokerSelectionMenu() {
  const [data, setData] = useState(null);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await api.get("/api/admin/broker-selection");
      setData(res.data);
      setErr(null);
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || "fetch failed");
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const tick = () => { if (alive) load(); };
    tick();
    return () => { alive = false; };
  }, [load]);

  const save = useCallback(async (next) => {
    setSaving(true);
    try {
      await api.put("/api/admin/broker-selection", next);
      setData((prev) => ({ ...(prev || {}), selection: next }));
      toast.success(
        `Broker selection saved · equity: ${BROKER_META[next.equity]?.label} · crypto: ${BROKER_META[next.crypto]?.label}`,
        { id: "broker-selection-saved" },
      );
    } catch (e) {
      toast.error(e?.response?.data?.detail || e?.message || "save failed");
    } finally {
      setSaving(false);
    }
  }, []);

  const sel = data?.selection || { equity: "public", crypto: "kraken" };
  const avail = data?.available || { equity: ["public", "webull"], crypto: ["kraken", "webull"] };
  const defaults = data?.defaults || { equity: "public", crypto: "kraken" };

  const onPick = (lane) => (broker) => {
    const next = { ...sel, [lane]: broker };
    save(next);
  };

  return (
    <Card data-testid="broker-selection-menu" className="p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <ListIcon size={18} weight="duotone" className="text-sky-400" />
          <h3 className="text-sm font-semibold tracking-wide">Broker Selection</h3>
        </div>
        {saving && <CircleNotch size={14} className="animate-spin opacity-60" />}
      </div>

      {err && (
        <div className="text-xs text-rose-400 mb-2" data-testid="broker-selection-error">
          {err}
        </div>
      )}

      <div className="grid grid-cols-2 gap-2">
        <BrokerPicker
          lane="equity"
          value={sel.equity}
          options={avail.equity}
          defaultBroker={defaults.equity}
          onChange={onPick("equity")}
        />
        <BrokerPicker
          lane="crypto"
          value={sel.crypto}
          options={avail.crypto}
          defaultBroker={defaults.crypto}
          onChange={onPick("crypto")}
        />
      </div>

      <div className="text-[10px] opacity-50 mt-3 leading-relaxed">
        Brain reads this selection on every tick and stamps it as a
        broker_override on emitted intents. Default = unset → lane
        router picks (equity → Public, crypto → Kraken). Webull for
        crypto is the hot-failover when Kraken is offline; Webull for
        equity is the $3-$10 live drop.
      </div>
    </Card>
  );
}
