import React, { useEffect, useState, useCallback } from "react";
import { api, RUNTIME_META, fmtTime, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";
import { CheckCircle, XCircle, ShieldCheck, Gavel, Lock } from "@phosphor-icons/react";

const LADDER = ["observer", "challenger", "advisor", "co_trader", "primary"];
const LADDER_META = {
  observer:   { title: "Observer",   note: "watches only" },
  challenger: { title: "Challenger", note: "can recommend veto / reduce / watch" },
  advisor:    { title: "Advisor",    note: "can influence sizing" },
  co_trader:  { title: "Co-Trader",  note: "can propose executable trades" },
  primary:    { title: "Primary",    note: "execution leader" },
};

export default function Promotion() {
  const [states, setStates] = useState(null);
  const [proposals, setProposals] = useState(null);
  const [artifacts, setArtifacts] = useState(null);
  const [readiness, setReadiness] = useState({});
  const [busy, setBusy] = useState(null);
  const [err, setErr] = useState("");
  const [info, setInfo] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [s, p, a] = await Promise.all([
        api.get("/admin/promotion/state"),
        api.get("/admin/promotion/proposals?limit=100"),
        api.get("/admin/promotion/artifacts?limit=20"),
      ]);
      setStates(s.data);
      setProposals(p.data);
      setArtifacts(a.data);
      const r = {};
      for (const item of s.data.items) {
        if (item.authority_state === "governor") continue;
        try {
          const rd = await api.get(`/admin/promotion/readiness/${item.runtime}`);
          r[item.runtime] = rd.data;
        } catch {
          /* ignore */
        }
      }
      setReadiness(r);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  async function propose(runtime) {
    setBusy(`propose-${runtime}`);
    setErr(""); setInfo("");
    try {
      const { data } = await api.post(`/admin/promotion/propose?runtime=${runtime}`);
      setInfo(`Proposal ${data.proposal_id.slice(0, 8)}… created · readiness ${data.readiness_passed ? "PASSED" : "FAILED"}`);
      await refresh();
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally { setBusy(null); }
  }

  async function decide(proposalId, action) {
    const note = window.prompt(`Optional note for ${action}:`, "") ?? "";
    setBusy(`${action}-${proposalId}`);
    setErr(""); setInfo("");
    try {
      const { data } = await api.post(`/admin/promotion/${proposalId}/${action}`, { note });
      if (action === "countersign") {
        setInfo(`Elevated ${data.from_state} → ${data.to_state}`);
      } else {
        setInfo("Proposal rejected");
      }
      await refresh();
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally { setBusy(null); }
  }

  return (
    <div className="reveal" data-testid="promotion-page">
      <PageHeader
        eyebrow="Admin · Promotion (Patent G + Patent J)"
        title="Governed authority elevation"
        sub="Roles can evolve — but only through measured evidence and operator countersign. A runtime cannot promote itself. Patent J readiness must pass; the operator must sign. The countersign cannot bypass a failed gate."
        right={<Badge color="#FBBF24"><Lock size={11} weight="bold" className="inline mr-1 -mt-0.5" />OBSERVATION</Badge>}
        testid="promotion-header"
      />

      {err && <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono">{err}</div>}
      {info && <div className="border border-rd-chevelle text-rd-chevelle px-3 py-2 mb-4 text-xs font-mono">{info}</div>}

      {!states && <LoadingRow />}

      {states && (
        <>
          {/* Ladder per runtime */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 md:gap-6 mb-6" data-testid="authority-ladders">
            {states.items.map((item) => {
              const meta = RUNTIME_META[item.runtime];
              const isGov = item.authority_state === "governor";
              const myIdx = LADDER.indexOf(item.authority_state);
              const r = readiness[item.runtime];
              return (
                <Card key={item.runtime} accentColor={meta.color} testid={`authority-card-${item.runtime}`}>
                  <div className="flex items-baseline justify-between mb-3">
                    <div>
                      <div className="font-display text-xl font-black tracking-tighter" style={{ color: meta.color }}>
                        {meta.label}
                      </div>
                      <div className="label-eyebrow mt-1">{meta.roleTitle} · {meta.roleTagline}</div>
                    </div>
                    <Badge color={isGov ? "#A1A1AA" : meta.color}>
                      {(LADDER_META[item.authority_state]?.title || item.authority_state).toUpperCase()}
                    </Badge>
                  </div>

                  {isGov ? (
                    <div className="border border-rd-border p-3 mb-3 text-[11px] font-mono text-rd-muted leading-relaxed">
                      <Gavel size={14} weight="bold" className="inline mr-1 -mt-0.5 text-rd-chevelle" />
                      Off-ladder. Governor authority is not promotable to a trading
                      authority. Chevelle holds the keys; it doesn't reach for them.
                    </div>
                  ) : (
                    <div className="space-y-1 mb-4">
                      {LADDER.map((s, i) => {
                        const reached = i <= myIdx;
                        const here = i === myIdx;
                        return (
                          <div key={s} className="flex items-center gap-2 text-[11px] font-mono"
                               data-testid={`ladder-${item.runtime}-${s}`}>
                            <span className="inline-block w-3 h-3 border" style={{
                              background: reached ? meta.color : "transparent",
                              borderColor: reached ? meta.color : "#3f3f46",
                            }} />
                            <span className={here ? "text-rd-text font-bold" : reached ? "text-rd-muted" : "text-rd-dim"}>
                              {LADDER_META[s].title}
                            </span>
                            <span className="text-rd-dim ml-auto">{LADDER_META[s].note}</span>
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {!isGov && r && (
                    <div className="border border-rd-border p-3 mb-3" data-testid={`readiness-${item.runtime}`}>
                      <div className="flex items-center justify-between mb-2">
                        <span className="label-eyebrow">Readiness → {r.target_authority || "—"}</span>
                        <Badge color={r.passed ? "#10B981" : "#EF4444"}>
                          {r.passed ? "PATENT J PASS" : "PATENT J FAIL"}
                        </Badge>
                      </div>
                      <div className="space-y-0.5">
                        {(r.checks || []).map((c) => (
                          <div key={c.name} className="flex items-center gap-2 text-[10px] font-mono">
                            {c.pass ? (
                              <CheckCircle size={11} weight="fill" className="text-rd-chevelle shrink-0" />
                            ) : (
                              <XCircle size={11} weight="fill" className="text-rd-danger shrink-0" />
                            )}
                            <span className={c.pass ? "text-rd-muted" : "text-rd-danger"}>{c.name}</span>
                            <span className="text-rd-dim ml-auto truncate">{String(c.observed)} / {c.threshold}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {!isGov && (
                    <div className="flex items-center gap-2 mb-2">
                      <button
                        onClick={() => propose(item.runtime)}
                        disabled={busy === `propose-${item.runtime}`}
                        className="btn-sharp px-3 py-2 border border-rd-border hover:border-rd-warn text-rd-text disabled:opacity-50"
                        data-testid={`propose-${item.runtime}`}
                      >
                        {busy === `propose-${item.runtime}` ? "…" : "Propose elevation"}
                      </button>
                      <span className="text-[10px] text-rd-dim uppercase tracking-widest">
                        from latest artifact
                      </span>
                    </div>
                  )}

                  <div className="text-[10px] text-rd-dim uppercase tracking-widest mt-2">
                    {item.history?.length || 0} authority change{(item.history?.length || 0) === 1 ? "" : "s"} on record
                  </div>
                </Card>
              );
            })}
          </div>

          {/* Pending proposals */}
          <Card className="p-0 overflow-hidden mb-6" testid="pending-proposals">
            <div className="px-4 py-3 border-b border-rd-border flex items-center justify-between">
              <div>
                <div className="label-eyebrow">Pending proposals</div>
                <div className="font-mono text-sm">awaiting operator countersign</div>
              </div>
            </div>
            {!proposals && <LoadingRow />}
            {proposals && (
              <ProposalsTable
                items={proposals.items.filter((p) => p.status === "pending")}
                onCountersign={(id) => decide(id, "countersign")}
                onReject={(id) => decide(id, "reject")}
                busy={busy}
                emptyMessage="No proposals awaiting decision."
              />
            )}
          </Card>

          {/* History */}
          <Card className="p-0 overflow-hidden mb-6" testid="proposal-history">
            <div className="px-4 py-3 border-b border-rd-border">
              <div className="label-eyebrow">Decision history</div>
              <div className="font-mono text-sm">approved + rejected</div>
            </div>
            {proposals && (
              <ProposalsTable
                items={proposals.items.filter((p) => p.status !== "pending")}
                emptyMessage="No decided proposals yet."
                showDecision
              />
            )}
          </Card>

          {/* Recent artifacts */}
          <Card testid="promotion-artifacts">
            <div className="label-eyebrow mb-3">Recent PromotionArtifacts (Patent G)</div>
            {artifacts && artifacts.items.length === 0 && (
              <EmptyState message="No artifacts emitted yet." />
            )}
            {artifacts && artifacts.items.length > 0 && (
              <div className="space-y-2">
                {artifacts.items.slice(0, 8).map((a) => {
                  const meta = RUNTIME_META[a.runtime];
                  return (
                    <div key={a.artifact_id} className="border border-rd-border p-3" data-testid={`artifact-${a.artifact_id}`}>
                      <div className="flex items-center justify-between mb-2">
                        <div className="flex items-center gap-2">
                          <span style={{ color: meta?.color }} className="font-bold">{meta?.label}</span>
                          <span className="text-rd-dim">→</span>
                          <Badge color="#A1A1AA">{a.target_authority.toUpperCase()}</Badge>
                        </div>
                        <span className="text-[10px] font-mono text-rd-dim">{relTime(a.emitted_at)}</span>
                      </div>
                      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-[11px] font-mono">
                        <Metric k="ECE" v={a.metrics?.ece} />
                        <Metric k="Brier" v={a.metrics?.brier} />
                        <Metric k="rows" v={a.metrics?.resolved_rows} />
                        <Metric k="dis. stab." v={a.metrics?.disagreement_stability} />
                        <Metric k="audit ✓" v={a.metrics?.audit_integrity_pass ? "true" : "false"} />
                      </div>
                      {a.notes && <div className="text-[10px] text-rd-muted mt-2 font-mono">{a.notes}</div>}
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}

function ProposalsTable({ items, onCountersign, onReject, busy, emptyMessage, showDecision }) {
  if (!items.length) return <EmptyState message={emptyMessage} />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs font-mono">
        <thead>
          <tr className="bg-rd-bg3 text-rd-dim uppercase tracking-widest">
            <th className="text-left px-4 py-3 border-b border-rd-border">Created</th>
            <th className="text-left px-4 py-3 border-b border-rd-border">Runtime</th>
            <th className="text-left px-4 py-3 border-b border-rd-border">From → Target</th>
            <th className="text-left px-4 py-3 border-b border-rd-border">Patent J</th>
            <th className="text-left px-4 py-3 border-b border-rd-border">{showDecision ? "Decision" : "Action"}</th>
          </tr>
        </thead>
        <tbody>
          {items.map((p) => {
            const meta = RUNTIME_META[p.runtime];
            return (
              <tr key={p.proposal_id} className="border-b border-rd-border last:border-b-0 hover:bg-rd-bg3"
                  data-testid={`proposal-row-${p.proposal_id}`}>
                <td className="px-4 py-2.5 text-rd-muted whitespace-nowrap">{fmtTime(p.created_at)}</td>
                <td className="px-4 py-2.5">
                  <span style={{ color: meta?.color }} className="font-bold">{meta?.label}</span>
                </td>
                <td className="px-4 py-2.5">{p.from_state} → <span className="text-rd-text font-bold">{p.target_authority}</span></td>
                <td className="px-4 py-2.5">
                  <Badge color={p.readiness?.passed ? "#10B981" : "#EF4444"}>
                    {p.readiness?.passed ? "PASS" : "FAIL"}
                  </Badge>
                </td>
                <td className="px-4 py-2.5">
                  {showDecision ? (
                    <span className={p.status === "approved" ? "text-rd-chevelle" : "text-rd-danger"}>
                      {p.status.toUpperCase()} · {p.decided_by || "—"}
                    </span>
                  ) : (
                    <div className="flex gap-2">
                      <button
                        disabled={!p.readiness?.passed || busy === `countersign-${p.proposal_id}`}
                        onClick={() => onCountersign(p.proposal_id)}
                        className="btn-sharp px-3 py-1.5 border border-rd-chevelle text-rd-chevelle hover:bg-rd-chevelle hover:text-black disabled:opacity-40 disabled:cursor-not-allowed"
                        data-testid={`countersign-${p.proposal_id}`}
                        title={p.readiness?.passed ? "" : "Patent J gate has not passed"}
                      >
                        <ShieldCheck size={12} weight="bold" className="inline mr-1" />
                        Countersign
                      </button>
                      <button
                        disabled={busy === `reject-${p.proposal_id}`}
                        onClick={() => onReject(p.proposal_id)}
                        className="btn-sharp px-3 py-1.5 border border-rd-border text-rd-muted hover:border-rd-danger hover:text-rd-danger"
                        data-testid={`reject-${p.proposal_id}`}
                      >
                        Reject
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Metric({ k, v }) {
  return (
    <div className="flex items-baseline justify-between border border-rd-border px-2 py-1.5">
      <span className="text-[9px] uppercase tracking-widest text-rd-dim">{k}</span>
      <span className="text-rd-text">{v ?? "—"}</span>
    </div>
  );
}
