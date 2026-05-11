import React from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
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
  ChatCircleDots,
  Crosshair,
  SignOut,
} from "@phosphor-icons/react";

const NAV = [
  { to: "/", label: "Overview", icon: ChartBar, end: true, testid: "nav-overview" },
  { to: "/receipts", label: "ADL Receipts", icon: Receipt, testid: "nav-receipts" },
  { to: "/memory", label: "Memory Firewall", icon: Shield, testid: "nav-memory" },
  { to: "/calibration", label: "Calibration", icon: Wrench, testid: "nav-calibration" },
  { to: "/feature-builders", label: "Feature Builders", icon: Stack, testid: "nav-features" },
  { to: "/artifacts", label: "Artifacts", icon: Cube, testid: "nav-artifacts" },
  { to: "/diagnostics", label: "Diagnostics", icon: Pulse, testid: "nav-diagnostics" },
  { to: "/recent", label: "Live Tail", icon: Pulse, testid: "nav-recent" },
  { to: "/discussion", label: "Discussion", icon: ChatCircleDots, testid: "nav-discussion" },
  { to: "/positions", label: "Positions", icon: Crosshair, testid: "nav-positions" },
  { to: "/scorecards", label: "Scorecards", icon: Trophy, testid: "nav-scorecards" },
  { to: "/conflicts", label: "Conflicts", icon: LightningSlash, testid: "nav-conflicts" },
  { to: "/flags", label: "Runtime Flags", icon: Flag, testid: "nav-flags" },
  { to: "/promotion", label: "Promotion", icon: TrendUp, testid: "nav-promotion" },
];

const RUNTIMES = [
  { to: "/runtime/alpha", label: "Alpha", color: "#3B82F6", testid: "nav-runtime-alpha" },
  { to: "/runtime/camaro", label: "Camaro", color: "#F59E0B", testid: "nav-runtime-camaro" },
  { to: "/runtime/chevelle", label: "Chevelle", color: "#10B981", testid: "nav-runtime-chevelle" },
  { to: "/redeye", label: "REDEYE", color: "#DC2626", testid: "nav-runtime-redeye" },
];

// REDEYE was promoted from advisor sidecar to a full seat (2026-02-11).
// Empty for now; if you re-introduce true advisor sidecars later, they
// go here.
const ADVISORS = [];

export default function Layout() {
  const { user, logout } = useAuth();
  const loc = useLocation();

  return (
    <div className="min-h-screen bg-rd-bg text-rd-text app-shell" data-testid="app-shell">
      {/* Observation banner */}
      <div
        className="bg-rd-warn text-black font-mono text-[10px] uppercase font-bold text-center py-1.5 tracking-[0.3em]"
        data-testid="observation-banner"
      >
        OBSERVATION ONLY · BROKER_LIVE_ORDER_ENABLED=false · execution authority
        disabled across all runtimes
      </div>

      <div className="grid grid-cols-12 min-h-[calc(100vh-26px)]">
        {/* Sidebar */}
        <aside className="col-span-12 md:col-span-3 lg:col-span-2 border-r border-rd-border bg-rd-bg2 flex flex-col">
          <div className="px-5 py-5 border-b border-rd-border flex items-center gap-2">
            <span className="inline-block w-3 h-3 bg-rd-warn pulse-dot" />
            <div>
              <div className="font-display text-sm font-black tracking-tighter">
                RISEDUAL
              </div>
              <div className="label-eyebrow">mission control</div>
            </div>
          </div>

          <nav className="px-3 py-4 flex-1 overflow-auto" data-testid="primary-nav">
            <div className="label-eyebrow px-2 mb-2">Shared</div>
            <ul className="space-y-px">
              {NAV.map((n) => (
                <li key={n.to}>
                  <NavLink
                    to={n.to}
                    end={n.end}
                    data-testid={n.testid}
                    className={({ isActive }) =>
                      `flex items-center gap-2 px-3 py-2 text-xs uppercase tracking-widest font-bold transition-colors ${
                        isActive
                          ? "bg-rd-bg3 text-rd-text border-l-2 border-rd-warn"
                          : "text-rd-muted hover:text-rd-text hover:bg-rd-bg3 border-l-2 border-transparent"
                      }`
                    }
                  >
                    <n.icon size={14} weight="bold" />
                    {n.label}
                  </NavLink>
                </li>
              ))}
            </ul>

            <div className="label-eyebrow px-2 mt-6 mb-2">Runtimes</div>
            <ul className="space-y-px">
              {RUNTIMES.map((r) => (
                <li key={r.to}>
                  <NavLink
                    to={r.to}
                    data-testid={r.testid}
                    className={({ isActive }) =>
                      `flex items-center gap-2 px-3 py-2 text-xs uppercase tracking-widest font-bold transition-colors ${
                        isActive
                          ? "bg-rd-bg3 text-rd-text"
                          : "text-rd-muted hover:text-rd-text hover:bg-rd-bg3"
                      }`
                    }
                    style={{
                      borderLeft: `2px solid ${
                        loc.pathname === r.to ? r.color : "transparent"
                      }`,
                    }}
                  >
                    <span
                      className="inline-block w-2 h-2"
                      style={{ background: r.color }}
                    />
                    {r.label}
                  </NavLink>
                </li>
              ))}
            </ul>

            {ADVISORS.length > 0 && (
              <>
                <div className="label-eyebrow px-2 mt-6 mb-2">Advisors</div>
                <ul className="space-y-px">
                  {ADVISORS.map((a) => (
                    <li key={a.to}>
                      <NavLink
                        to={a.to}
                        data-testid={a.testid}
                        className={({ isActive }) =>
                          `flex items-start gap-2 px-3 py-2 text-xs uppercase tracking-widest font-bold transition-colors ${
                            isActive
                              ? "bg-rd-bg3 text-rd-text"
                              : "text-rd-muted hover:text-rd-text hover:bg-rd-bg3"
                          }`
                        }
                        style={{
                          borderLeft: `2px solid ${
                            loc.pathname === a.to ? a.color : "transparent"
                          }`,
                        }}
                      >
                        <span
                          className="inline-block w-2 h-2 mt-[5px]"
                          style={{ background: a.color }}
                        />
                        <span className="flex flex-col leading-tight">
                          <span>{a.label}</span>
                          <span className="text-[9px] text-rd-dim font-normal normal-case tracking-normal">
                            → {a.reportsTo}
                          </span>
                        </span>
                      </NavLink>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </nav>

          <div className="border-t border-rd-border px-3 py-3">
            <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-1">
              Operator
            </div>
            <div className="text-xs font-mono text-rd-text mb-3 truncate">
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
        </aside>

        {/* Main */}
        <main className="col-span-12 md:col-span-9 lg:col-span-10 p-4 md:p-6 overflow-x-hidden">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
