import React, { useState } from "react";
import { api } from "@/lib/api";
import { ArrowsClockwise, MagnifyingGlass } from "@phosphor-icons/react";

/**
 * BrainDeepDiagnoseCard — operator-facing one-tap deep diagnose for
 * any brain. Calls /admin/brain/emission-diagnose/{brain} and
 * /admin/runtime/{brain}/status and renders the raw JSON. Read-only,
 * never mutates anything.
 *
 * Purpose: avoid having to drop into mobile dev tools to run an
 * api.get() by hand. One tap → full picture.
 */
const BRAINS = ["alpha", "camaro", "chevelle", "redeye"];


function JsonBlock({ data, label, testid }) {
  if (!data) return null;
  return (
    <div className="border border-rd-border bg-rd-bg2 p-3 mb-2" data-testid={testid}>
      <div className="label-eyebrow mb-2 text-rd-warning">{label}</div>
      <pre className="text-[10px] font-mono leading-snug whitespace-pre-wrap break-words text-rd-text overflow-auto max-h-96">
{JSON.stringify(data, null, 2)}
      </pre>
    </div>
  );
}


export default function BrainDeepDiagnoseCard() {
  const [brain, setBrain] = useState("redeye");
  const [emission, setEmission] = useState(null);
  const [status, setStatus] = useState(null);
  const [emissionErr, setEmissionErr] = useState("");
  const [statusErr, setStatusErr] = useState("");
  const [busy, setBusy] = useState(false);

  const fire = async (b) => {
    setBusy(true);
    setEmission(null);
    setStatus(null);
    setEmissionErr("");
    setStatusErr("");
    try {
      const r = await api.get(`/admin/brain/emission-diagnose/${b}`);
      setEmission(r.data);
    } catch (e) {
      setEmissionErr(
        e?.response?.data?.detail
          ? JSON.stringify(e.response.data.detail)
          : (e?.response?.status ? `HTTP ${e.response.status}` : e.message)
      );
    }
    try {
      const r = await api.get(`/admin/runtime/${b}/status`);
      setStatus(r.data);
    } catch (e) {
      setStatusErr(
        e?.response?.data?.detail
          ? JSON.stringify(e.response.data.detail)
          : (e?.response?.status ? `HTTP ${e.response.status}` : e.message)
      );
    }
    setBusy(false);
  };

  return (
    <div
      className="border border-rd-border bg-rd-bg p-4 mb-6 font-mono"
      data-testid="brain-deep-diagnose-card"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-rd-warning">
          <MagnifyingGlass size={14} weight="bold" />
          <span className="label-eyebrow text-rd-warning">Deep diagnose · per brain</span>
        </div>
        {busy && <ArrowsClockwise size={12} className="animate-spin text-rd-dim" />}
      </div>

      <div className="flex flex-wrap gap-1 mb-3">
        {BRAINS.map((b) => (
          <button
            key={b}
            onClick={() => { setBrain(b); fire(b); }}
            disabled={busy}
            data-testid={`brain-deep-diagnose-btn-${b}`}
            className={
              "px-3 py-1 text-[11px] uppercase tracking-wider border " +
              (brain === b
                ? "border-rd-warning text-rd-warning"
                : "border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text")
            }
          >
            {b}
          </button>
        ))}
        <button
          onClick={() => fire(brain)}
          disabled={busy}
          data-testid="brain-deep-diagnose-refire"
          className="px-3 py-1 text-[11px] uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text ml-auto"
        >
          refresh
        </button>
      </div>

      {!emission && !status && !emissionErr && !statusErr && !busy && (
        <div className="text-rd-dim text-[11px]">
          Tap a brain to fire `/admin/brain/emission-diagnose/{brain}` and `/admin/runtime/{brain}/status`. Read-only.
        </div>
      )}

      {emissionErr && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-2 text-[11px]">
          emission-diagnose: {emissionErr}
        </div>
      )}
      {statusErr && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-2 text-[11px]">
          runtime/status: {statusErr}
        </div>
      )}

      <JsonBlock
        data={emission}
        label={`emission-diagnose · ${brain}`}
        testid={`brain-deep-diagnose-emission-${brain}`}
      />
      <JsonBlock
        data={status}
        label={`runtime/status · ${brain}`}
        testid={`brain-deep-diagnose-status-${brain}`}
      />
    </div>
  );
}
