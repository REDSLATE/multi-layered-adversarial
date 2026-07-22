import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Warning, Gauge } from "@phosphor-icons/react";

const num = (v) => (v === "" || v === null || v === undefined ? null : Number(v));

/** Opportunity Policy — aggression knobs (authority windows, action
 *  tiers, Rise Kernel throttle). Safety layer untouched. */
export default function OpportunityPolicyPanel() {
  const [data, setData] = useState(null);
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data: d } = await api.get("/admin/opportunity-policy");
      setData(d);
      const p = d.policy;
      setForm({
        eqAuth: p.authority_min.equity, crAuth: p.authority_min.crypto,
        eqProbe: p.tiers.equity.probe, eqEnter: p.tiers.equity.enter, eqPress: p.tiers.equity.press,
        crProbe: p.tiers.crypto.probe, crEnter: p.tiers.crypto.enter, crPress: p.tiers.crypto.press,
        nProbe: p.tier_notionals.probe, nEnter: p.tier_notionals.enter, nFull: p.tier_notionals.full,
        kMin: p.kernel.min_mult, kMax: p.kernel.max_mult,
      });
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const post = async (body, okText) => {
    setBusy(true);
    setMsg(null);
    try {
      await api.post("/admin/opportunity-policy", body);
      setMsg({ ok: true, text: okText });
      await load();
    } catch (e) {
      setMsg({ ok: false, text: e?.response?.data?.detail || String(e) });
    } finally {
      setBusy(false);
    }
  };

  const save = () => post({
    authority_min: { equity: num(form.eqAuth), crypto: num(form.crAuth) },
    tiers: {
      equity: { probe: num(form.eqProbe), enter: num(form.eqEnter), press: num(form.eqPress) },
      crypto: { probe: num(form.crProbe), enter: num(form.crEnter), press: num(form.crPress) },
    },
    tier_notionals: { probe: num(form.nProbe), enter: num(form.nEnter), full: num(form.nFull) },
    kernel: { min_mult: num(form.kMin), max_mult: num(form.kMax) },
  }, "opportunity policy saved — applies within ~15s");

  if (!data || !form) return null;
  const p = data.policy;
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const inp = (k, tid, w = "w-12") => (
    <input value={form[k] ?? ""} onChange={set(k)} data-testid={tid}
      className={`${w} bg-transparent border border-rd-border px-1 py-0.5 text-[10px] font-mono text-rd-text focus:outline-none focus:border-rd-text`} />
  );

  const kernelRows = [];
  for (const lane of ["equity", "crypto"]) {
    for (const [brain, k] of Object.entries(data.kernel_throttle?.[lane] || {})) {
      kernelRows.push({ lane, brain, ...k });
    }
  }
  const liveRows = kernelRows.filter((r) => r.state === "live");

  return (
    <div className="border border-rd-border p-3 mb-5" data-testid="opportunity-policy-panel">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono flex items-center gap-1">
          <Gauge size={11} />
          Opportunity Policy · aggression knobs — safety layer untouched
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => post({ tiers_enabled: !p.tiers_enabled }, `tiers ${!p.tiers_enabled ? "ON" : "OFF"}`)}
            disabled={busy} data-testid="opp-tiers-toggle"
            className={`text-[10px] font-mono uppercase tracking-widest px-2 py-1 border ${p.tiers_enabled ? "bg-emerald-600 border-emerald-600 text-black" : "border-rd-border text-rd-dim"}`}>
            tiers {p.tiers_enabled ? "ON" : "OFF"}
          </button>
          <button onClick={() => post({ kernel: { enabled: !p.kernel.enabled } }, `kernel ${!p.kernel.enabled ? "ON" : "OFF"}`)}
            disabled={busy} data-testid="opp-kernel-toggle"
            className={`text-[10px] font-mono uppercase tracking-widest px-2 py-1 border ${p.kernel.enabled ? "bg-emerald-600 border-emerald-600 text-black" : "border-rd-border text-rd-dim"}`}>
            kernel {p.kernel.enabled ? "ON" : "OFF"}
          </button>
        </div>
      </div>

      <div className="flex items-center gap-3 flex-wrap text-[10px] font-mono text-rd-dim py-1">
        <span className="text-[9px] uppercase tracking-widest">authority:</span>
        <label className="flex items-center gap-1">eq {inp("eqAuth", "opp-auth-equity")} min</label>
        <label className="flex items-center gap-1">cr {inp("crAuth", "opp-auth-crypto")} min</label>
        <span className="text-rd-muted">· intents older than this are retained but never executed</span>
      </div>

      <div className="flex items-center gap-3 flex-wrap text-[10px] font-mono text-rd-dim py-1">
        <span className="text-[9px] uppercase tracking-widest">tiers eq:</span>
        probe {inp("eqProbe", "opp-tier-eq-probe")} enter {inp("eqEnter", "opp-tier-eq-enter")} press {inp("eqPress", "opp-tier-eq-press")}
        <span className="text-[9px] uppercase tracking-widest ml-2">cr:</span>
        probe {inp("crProbe", "opp-tier-cr-probe")} enter {inp("crEnter", "opp-tier-cr-enter")} press {inp("crPress", "opp-tier-cr-press")}
      </div>

      <div className="flex items-center gap-3 flex-wrap text-[10px] font-mono text-rd-dim py-1">
        <span className="text-[9px] uppercase tracking-widest">notionals $:</span>
        probe {inp("nProbe", "opp-notional-probe")} enter {inp("nEnter", "opp-notional-enter")} full {inp("nFull", "opp-notional-full")}
        <span className="text-[9px] uppercase tracking-widest ml-2">kernel clamp:</span>
        {inp("kMin", "opp-kernel-min")} – {inp("kMax", "opp-kernel-max")}
        <button onClick={save} disabled={busy} data-testid="opp-save"
          className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-1 disabled:opacity-40">
          save all
        </button>
      </div>

      {msg && (
        <div className="mt-2 px-2 py-1 text-[10px] font-mono border"
          style={{ color: msg.ok ? "#10B981" : "#EF4444", borderColor: msg.ok ? "#10B981" : "#EF4444" }}
          data-testid="opp-msg">
          {!msg.ok && <Warning size={10} className="inline mr-1" />}{msg.text}
        </div>
      )}

      <div className="mt-2 border-t border-rd-border/50 pt-2 text-[10px] font-mono text-rd-dim" data-testid="opp-kernel-status">
        <span className="text-[9px] uppercase tracking-widest">rise kernel throttle: </span>
        {liveRows.length === 0
          ? "all brains cold-start neutral (×1.00) — throttle activates as realized exit outcomes accumulate"
          : liveRows.map((r) => `${r.brain}/${r.lane === "crypto" ? "cr" : "eq"} ×${r.multiplier} (score ${r.score}, ${r.trades}t)`).join(" · ")}
      </div>

      <div className="text-[10px] text-rd-muted mt-2 font-mono leading-relaxed">
        Conviction below probe → WATCH (no capital). Probe/enter/full tiers set base notional before the multiplier
        stack (governor × arbiter × kernel); risk per-order + daily caps still clamp. Kernel = hot-score from realized
        exits mapped to [{p.kernel.min_mult}, {p.kernel.max_mult}] — throttle, never veto.
      </div>
    </div>
  );
}
