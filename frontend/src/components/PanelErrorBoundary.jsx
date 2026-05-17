import React from "react";
import { Warning } from "@phosphor-icons/react";

/**
 * PanelErrorBoundary — generic render-crash isolator.
 *
 * Wrap any panel that pulls live backend data so one render crash
 * doesn't blank the whole page. The pattern is intentionally the same
 * as the existing `BrokerTileErrorBoundary` in `KrakenBrokerTile.jsx`
 * (which fixed the original Kraken blank-screen) — promoted here for
 * reuse across every page.
 *
 * Props:
 *   panelName  — label shown on the error chip (e.g., "Brain Roster")
 *   testid     — root testid for the crashed state; defaults to a
 *                slug derived from panelName
 *   compact    — when true, renders a one-row strip instead of a card
 *   children   — the panel to render normally
 *
 * The boundary catches:
 *   • render errors (getDerivedStateFromError)
 *   • lifecycle errors (componentDidCatch)
 * It does NOT catch:
 *   • async errors inside event handlers (those still need try/catch)
 *   • errors in useEffect callbacks (caught by React but logged only)
 */
export default class PanelErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { err: null };
  }

  static getDerivedStateFromError(err) {
    return { err };
  }

  componentDidCatch(err, info) {
     
    console.error(`[PanelErrorBoundary:${this.props.panelName}]`, err, info);
  }

  reset = () => this.setState({ err: null });

  render() {
    if (this.state.err) {
      const panelName = this.props.panelName || "Panel";
      const testid = this.props.testid
        || `panel-error-${panelName.toLowerCase().replace(/\s+/g, "-")}`;
      const message = String(
        this.state.err?.message || this.state.err || "render error",
      );
      const compact = !!this.props.compact;
      return (
        <div
          className={
            compact
              ? "border border-rd-danger px-3 py-2 mb-3 flex items-center gap-2"
              : "border border-rd-danger bg-rd-bg p-3 mb-4"
          }
          data-testid={testid}
        >
          <Warning size={12} weight="bold" className="text-rd-danger shrink-0" />
          <div className="flex-1 min-w-0">
            <div className="font-mono text-[10px] uppercase tracking-widest text-rd-danger">
              {panelName} · render error
            </div>
            {!compact && (
              <div className="mt-1 text-[10px] font-mono text-rd-dim leading-relaxed">
                This panel failed to render. The rest of the page is
                unaffected.
                <span className="block mt-1 text-rd-danger break-all">
                  {message}
                </span>
              </div>
            )}
            {compact && (
              <span className="ml-2 text-[10px] font-mono text-rd-dim truncate">
                {message}
              </span>
            )}
          </div>
          <button
            onClick={this.reset}
            data-testid={`${testid}-retry`}
            className="ml-2 px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text"
          >
            retry
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
