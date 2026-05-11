import React, { useCallback, useEffect, useState } from "react";
import { api, relTime, fmtTime } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { Plus, Crosshair, Trophy, Prohibit, ChatCircleDots } from "@phosphor-icons/react";
import { toast } from "sonner";

const BRAIN_META = {
  alpha:    { label: "ALPHA",    color: "#3B82F6" },
  camaro:   { label: "CAMARO",   color: "#F59E0B" },
  chevelle: { label: "CHEVELLE", color: "#10B981" },
  redeye:   { label: "REDEYE",   color: "#DC2626" },
};

const STANCE_META = {
  long:    { label: "LONG",    color: "#22C55E" },
  short:   { label: "SHORT",   color: "#DC2626" },
  abstain: { label: "ABSTAIN", color: "#A1A1AA" },
};

const STATE_META = {
  proposed:        { label: "PROPOSED",        color: "#FBBF24" },
  discussing:      { label: "DISCUSSING",      color: "#3B82F6" },
  consensus_long:  { label: "→ LONG",          color: "#22C55E" },
  consensus_short: { label: "→ SHORT",         color: "#DC2626" },
  rejected:        { label: "REJECTED",        color: "#71717A" },
  stale:           { label: "STALE",           color: "#52525B" },
};

export default function Positions() {
  const [items, setItems] = useState(null);
  const [filter, setFilter] = useState("open");
  const [propOpen, setPropOpen] = useState(false);
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    setErr("");
    try {
      const params = new URLSearchParams();
      if (filter !== "all") params.set("state", filter);
      params.set("limit", "100");
      const { data } = await api.get(`/shared/positions?${params.toString()}`);
      setItems(data.items);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, [filter]);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    const id = setInterval(refresh, 15000);
    return () => clearInterval(id);
  }, [refresh]);

  return (
    <div className="reveal" data-testid="positions-page">
      <PageHeader
        eyebrow="position primitive · 4-brain debate"
        title="Positions"
        sub="A position is a discrete thesis the 4 brains argue. Every brain stamps LONG / SHORT / ABSTAIN. The brain in the executor seat (per Roster — default Alpha) makes the call. No trades fire — observation only."
        right={
          <Button
            size="sm"
            onClick={() => setPropOpen(true)}
            data-testid="propose-position-btn"
            className="bg-rd-text text-rd-bg hover:bg-rd-muted"
          >
            <Plus size={12} weight="bold" className="mr-1" /> Propose Position
          </Button>
        }
        testid="positions-header"
      />

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono">
          {err}
        </div>
      )}

      <Card className="mb-4" testid="positions-filter">
        <div className="flex items-center gap-2 text-[11px] font-mono">
          {["open", "all", "consensus_long", "consensus_short", "rejected", "stale"].map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1.5 border text-[10px] uppercase tracking-widest ${
                filter === f
                  ? "border-rd-text text-rd-text bg-rd-bg3"
                  : "border-rd-border text-rd-muted hover:text-rd-text"
              }`}
              data-testid={`filter-${f}`}
            >
              {f.replace("_", " ")}
            </button>
          ))}
        </div>
      </Card>

      {!items && <LoadingRow />}
      {items && items.length === 0 && (
        <EmptyState
          message={
            filter === "open"
              ? "No open positions. Click 'Propose Position' to start a debate."
              : `No positions in '${filter}' state.`
          }
          testid="positions-empty"
        />
      )}

      {items && items.length > 0 && (
        <div className="space-y-3" data-testid="positions-list">
          {items.map((p) => (
            <PositionCard key={p.position_id} p={p} onChange={refresh} />
          ))}
        </div>
      )}

      <ProposeDialog
        open={propOpen}
        onClose={() => setPropOpen(false)}
        onCreated={() => { setPropOpen(false); refresh(); }}
      />
    </div>
  );
}

function PositionCard({ p, onChange }) {
  const state = STATE_META[p.state] || STATE_META.proposed;
  const executor = p.executor_seat;
  const isOpen = ["proposed", "discussing"].includes(p.state);
  const [showStanceBox, setShowStanceBox] = useState(null);   // brain name

  const onStance = async (brain, stance) => {
    try {
      await api.post(`/admin/positions/${p.position_id}/stance`, {
        brain, stance, confidence: 0.7, notes: "",
      });
      toast.success(`${brain} → ${stance}`);
      setShowStanceBox(null);
      onChange();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    }
  };

  const onCall = async (direction) => {
    if (!confirm(`Record executor (${executor?.toUpperCase() || "—"})'s call as ${direction.toUpperCase()}? No trade fires.`)) return;
    try {
      await api.post(`/admin/positions/${p.position_id}/executor-call`, {
        direction, notes: "",
      });
      toast.success(`Executor call: ${direction.toUpperCase()}`);
      onChange();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    }
  };

  const onReject = async () => {
    if (!confirm("Reject this position thesis?")) return;
    try {
      await api.post(`/admin/positions/${p.position_id}/reject`, { notes: "" });
      toast.success("Position rejected");
      onChange();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    }
  };

  return (
    <Card className="p-0 overflow-hidden" testid={`position-${p.position_id}`}>
      <div className="px-4 py-3 border-b border-rd-border flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <span className="font-display text-2xl font-black tracking-tighter">
            {p.symbol}
          </span>
          {p.regime_tag && (
            <Badge color="#A1A1AA">{p.regime_tag}</Badge>
          )}
          <span className="text-[10px] text-rd-dim font-mono">
            proposed by {p.proposed_by} · {relTime(p.created_at)}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <Badge color={state.color} testid={`position-state-${p.position_id}`}>
            {state.label}
          </Badge>
          {p.direction && (
            <Badge color={STANCE_META[p.direction]?.color}>
              EXECUTOR CALL: {STANCE_META[p.direction]?.label}
            </Badge>
          )}
        </div>
      </div>

      {p.thesis && (
        <div className="px-4 py-2 text-[12px] text-rd-muted font-mono border-b border-rd-border whitespace-pre-wrap">
          {p.thesis}
        </div>
      )}

      {/* Brain stance grid */}
      <div className="grid grid-cols-1 md:grid-cols-4 divide-y md:divide-y-0 md:divide-x divide-rd-border">
        {Object.keys(BRAIN_META).map((brain) => {
          const meta = BRAIN_META[brain];
          const stance = p.stances_by_brain?.[brain];
          const stanceMeta = stance ? STANCE_META[stance.stance] : null;
          return (
            <div
              key={brain}
              className="px-4 py-3 relative"
              data-testid={`position-stance-${p.position_id}-${brain}`}
            >
              <div className="flex items-baseline gap-2 mb-1">
                <span style={{ color: meta.color }} className="font-mono font-bold text-xs">
                  {meta.label}
                </span>
                {executor === brain && (
                  <Badge color="#FBBF24">EXEC</Badge>
                )}
                {stance?.posted_as && (
                  <span className="text-[9px] text-rd-dim font-mono ml-auto">
                    as {stance.posted_as.replace("_", " ")}
                  </span>
                )}
              </div>
              {stance ? (
                <>
                  <div className="flex items-baseline gap-2">
                    <Badge color={stanceMeta.color}>{stanceMeta.label}</Badge>
                    <span className="text-[10px] text-rd-dim font-mono">
                      conf {Number(stance.confidence).toFixed(2)}
                    </span>
                  </div>
                  {stance.notes && (
                    <div className="text-[10px] text-rd-muted font-mono mt-1 line-clamp-2">
                      {stance.notes}
                    </div>
                  )}
                  <div className="text-[9px] text-rd-dim font-mono mt-1">
                    {relTime(stance.posted_at)} · via {stance.posted_via}
                  </div>
                </>
              ) : (
                <div className="text-[10px] text-rd-dim font-mono italic">
                  no stance yet
                </div>
              )}
              {isOpen && (
                <div className="mt-2 flex gap-1">
                  {["long", "short", "abstain"].map((s) => (
                    <button
                      key={s}
                      onClick={() => onStance(brain, s)}
                      className="px-1.5 py-0.5 text-[9px] font-mono uppercase border border-rd-border text-rd-muted hover:text-rd-text hover:border-rd-borderStrong"
                      data-testid={`stance-btn-${p.position_id}-${brain}-${s}`}
                    >
                      {s}
                    </button>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Executor decision row */}
      {isOpen && (
        <div className="px-4 py-2.5 bg-rd-bg2 border-t border-rd-border flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-2 text-[10px] text-rd-dim uppercase tracking-widest">
            <Crosshair size={11} weight="bold" />
            executor seat ({executor?.toUpperCase() || "VACATED"}) makes the call:
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => onCall("long")}
              disabled={!executor}
              className="btn-sharp px-3 py-1.5 border text-[11px] font-mono"
              style={{ borderColor: STANCE_META.long.color, color: STANCE_META.long.color }}
              data-testid={`call-long-${p.position_id}`}
            >
              <Trophy size={11} weight="bold" className="inline mr-1 -mt-0.5" />
              CALL LONG
            </button>
            <button
              onClick={() => onCall("short")}
              disabled={!executor}
              className="btn-sharp px-3 py-1.5 border text-[11px] font-mono"
              style={{ borderColor: STANCE_META.short.color, color: STANCE_META.short.color }}
              data-testid={`call-short-${p.position_id}`}
            >
              <Trophy size={11} weight="bold" className="inline mr-1 -mt-0.5" />
              CALL SHORT
            </button>
            <button
              onClick={onReject}
              className="btn-sharp px-3 py-1.5 border border-rd-border text-rd-muted hover:text-rd-text text-[11px] font-mono"
              data-testid={`reject-${p.position_id}`}
            >
              <Prohibit size={11} weight="bold" className="inline mr-1 -mt-0.5" />
              REJECT
            </button>
          </div>
        </div>
      )}

      {!isOpen && p.executor_call_at && (
        <div className="px-4 py-2 bg-rd-bg2 border-t border-rd-border text-[10px] text-rd-muted font-mono">
          {p.executor_call_by?.toUpperCase()} called {STANCE_META[p.direction]?.label} at{" "}
          {fmtTime(p.executor_call_at)} (recorded by {p.executor_call_recorded_by})
          {p.executor_call_notes && <> · {p.executor_call_notes}</>}
        </div>
      )}
    </Card>
  );
}

function ProposeDialog({ open, onClose, onCreated }) {
  const [symbol, setSymbol] = useState("");
  const [regimeTag, setRegimeTag] = useState("");
  const [thesis, setThesis] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    setErr("");
    if (!symbol.trim()) { setErr("symbol required"); return; }
    setSubmitting(true);
    try {
      await api.post("/shared/positions", {
        symbol: symbol.trim(),
        regime_tag: regimeTag.trim() || null,
        thesis: thesis.trim(),
        proposed_by: "operator",
      });
      toast.success(`Position proposed · ${symbol.trim().toUpperCase()}`);
      setSymbol(""); setRegimeTag(""); setThesis("");
      onCreated();
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="bg-rd-bg2 border-rd-border max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-baseline gap-2">
            <ChatCircleDots size={16} weight="bold" />
            Propose a position
          </DialogTitle>
          <DialogDescription className="text-rd-dim text-[11px] font-mono">
            Open a thesis the 4 brains will debate. No trade fires.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-sm">
          <div>
            <Label htmlFor="prop-symbol" className="text-[10px] uppercase tracking-widest text-rd-dim">
              Symbol
            </Label>
            <Input
              id="prop-symbol"
              data-testid="propose-symbol-input"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              placeholder="NVDA, BTC/USD, SPY, ..."
              className="font-mono text-xs bg-rd-bg3 border-rd-border"
              autoFocus
            />
          </div>
          <div>
            <Label htmlFor="prop-regime" className="text-[10px] uppercase tracking-widest text-rd-dim">
              Regime tag <span className="text-rd-muted">· optional</span>
            </Label>
            <Input
              id="prop-regime"
              data-testid="propose-regime-input"
              value={regimeTag}
              onChange={(e) => setRegimeTag(e.target.value)}
              placeholder="trend, chop, breakout, fade..."
              className="font-mono text-xs bg-rd-bg3 border-rd-border"
            />
          </div>
          <div>
            <Label htmlFor="prop-thesis" className="text-[10px] uppercase tracking-widest text-rd-dim">
              Thesis <span className="text-rd-muted">· what are we debating?</span>
            </Label>
            <textarea
              id="prop-thesis"
              data-testid="propose-thesis-input"
              value={thesis}
              onChange={(e) => setThesis(e.target.value)}
              rows={4}
              className="w-full font-mono text-xs bg-rd-bg3 border border-rd-border px-3 py-2 text-rd-text"
              placeholder="e.g. Earnings tomorrow, IV crush risk, but trend is up..."
            />
          </div>
          {err && (
            <div className="border border-rd-danger text-rd-danger px-3 py-2 text-[11px] font-mono">
              {err}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button
            onClick={submit}
            disabled={submitting || !symbol.trim()}
            data-testid="propose-submit-btn"
            className="bg-rd-text text-rd-bg hover:bg-rd-muted"
          >
            {submitting ? "OPENING…" : "OPEN POSITION FOR DEBATE"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
