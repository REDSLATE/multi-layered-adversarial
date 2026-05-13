import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { PageHeader, Card, Badge } from "@/components/ui-bits";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toast } from "sonner";
import {
  MagnifyingGlass, Lightning, Warning, Skull, Trash, ArrowsClockwise,
  Eye, TrendUp, CaretDown, CaretUp, ShieldWarning, Sparkle, Brain,
  ChatCircleDots, Archive, Pulse, X,
} from "@phosphor-icons/react";

// ── client-side ticker cache (operator-spec: 30-min TTL) ────────────
const CACHE_TTL_MS = 30 * 60 * 1000;
const TICKER_CACHE = new Map();

function cacheGet(symbol) {
  const row = TICKER_CACHE.get(symbol);
  if (!row) return null;
  if (Date.now() > row.expiresAt) { TICKER_CACHE.delete(symbol); return null; }
  return row;
}
function cachePut(symbol, result) {
  TICKER_CACHE.set(symbol, { result, expiresAt: Date.now() + CACHE_TTL_MS });
}

const BRAIN_COLOR = {
  alpha: "#3B82F6", camaro: "#F59E0B",
  chevelle: "#10B981", redeye: "#DC2626",
};
const BRAIN_LABEL = {
  alpha: "ALPHA", camaro: "CAMARO",
  chevelle: "CHEVELLE", redeye: "REDEYE",
};

const ACTION_COLOR = {
  BUY: "#10B981", SELL: "#DC2626", SHORT: "#DC2626",
  COVER: "#10B981", HOLD: "#A1A1AA",
};

const LABEL_COLOR = {
  safe: "#10B981", review: "#F59E0B", quarantine: "#DC2626",
};

function fmtTime(iso) {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

function fmtPct(v) {
  if (v == null || isNaN(v)) return "—";
  return `${Math.round(Number(v) * 100)}%`;
}

// ── reusable collapsible section ────────────────────────────────────

function Section({ title, icon: Icon, color, children, count, defaultOpen = true, testid }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border-b border-rd-border last:border-b-0" data-testid={testid}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-baseline justify-between py-3 hover:bg-rd-bg/40 px-2 -mx-2"
      >
        <div className="flex items-baseline gap-2">
          {Icon && <Icon size={12} weight="bold" style={{ color }} />}
          <span className="text-[10px] font-mono uppercase tracking-[0.22em]" style={{ color }}>
            {title}
          </span>
          {count != null && (
            <span className="text-[9px] font-mono text-rd-dim">· {count}</span>
          )}
        </div>
        {open ? <CaretUp size={11} weight="bold" /> : <CaretDown size={11} weight="bold" />}
      </button>
      {open && <div className="pb-3 px-1">{children}</div>}
    </div>
  );
}

// ── per-section renderers ───────────────────────────────────────────

