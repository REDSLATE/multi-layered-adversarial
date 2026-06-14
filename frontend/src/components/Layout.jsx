import React, { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";
import MisreadToastHost from "@/components/MisreadToastHost";
import {
  ChartBar,
  Receipt,
  Shield,
  Wrench,
  Cube,
  Stack,
  Pulse,
  Flag,
  TrendUp,
  Trophy,
  LightningSlash,
  Lightning,
  ChatCircleDots,
  Crosshair,
  Sparkle,
  Brain,
  Eye,
  SignOut,
  List as Hamburger,
  X as CloseIcon,
} from "@phosphor-icons/react";

// Grouped navigation. Order within each section matters: most-used at top.
const SECTIONS = [
  {
    label: "RISE_AI",
    items: [
      { to: "/admin/rise-ai", label: "Console", icon: Brain, testid: "nav-rise-ai" },
    ],
  },
  {
    label: "Trading",
    items: [
      { to: "/admin/hypothesis", label: "Hypothesis", icon: Sparkle, testid: "nav-hypothesis" },
      { to: "/admin/intents", label: "Intents", icon: Lightning, testid: "nav-intents" },
      { to: "/admin/paradox", label: "Paradox V2", icon: Eye, testid: "nav-paradox" },
      { to: "/admin/learning-ladder", label: "Live Routes", icon: TrendUp, testid: "nav-learning-ladder" },
      { to: "/admin/positions", label: "Positions", icon: Crosshair, testid: "nav-positions" },
    ],
  },
  {
    label: "Governance",
    items: [
      { to: "/admin/overview", label: "Overview", icon: ChartBar, testid: "nav-overview" },
      { to: "/admin/discussion", label: "Discussion", icon: ChatCircleDots, testid: "nav-discussion" },
      { to: "/admin/scorecards", label: "Scorecards", icon: Trophy, testid: "nav-scorecards" },
      { to: "/admin/doctrine", label: "Doctrine", icon: Shield, testid: "nav-doctrine" },
      { to: "/admin/doctrine-reference", label: "Doctrine Ref", icon: Shield, testid: "nav-doctrine-reference" },
      { to: "/admin/safety-gates", label: "Safety Gates", icon: Shield, testid: "nav-safety-gates" },
      { to: "/admin/conflicts", label: "Conflicts", icon: LightningSlash, testid: "nav-conflicts" },
      { to: "/admin/promotion", label: "Promotion", icon: TrendUp, testid: "nav-promotion" },
    ],
  },
  {
    label: "Audit",
    items: [
      { to: "/admin/mc-shelly", label: "MC Memory", icon: Brain, testid: "nav-mc-shelly" },
      { to: "/admin/llm-ledger", label: "LLM Ledger", icon: Sparkle, testid: "nav-llm-ledger" },
      { to: "/admin/receipts", label: "ADL Receipts", icon: Receipt, testid: "nav-receipts" },
      { to: "/admin/memory", label: "Memory Firewall", icon: Shield, testid: "nav-memory" },
      { to: "/admin/recent", label: "Live Tail", icon: Pulse, testid: "nav-recent" },
      { to: "/admin/diagnostics", label: "Diagnostics", icon: Pulse, testid: "nav-diagnostics" },
    ],
  },
  {
    label: "System",
    items: [
      { to: "/admin/calibration", label: "Calibration", icon: Wrench, testid: "nav-calibration" },
      { to: "/admin/feature-builders", label: "Feature Builders", icon: Stack, testid: "nav-features" },
      { to: "/admin/artifacts", label: "Artifacts", icon: Cube, testid: "nav-artifacts" },
      { to: "/admin/flags", label: "Runtime Flags", icon: Flag, testid: "nav-flags" },
      { to: "/admin/public-traffic", label: "Public Traffic", icon: Pulse, testid: "nav-public-traffic" },
    ],
  },
];

const RUNTIMES = [
  { to: "/admin/brain/alpha", label: "Alpha", color: "#3B82F6", testid: "nav-runtime-alpha" },
  { to: "/admin/brain/camaro", label: "Camaro", color: "#F59E0B", testid: "nav-runtime-camaro" },
  { to: "/admin/brain/chevelle", label: "Chevelle", color: "#10B981", testid: "nav-runtime-chevelle" },
  { to: "/admin/brain/redeye", label: "REDEYE", color: "#DC2626", testid: "nav-runtime-redeye" },
];

// Per-brain operator/verification dashboards. One per brain so each
// runtime team has a verification page after pushing updates.
const BRAIN_OPERATORS = [
  { to: "/admin/brain-op/alpha",    label: "Alpha Ops",    color: "#3B82F6", testid: "nav-brain-op-alpha" },
  { to: "/admin/brain-op/camaro",   label: "Camaro Ops",   color: "#F59E0B", testid: "nav-brain-op-camaro" },
  { to: "/admin/brain-op/chevelle", label: "Chevelle Ops", color: "#10B981", testid: "nav-brain-op-chevelle" },
  { to: "/admin/brain-op/redeye",   label: "REDEYE Ops",   color: "#DC2626", testid: "nav-brain-op-redeye" },
];

// Find a short title for the mobile top bar based on the current route.
function currentTitle(pathname) {
  for (const sec of SECTIONS) {
    const hit = sec.items.find((i) => pathname.startsWith(i.to));
    if (hit) return hit.label;
  }
  const r = RUNTIMES.find((i) => pathname.startsWith(i.to));
  if (r) return r.label;
  const b = BRAIN_OPERATORS.find((i) => pathname.startsWith(i.to));
  if (b) return b.label;
  return "Mission Control";
}

function NavSection({ section, onNavigate, loc }) {
  return (
    <div className="mb-5">
      <div className="label-eyebrow px-3 mb-1.5 text-rd-dim">{section.label}</div>
      <ul className="space-y-px">
        {section.items.map((n) => (
          <li key={n.to}>
            <NavLink
              to={n.to}
              data-testid={n.testid}
              onClick={onNavigate}
              className={({ isActive }) =>
                `flex items-center gap-2.5 px-3 min-h-[44px] md:min-h-[36px] text-[11px] md:text-[11px] uppercase tracking-widest font-bold transition-colors ${
                  isActive
                    ? "bg-rd-bg3 text-rd-text border-l-2 border-rd-warn"
                    : "text-rd-muted hover:text-rd-text hover:bg-rd-bg3 border-l-2 border-transparent"
                }`
              }
            >
              <n.icon size={15} weight="bold" />
              <span>{n.label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ColoredList({ title, items, onNavigate, loc, dot = "square" }) {
  return (
    <div className="mb-5">
      <div className="label-eyebrow px-3 mb-1.5 text-rd-dim">{title}</div>
      <ul className="space-y-px">
        {items.map((r) => (
          <li key={r.to}>
            <NavLink
              to={r.to}
              data-testid={r.testid}
              onClick={onNavigate}
              className={({ isActive }) =>
                `flex items-center gap-2.5 px-3 min-h-[44px] md:min-h-[36px] text-[11px] uppercase tracking-widest font-bold transition-colors ${
                  isActive
                    ? "bg-rd-bg3 text-rd-text"
                    : "text-rd-muted hover:text-rd-text hover:bg-rd-bg3"
                }`
              }
              style={{
                borderLeft: `2px solid ${
                  loc.pathname.startsWith(r.to) ? r.color : "transparent"
                }`,
              }}
            >
              <span
                className={`inline-block w-2.5 h-2.5 ${dot === "round" ? "rounded-full" : ""}`}
                style={{ background: r.color }}
              />
              {r.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Sidebar({ user, logout, loc, onNavigate }) {
  return (
    <div className="h-full flex flex-col bg-rd-bg2">
      <div className="px-5 py-4 border-b border-rd-border flex items-center gap-2 shrink-0">
        <span className="inline-block w-3 h-3 bg-rd-warn pulse-dot" />
        <div>
          <div className="font-display text-sm font-black tracking-tighter">RISEDUAL</div>
          <div className="label-eyebrow">mission control</div>
        </div>
      </div>

      <nav className="px-2 py-4 flex-1 overflow-y-auto overscroll-contain" data-testid="primary-nav">
        {SECTIONS.map((s) => (
          <NavSection key={s.label} section={s} onNavigate={onNavigate} loc={loc} />
        ))}
        <ColoredList title="Runtimes" items={RUNTIMES} onNavigate={onNavigate} loc={loc} />
        <ColoredList title="Brain Operators" items={BRAIN_OPERATORS} onNavigate={onNavigate} loc={loc} dot="round" />
      </nav>

      <div className="border-t border-rd-border px-3 py-3 shrink-0">
        <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-1">Operator</div>
        <div className="text-xs font-mono text-rd-text mb-3 truncate" title={user?.email || ""}>
          {user?.email || "—"}
        </div>
        <button
          onClick={logout}
          className="btn-sharp w-full border border-rd-border text-rd-muted hover:text-rd-text hover:border-rd-borderStrong px-3 py-2 flex items-center justify-center gap-2"
          data-testid="logout-button"
        >
          <SignOut size={12} weight="bold" />
          Sign out
        </button>
      </div>
    </div>
  );
}

export default function Layout() {
  const { user, logout } = useAuth();
  const loc = useLocation();
  const [drawerOpen, setDrawerOpen] = useState(false);

  // Auto-close drawer on route change.
  useEffect(() => { setDrawerOpen(false); }, [loc.pathname]);

  // Lock body scroll while drawer is open.
  useEffect(() => {
    if (drawerOpen) {
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => { document.body.style.overflow = prev; };
    }
  }, [drawerOpen]);

  // Close drawer on Escape.
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e) => { if (e.key === "Escape") setDrawerOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  return (
    <div className="min-h-screen bg-rd-bg text-rd-text app-shell" data-testid="app-shell">
      {/* Ephemeral position-misread toast host — mounted once at the
          layout level so it surfaces on every admin page (the operator
          could be looking at any tab when the next misread lands). */}
      <MisreadToastHost />

      {/* MOBILE top bar — only visible < md */}
      <div className="md:hidden sticky top-0 z-30 bg-rd-bg2 border-b border-rd-border flex items-center justify-between px-3 h-12">
        <button
          onClick={() => setDrawerOpen(true)}
          aria-label="Open menu"
          data-testid="mobile-menu-button"
          className="flex items-center justify-center w-10 h-10 -ml-2 text-rd-text active:bg-rd-bg3"
        >
          <Hamburger size={22} weight="bold" />
        </button>
        <div className="flex items-center gap-2">
          <span className="inline-block w-2 h-2 bg-rd-warn pulse-dot" />
          <div className="font-display text-xs font-black tracking-tighter uppercase">
            {currentTitle(loc.pathname)}
          </div>
        </div>
        <div className="w-10" />
      </div>

      <div className="md:grid md:grid-cols-12 md:min-h-[calc(100vh-26px)]">
        {/* DESKTOP sidebar — hidden on mobile (drawer takes over) */}
        <aside
          className="hidden md:flex md:col-span-3 lg:col-span-2 border-r border-rd-border flex-col sticky top-0 self-start max-h-screen"
          data-testid="desktop-sidebar"
        >
          <Sidebar user={user} logout={logout} loc={loc} onNavigate={() => {}} />
        </aside>

        {/* MOBILE drawer */}
        {drawerOpen && (
          <>
            <div
              className="md:hidden fixed inset-0 bg-black/60 z-40"
              onClick={() => setDrawerOpen(false)}
              data-testid="mobile-drawer-backdrop"
            />
            <aside
              className="md:hidden fixed inset-y-0 left-0 z-50 w-[82%] max-w-[320px] border-r border-rd-border shadow-2xl flex flex-col"
              data-testid="mobile-drawer"
            >
              <div className="flex items-center justify-between border-b border-rd-border h-12 px-3 bg-rd-bg2 shrink-0">
                <div className="flex items-center gap-2">
                  <span className="inline-block w-2.5 h-2.5 bg-rd-warn pulse-dot" />
                  <div className="font-display text-xs font-black tracking-tighter">RISEDUAL</div>
                </div>
                <button
                  onClick={() => setDrawerOpen(false)}
                  aria-label="Close menu"
                  data-testid="mobile-menu-close"
                  className="flex items-center justify-center w-10 h-10 -mr-2 text-rd-text active:bg-rd-bg3"
                >
                  <CloseIcon size={20} weight="bold" />
                </button>
              </div>
              <div className="flex-1 min-h-0">
                <Sidebar
                  user={user}
                  logout={logout}
                  loc={loc}
                  onNavigate={() => setDrawerOpen(false)}
                />
              </div>
            </aside>
          </>
        )}

        {/* Main content. Top-level boundary so any unwrapped child route
            that throws shows a typed error instead of a blank page. */}
        <main
          className="md:col-span-9 lg:col-span-10 p-4 md:p-6 overflow-x-hidden"
          data-testid="admin-main"
        >
          <PanelErrorBoundary panelName="Page" testid="panel-error-page">
            <Outlet />
          </PanelErrorBoundary>
        </main>
      </div>
    </div>
  );
}
