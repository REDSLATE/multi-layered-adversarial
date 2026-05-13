import React from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, Cpu, Eye, MessageSquare, ShieldCheck } from "lucide-react";

function Pill({ children }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 text-[11px] font-mono uppercase tracking-[0.18em] text-emerald-300">
      <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_6px_rgba(16,185,129,0.9)]" />
      {children}
    </span>
  );
}

function FeatureCard({ icon: Icon, title, body, testid }) {
  return (
    <div
      data-testid={testid}
      className="group relative overflow-hidden rounded-lg border border-slate-700 bg-slate-800/40 p-6 transition-colors hover:border-slate-600"
    >
      <div className="mb-4 inline-flex h-9 w-9 items-center justify-center rounded-md bg-emerald-500/10 text-emerald-400">
        <Icon size={18} strokeWidth={1.5} />
      </div>
      <div className="font-display text-base text-white">{title}</div>
      <p className="mt-2 text-[13px] leading-relaxed text-zinc-400">{body}</p>
    </div>
  );
}

export default function Landing() {
  return (
    <div className="space-y-24" data-testid="rd-landing-page">
      {/* HERO */}
      <section className="relative pt-8 md:pt-16">
        <div className="absolute -left-32 top-0 h-72 w-72 rounded-full bg-emerald-500/10 blur-3xl" />
        <div className="absolute -right-20 top-20 h-56 w-56 rounded-full bg-cyan-500/5 blur-3xl" />
        <div className="relative max-w-3xl">
          <Pill>4 AIs · 1 verdict · 0 execution</Pill>
          <h1 className="mt-6 font-display text-4xl leading-[1.05] tracking-tight text-white sm:text-5xl lg:text-6xl">
            Four AI brains argue.
            <br />
            <span className="bg-gradient-to-r from-emerald-300 to-emerald-500 bg-clip-text text-transparent">
              You see the verdict.
            </span>
          </h1>
          <p className="mt-6 max-w-xl text-[15px] leading-relaxed text-zinc-400">
            RiseDual runs four independent AI traders in adversarial dialogue —
            a Strategist, a Challenger, a Governor, and an Opponent. They debate
            every signal. We never trade for you. You decide.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <Link
              to="/r/signals"
              data-testid="rd-cta-signals"
              className="inline-flex items-center gap-2 rounded-md bg-emerald-500 px-5 py-2.5 text-[13px] font-medium text-black transition-colors hover:bg-emerald-400"
            >
              See live signals <ArrowUpRight size={14} strokeWidth={2.2} />
            </Link>
            <Link
              to="/r/chat"
              data-testid="rd-cta-chat"
              className="inline-flex items-center gap-2 rounded-md border border-slate-600 bg-slate-800/60 px-5 py-2.5 text-[13px] text-zinc-300 transition-colors hover:border-slate-500 hover:text-white"
            >
              Ask RiseDualGPT <MessageSquare size={14} strokeWidth={1.8} />
            </Link>
          </div>
        </div>
      </section>

      {/* COUNCIL */}
      <section data-testid="rd-council-section">
        <div className="mb-6 flex items-end justify-between">
          <div>
            <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">The Council</div>
            <h2 className="mt-2 font-display text-xl text-white md:text-2xl">
              Four brains. Separated by design.
            </h2>
          </div>
          <div className="hidden text-[11px] font-mono uppercase tracking-[0.18em] text-zinc-600 md:block">
            no peer-to-peer · mediated by Mission Control
          </div>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[
            { name: "Strategist", role: "writes the thesis", bar: "bg-emerald-400" },
            { name: "Challenger", role: "attacks the thesis", bar: "bg-amber-400" },
            { name: "Governor", role: "holds the keys", bar: "bg-sky-400" },
            { name: "Opponent", role: "argues the bear case", bar: "bg-rose-400" },
          ].map((b) => (
            <div
              key={b.name}
              data-testid={`rd-council-${b.name.toLowerCase()}`}
              className="rounded-lg border border-slate-700 bg-slate-800/40 p-5"
            >
              <div className={`mb-3 h-1 w-8 rounded-full ${b.bar}`} />
              <div className="font-display text-base text-white">{b.name}</div>
              <div className="mt-1 text-[12px] font-mono uppercase tracking-[0.16em] text-zinc-500">
                {b.role}
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* WHY */}
      <section data-testid="rd-features-section">
        <div className="mb-6">
          <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">Why RiseDual</div>
          <h2 className="mt-2 font-display text-xl text-white md:text-2xl">
            Built so no single AI can move alone.
          </h2>
        </div>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <FeatureCard
            icon={Cpu}
            title="Adversarial by design"
            body="Every signal earns its place by surviving challenge from a brain whose job is to disagree."
            testid="rd-feature-adversarial"
          />
          <FeatureCard
            icon={ShieldCheck}
            title="Observation only"
            body="No automated execution. Ever. The wire is physically cut. You read the verdict, you decide."
            testid="rd-feature-observation"
          />
          <FeatureCard
            icon={Eye}
            title="One source of truth"
            body="Brains can't talk peer-to-peer. Everything flows through Mission Control. No coordination collusion."
            testid="rd-feature-mediated"
          />
        </div>
      </section>

      {/* CTA */}
      <section className="relative overflow-hidden rounded-xl border border-slate-700 bg-gradient-to-br from-slate-800 to-slate-900 p-8 md:p-12">
        <div className="absolute -right-10 -top-10 h-48 w-48 rounded-full bg-emerald-500/10 blur-3xl" />
        <div className="relative flex flex-col items-start gap-4 md:flex-row md:items-end md:justify-between">
          <div className="max-w-xl">
            <h2 className="font-display text-2xl text-white md:text-3xl">
              Today's tape, read by four AIs.
            </h2>
            <p className="mt-3 text-[14px] text-zinc-400">
              Pull the daily digest, scan active signals, or have a grounded
              conversation about a specific ticker.
            </p>
          </div>
          <div className="flex flex-wrap gap-3">
            <Link
              to="/r/digest"
              data-testid="rd-cta-digest"
              className="inline-flex items-center gap-2 rounded-md bg-white px-5 py-2.5 text-[13px] font-medium text-black transition-colors hover:bg-zinc-200"
            >
              Read today's digest <ArrowUpRight size={14} strokeWidth={2.2} />
            </Link>
          </div>
        </div>
      </section>
    </div>
  );
}