function LatestIntent({ intent }) {
  if (!intent) {
    return <div className="text-[11px] font-mono text-rd-dim italic">No recent intent on this symbol.</div>;
  }
  const color = ACTION_COLOR[intent.action] || "#A1A1AA";
  return (
    <div className="space-y-2">
      <div className="flex items-baseline gap-4">
        <div>
          <div className="text-[9px] uppercase tracking-widest text-rd-dim">Action</div>
          <div className="text-2xl font-black tracking-tight" style={{ color }}>
            {intent.action}
          </div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-widest text-rd-dim">Confidence</div>
          <div className="text-2xl font-black tracking-tight text-rd-text">
            {fmtPct(intent.confidence)}
          </div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-widest text-rd-dim">Gate</div>
          <div className="text-xs font-mono uppercase text-rd-text mt-1">
            {(intent.gate_state || "pending").replace(/_/g, " ")}
          </div>
        </div>
        {intent.executed && (
          <div>
            <div className="text-[9px] uppercase tracking-widest text-rd-dim">Status</div>
            <div className="text-xs font-mono text-emerald-400 mt-1">EXECUTED</div>
          </div>
        )}
      </div>
      <div className="text-sm text-rd-text leading-relaxed">
        {intent.rationale || <span className="text-rd-dim italic">no rationale text</span>}
      </div>
      <div className="text-[10px] font-mono text-rd-muted">
        posted {fmtTime(intent.ingest_ts)}
      </div>
      {intent.evidence && Object.keys(intent.evidence).length > 0 && (
        <details className="text-[10px] font-mono text-rd-muted">
          <summary className="cursor-pointer hover:text-rd-text">evidence ({Object.keys(intent.evidence).length} keys)</summary>
          <pre className="mt-1 p-2 bg-rd-bg border border-rd-border whitespace-pre-wrap overflow-x-auto text-[10px]">
            {JSON.stringify(intent.evidence, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}

function LatestOpinion({ opinion }) {
  if (!opinion) {
    return <div className="text-[11px] font-mono text-rd-dim italic">No discussion stance on this symbol.</div>;
  }
  return (
    <div className="space-y-2">
      <div className="flex items-baseline gap-2 text-[10px] font-mono">
        <span className="uppercase tracking-widest text-rd-dim">Stance</span>
        <span className="uppercase text-rd-text">{opinion.stance}</span>
        <span className="text-rd-dim">·</span>
        <span className="text-rd-text">{fmtPct(opinion.confidence)} conf</span>
      </div>
      <div className="text-sm text-rd-text leading-relaxed">
        {opinion.body}
      </div>
      <div className="text-[10px] font-mono text-rd-muted">posted {fmtTime(opinion.posted_at)}</div>
    </div>
  );
}

function ShellyMemories({ memories, brain }) {
  if (!memories || memories.length === 0) {
    return (
      <div className="text-[11px] font-mono text-rd-dim italic">
        No Shelly memories from {BRAIN_LABEL[brain] || brain} mention this symbol.
      </div>
    );
  }
  return (
    <ul className="space-y-2">
      {memories.map((m, i) => (
        <li
          key={m.id || i}
          className="flex items-start gap-2 text-[11px] leading-relaxed"
          data-testid={`shelly-memory-${i}`}
        >
          <Badge color={LABEL_COLOR[m.label] || "#A1A1AA"}>
            {(m.label || "—").toUpperCase()}
          </Badge>
          <div className="flex-1 min-w-0">
            <div className="font-mono text-rd-text break-words">{m.payload_summary || "—"}</div>
            <div className="text-[10px] font-mono text-rd-muted">
              {m.reason} · {fmtTime(m.timestamp)}
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}

function TrackRecord({ track }) {
  const total = (track?.wins || 0) + (track?.losses || 0);
  if (!total && !(track?.items?.length)) {
    return <div className="text-[11px] font-mono text-rd-dim italic">No resolved outcomes yet.</div>;
  }
  return (
    <div className="space-y-2">
      <div className="flex items-baseline gap-3 text-[11px] font-mono">
        <span className="text-emerald-400">{track.wins}W</span>
        <span className="text-rose-400">{track.losses}L</span>
        {track.open > 0 && <span className="text-amber-400">{track.open} unresolved</span>}
        {total > 0 && (
          <span className="text-rd-text">
            · {Math.round((track.wins / total) * 100)}% hit rate
          </span>
        )}
      </div>
      {track.items?.length > 0 && (
        <ul className="space-y-1.5 mt-2">
          {track.items.map((it, i) => (
            <li key={it.opinion_id || i} className="text-[11px] font-mono flex items-baseline gap-2" data-testid={`track-item-${i}`}>
              <span style={{
                color:
                  ["win", "correct", "good"].includes((it.outcome || "").toLowerCase()) ? "#10B981" :
                  ["loss", "wrong", "bad"].includes((it.outcome || "").toLowerCase()) ? "#DC2626" : "#A1A1AA",
              }}>
                {(it.outcome || "—").toUpperCase()}
              </span>
              <span className="text-rd-muted">{fmtTime(it.resolved_at)}</span>
              {it.rationale && <span className="text-rd-text truncate">— {it.rationale}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SimilarSetups({ setups }) {
  if (!setups || setups.length === 0) {
    return <div className="text-[11px] font-mono text-rd-dim italic">No prior plays in this regime.</div>;
  }
  return (
    <ul className="space-y-1.5">
      {setups.map((s, i) => (
        <li
          key={s.intent_id || i}
          className="text-[11px] font-mono flex items-baseline gap-2"
          data-testid={`similar-setup-${i}`}
        >
          <span style={{ color: ACTION_COLOR[s.action] || "#A1A1AA" }}>{s.action}</span>
          <span className="text-rd-text">{s.symbol}</span>
          <span className="text-rd-dim">@ {fmtPct(s.confidence)}</span>
          <span className="text-rd-muted">· {fmtTime(s.executed_at)}</span>
          {s.rationale && <span className="text-rd-text truncate">— {s.rationale}</span>}
        </li>
      ))}
    </ul>
  );
}

// ── role cards ──────────────────────────────────────────────────────

function RoleCard({ role, payload, accent, RoleIcon, eyebrow, testid }) {
  const brain = payload?.brain;
  return (
    <Card className="border-l-2" accentColor={accent} testid={testid}>
      <div className="flex items-baseline justify-between mb-3">
        <div className="flex items-baseline gap-2">
          <RoleIcon size={13} weight="bold" style={{ color: accent }} />
          <span className="label-eyebrow" style={{ color: accent }}>{eyebrow}</span>
          {brain ? (
            <Badge color={BRAIN_COLOR[brain] || "#A1A1AA"} testid={`${role}-brain`}>
              {BRAIN_LABEL[brain] || brain}
            </Badge>
          ) : (
            <Badge color="#71717A" testid={`${role}-brain`}>SEAT EMPTY</Badge>
          )}
        </div>
      </div>

      {payload?.summary && (
        <div className="text-[11px] font-mono text-rd-muted mb-3 px-1" data-testid={`${role}-summary`}>
          {payload.summary}
        </div>
      )}

      {payload?.seat_empty ? (
        <div className="border border-rd-border bg-rd-bg p-3 text-[11px] font-mono text-rd-dim">
          Rotate a brain into the {eyebrow.toLowerCase()} seat from the Intents page to surface its analysis here.
        </div>
      ) : (
        <>
          <Section
            title="Latest Intent"
            icon={Lightning}
            color={accent}
            testid={`${role}-latest-intent`}
          >
            <LatestIntent intent={payload.latest_intent} />
          </Section>
          <Section
            title="Discussion Stance"
            icon={ChatCircleDots}
            color={accent}
            testid={`${role}-discussion`}
          >
            <LatestOpinion opinion={payload.latest_opinion} />
          </Section>
          <Section
            title="Shelly Memories"
            icon={Brain}
            color={accent}
            count={payload.shelly_memories?.length || 0}
            testid={`${role}-shelly`}
          >
            <ShellyMemories memories={payload.shelly_memories} brain={brain} />
          </Section>
          <Section
            title="Track Record"
            icon={TrendUp}
            color={accent}
            testid={`${role}-track-record`}
          >
            <TrackRecord track={payload.track_record} />
          </Section>
          <Section
            title="Similar Past Setups (same regime)"
            icon={Archive}
            color={accent}
            count={payload.similar_setups?.length || 0}
            testid={`${role}-similar`}
            defaultOpen={false}
          >
            <SimilarSetups setups={payload.similar_setups} />
          </Section>
        </>
      )}
    </Card>
  );
}

// ── page ─────────────────────────────────────────────────────────────

export default function Hypothesis() {
  const [symbol, setSymbol] = useState("");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [cached, setCached] = useState(false);
  const inputRef = useRef(null);

  const cacheStats = useMemo(() => {
    const now = Date.now();
    let live = 0;
    for (const [, v] of TICKER_CACHE) if (v.expiresAt > now) live += 1;
    return { live };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result]);

  const runAnalyze = useCallback(async (sym) => {
    const s = (sym || "").trim().toUpperCase();
    if (!s) { setErr("Enter a ticker symbol"); return; }
    setErr("");
    const hit = cacheGet(s);
    if (hit) {
      setResult({ ...hit.result, _from_cache: true, _expires_at: hit.expiresAt });
      setCached(true);
      return;
    }
    setLoading(true);
    setCached(false);
    try {
      const res = await api.post("/hypothesis/analyze", { symbol: s });
      cachePut(s, res.data);
      setResult({ ...res.data, _from_cache: false });
    } catch (e) {
      setErr(e?.message || "Analysis failed");
      toast.error(e?.message || "Analysis failed");
    } finally {
      setLoading(false);
    }
  }, []);

  const onSubmit = (e) => { e.preventDefault(); runAnalyze(symbol); };
  const clearAll = () => {
    setSymbol(""); setResult(null); setErr(""); setCached(false);
    inputRef.current?.focus();
  };

  useEffect(() => { inputRef.current?.focus(); }, []);

  const regimeFp = result?.context?.regime_fp || {};
  const regimeStr = Object.entries(regimeFp).map(([k, v]) => `${k}=${v}`).join(" · ");

  return (
    <div className="space-y-6" data-testid="hypothesis-page">
      <PageHeader
        eyebrow="Brain Recall"
        title="AI Investment Hypothesis"
        sub="Strategist (Executor seat) and Auditor (Auditor seat) each surface their own intents, opinions, and Shelly-gated memories on this symbol. No external AIs — pure brain content."
        right={
          <div className="flex items-baseline gap-3 text-[10px] font-mono uppercase tracking-widest text-rd-dim">
            <span>cache · <span className="text-rd-text">{cacheStats.live}</span> tickers · ttl 30m</span>
          </div>
        }
        testid="hypothesis-header"
      />

      <Card testid="hypothesis-search-card">
        <form onSubmit={onSubmit} className="flex items-stretch gap-2">
          <div className="relative flex-1">
            <MagnifyingGlass size={14} weight="bold" className="absolute left-3 top-1/2 -translate-y-1/2 text-rd-dim" />
            <Input
              ref={inputRef}
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              placeholder="Ticker (e.g. NVDA, AAPL, TSLA)"
              data-testid="hypothesis-symbol-input"
              className="pl-9 h-11 font-mono text-base tracking-wide bg-rd-bg border-rd-border uppercase"
              autoComplete="off"
              maxLength={10}
              disabled={loading}
            />
          </div>
          <Button
            type="submit"
            disabled={loading || !symbol.trim()}
            data-testid="hypothesis-analyze-btn"
            className="h-11 px-6 bg-emerald-500 hover:bg-emerald-400 text-black font-mono uppercase tracking-wider text-xs"
          >
            {loading ? (<><ArrowsClockwise size={14} weight="bold" className="animate-spin mr-2" /> Loading</>)
                     : (<><Lightning size={14} weight="bold" className="mr-2" /> Analyze</>)}
          </Button>
          {(result || symbol) && (
            <Button
              type="button"
              variant="outline"
              onClick={clearAll}
              disabled={loading}
              data-testid="hypothesis-clear-btn"
              className="h-11 px-4 font-mono uppercase tracking-wider text-xs"
            >
              <Trash size={12} weight="bold" className="mr-1.5" /> Clear
            </Button>
          )}
        </form>
        {err && (
          <div className="mt-3 border border-rd-danger text-rd-danger px-3 py-2 text-[11px] font-mono" data-testid="hypothesis-error">{err}</div>
        )}
        {result && (
          <div className="mt-3 flex items-baseline gap-3 text-[10px] font-mono uppercase tracking-widest text-rd-dim flex-wrap">
            <Eye size={11} weight="bold" />
            <span>analysis · <span className="text-rd-text">{result.symbol}</span></span>
            <span>·</span>
            <span>generated {new Date(result.generated_at).toLocaleTimeString()}</span>
            {regimeStr && (
              <>
                <span>·</span>
                <Pulse size={11} weight="bold" className="text-emerald-400" />
                <span className="text-rd-text">regime {regimeStr}</span>
              </>
            )}
            {cached && (
              <>
                <span>·</span>
                <span className="text-amber-400" data-testid="hypothesis-cached-tag">CACHED</span>
                <span className="text-rd-muted">
                  · expires in {Math.max(0, Math.round((result._expires_at - Date.now()) / 60000))}m
                </span>
              </>
            )}
          </div>
        )}
      </Card>

      {result && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4" data-testid="hypothesis-result">
          <RoleCard
            role="strategist"
            payload={result.strategist}
            accent="#10B981"
            RoleIcon={Sparkle}
            eyebrow="Strategist"
            testid="strategist-card"
          />
          <RoleCard
            role="auditor"
            payload={result.auditor}
            accent="#DC2626"
            RoleIcon={ShieldWarning}
            eyebrow="Auditor"
            testid="auditor-card"
          />
        </div>
      )}

      {result && result.context?.indicator_snapshot && (
        <Card testid="hypothesis-context-card">
          <div className="label-eyebrow mb-2">Anchored Context</div>
          <div className="text-[11px] font-mono text-rd-muted leading-relaxed">
            Latest snapshot:{" "}
            <span className="text-rd-text">
              {result.context.indicator_snapshot.symbol} · {result.context.indicator_snapshot.tf} · {result.context.indicator_snapshot.source}
            </span>
            {result.context.indicator_snapshot.indicators?.last_close != null && (
              <> · last close ${result.context.indicator_snapshot.indicators.last_close.toFixed(2)}</>
            )}
            <div className="mt-1">
              · open positions on {result.symbol}: {result.context.open_positions?.length || 0}
            </div>
          </div>
        </Card>
      )}

      {result && !result.context?.indicator_snapshot && (
        <Card testid="hypothesis-no-context-card">
          <div className="flex items-start gap-2 text-[11px] font-mono text-amber-400">
            <Warning size={13} weight="bold" />
            <div>
              No live indicator snapshot for {result.symbol}. Similar-setup recall is disabled until a brain publishes a snapshot.
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
