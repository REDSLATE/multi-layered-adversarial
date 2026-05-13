import React from "react";
import { Outlet, NavLink, Link } from "react-router-dom";
import { TierProvider, useTier } from "./context/TierContext";
import { TIERS } from "./lib/mc";

function TierBadge() {
  const { tier, setTier } = useTier();
  return (
    <div className="flex items-center gap-2" data-testid="rd-tier-selector-wrap">
      <span className="text-[10px] uppercase tracking-[0.18em] text-zinc-500 font-mono">Tier</span>
      <div className="flex rounded-md border border-slate-600 bg-slate-800/60 p-0.5">
        {TIERS.map((t) => {
          const active = t.id === tier;
          return (
            <button
              key={t.id}
              onClick={() => setTier(t.id)}
              data-testid={`rd-tier-btn-${t.id}`}
              className={
                "whitespace-nowrap px-2.5 py-1 text-[11px] font-mono tracking-wide rounded transition-colors " +
                (active
                  ? "bg-emerald-500/20 text-emerald-300"
                  : "text-zinc-500 hover:text-zinc-200")
              }
            >
              {t.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function NavItem({ to, end, children, testid }) {
  return (
    <NavLink
      to={to}
      end={end}
      data-testid={testid}
      className={({ isActive }) =>
        "px-3 py-1.5 text-[13px] tracking-wide transition-colors " +
        (isActive ? "text-white" : "text-zinc-400 hover:text-zinc-100")
      }
    >
      {children}
    </NavLink>
  );
}

function Header() {
  return (
    <header className="sticky top-0 z-30 border-b border-slate-700 bg-slate-900/80 backdrop-blur supports-[backdrop-filter]:bg-slate-900/60">
      <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
        <Link to="/r" className="flex items-center gap-2" data-testid="rd-logo-home">
          <div className="h-6 w-6 rounded-sm bg-gradient-to-br from-emerald-400 to-emerald-600 shadow-[0_0_24px_-4px_rgba(16,185,129,0.6)]" />
          <span className="font-display text-base tracking-[0.18em] text-white">
            RISE<span className="text-emerald-400">DUAL</span>
          </span>
          <span className="ml-2 hidden font-mono text-[10px] uppercase tracking-[0.22em] text-zinc-600 sm:inline">
            multi-AI · observation only
          </span>
        </Link>
        <nav className="hidden items-center gap-1 md:flex" data-testid="rd-main-nav">
          <NavItem to="/r" end testid="rd-nav-home">Home</NavItem>
          <NavItem to="/r/signals" testid="rd-nav-signals">Signals</NavItem>
          <NavItem to="/r/markets" testid="rd-nav-markets">Markets</NavItem>
          <NavItem to="/r/scanner" testid="rd-nav-scanner">Scanner</NavItem>
          <NavItem to="/r/heatmap" testid="rd-nav-heatmap">Heatmap</NavItem>
          <NavItem to="/r/activity" testid="rd-nav-activity">Activity</NavItem>
          <NavItem to="/r/digest" testid="rd-nav-digest">Digest</NavItem>
          <NavItem to="/r/chat" testid="rd-nav-chat">RiseDualGPT</NavItem>
        </nav>
        <TierBadge />
      </div>
    </header>
  );
}

function Footer() {
  return (
    <footer className="border-t border-slate-700 bg-slate-900">
      <div className="mx-auto flex max-w-7xl flex-col items-start justify-between gap-4 px-6 py-8 text-[12px] text-zinc-500 md:flex-row md:items-center">
        <div className="flex items-center gap-2 font-mono">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.8)]" />
          MC backbone · 4 brains · observation only
        </div>
        <div className="font-mono uppercase tracking-[0.18em]">
          risedual.ai · not financial advice
        </div>
      </div>
    </footer>
  );
}

function LayoutInner() {
  return (
    <div className="min-h-screen bg-slate-900 text-zinc-100 antialiased selection:bg-emerald-500/30">
      <Header />
      <main className="mx-auto max-w-7xl px-6 py-8 md:py-12">
        <Outlet />
      </main>
      <Footer />
    </div>
  );
}

export default function RisedualLayout() {
  return (
    <TierProvider>
      <LayoutInner />
    </TierProvider>
  );
}
