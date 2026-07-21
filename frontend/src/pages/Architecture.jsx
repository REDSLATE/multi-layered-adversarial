import React, { useState } from "react";
import { PageHeader } from "@/components/ui-bits";
import { ArrowsOutSimple, ArrowsInSimple } from "@phosphor-icons/react";

const PRINCIPLES = [
  "Brains advise. The Seat decides.",
  "The Seat executes. No other.",
  "Every order is real.",
  "Every outcome is learned.",
  "One Pulse. One Truth. One System.",
];

const SPINE = [
  { name: "Stage Trace", detail: "Every decision point recorded per routed intent" },
  { name: "Kill Map", detail: "Where trades die — and why" },
  { name: "End-to-End Trace", detail: "Snapshot → Brain → Seat → Risk → Broker → Fill" },
  { name: "Receipts & Reconciliation", detail: "Submitted, rejected, filled, partial, expired" },
  { name: "Retention Sweeper", detail: "Autonomous 72h diagnostic-data lifecycle" },
];

export default function Architecture() {
  const [zoomed, setZoomed] = useState(false);

  return (
    <div className="space-y-4" data-testid="architecture-page">
      <PageHeader
        eyebrow="System Blueprint"
        title="Architecture"
        sub="Intelligence, execution, and learning at scale — one pulse, one truth, one system."
        testid="architecture-header"
        right={
          <a
            href="/architecture.png"
            target="_blank"
            rel="noreferrer"
            className="text-[10px] font-mono uppercase tracking-widest px-3 py-1.5 border border-rd-dim/40 rounded hover:border-rd-text transition-colors"
            data-testid="architecture-open-full"
          >
            Open Full Size
          </a>
        }
      />

      <div
        className={`relative border border-rd-dim/30 rounded-lg bg-black/40 ${zoomed ? "overflow-auto" : "overflow-hidden"}`}
        data-testid="architecture-diagram-frame"
      >
        <button
          onClick={() => setZoomed((z) => !z)}
          className="absolute top-2 right-2 z-10 p-2 rounded bg-black/70 border border-rd-dim/40 text-rd-text hover:border-rd-text transition-colors"
          title={zoomed ? "Fit to width" : "Zoom to native size"}
          data-testid="architecture-zoom-toggle"
        >
          {zoomed ? <ArrowsInSimple size={16} /> : <ArrowsOutSimple size={16} />}
        </button>
        <img
          src="/architecture.png"
          alt="RISEDUAL Architecture — AI Trading Enterprise"
          className={zoomed ? "max-w-none cursor-zoom-out" : "w-full cursor-zoom-in"}
          onClick={() => setZoomed((z) => !z)}
          data-testid="architecture-diagram-img"
        />
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        <div className="border border-rd-dim/30 rounded-lg p-4 bg-black/40" data-testid="architecture-principles">
          <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-2">
            Core Principles
          </div>
          <ul className="space-y-1.5">
            {PRINCIPLES.map((p) => (
              <li key={p} className="text-[12px] text-rd-text pl-3 relative before:content-['—'] before:absolute before:left-0 before:text-rd-dim">
                {p}
              </li>
            ))}
          </ul>
        </div>
        <div className="border border-rd-dim/30 rounded-lg p-4 bg-black/40" data-testid="architecture-spine">
          <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-2">
            Diagnostic &amp; Observability Spine
          </div>
          <ul className="space-y-1.5">
            {SPINE.map((s) => (
              <li key={s.name} className="text-[12px] text-rd-text pl-3 relative before:content-['—'] before:absolute before:left-0 before:text-rd-dim">
                <span className="font-mono">{s.name}</span>
                <span className="text-rd-dim"> · {s.detail}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
