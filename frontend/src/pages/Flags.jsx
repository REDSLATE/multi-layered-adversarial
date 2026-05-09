import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { PageHeader, Card, Badge, LoadingRow } from "@/components/ui-bits";
import { Lock } from "@phosphor-icons/react";

export default function Flags() {
  const [data, setData] = useState(null);
  useEffect(() => {
    (async () => {
      const { data } = await api.get("/admin/flags");
      setData(data);
    })();
  }, []);

  return (
    <div className="reveal" data-testid="flags-page">
      <PageHeader
        eyebrow="Admin · Runtime flags"
        title="Promotion gates & execution authority"
        sub="Flags are read-only in observation mode. Each enforce flag is owned by exactly one runtime — they cannot be flipped collectively. Promotion is a per-stack decision."
        testid="flags-header"
      />

      {!data && <LoadingRow />}

      {data && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 md:gap-6">
          <Card className="md:col-span-2" testid="flags-broker">
            <div className="flex items-start gap-3">
              <Lock size={20} weight="bold" className="text-rd-warn mt-1" />
              <div className="flex-1">
                <div className="label-eyebrow mb-1">Broker control (global)</div>
                <div className="font-mono text-sm mb-2">BROKER_LIVE_ORDER_ENABLED</div>
                <div className="text-xs text-rd-muted leading-relaxed">
                  Master switch. While false, no runtime can place a live order — even if its own
                  enforce flag is true.
                </div>
              </div>
              <Badge color={data.broker_live_order_enabled ? "#EF4444" : "#71717A"}>
                {data.broker_live_order_enabled ? "TRUE · LIVE" : "FALSE · DISABLED"}
              </Badge>
            </div>
          </Card>

          <FlagCard
            color="#3B82F6"
            label="ALPHA"
            project="RISEDUAL-AI-2"
            flag="PHASE6_ENFORCE_ENABLED"
            on={data.enforce_flags.alpha_phase6_enforce_enabled}
            note="Promotes Alpha's Phase-6 proposals from advisory to enforced."
            testid="flag-alpha"
          />
          <FlagCard
            color="#F59E0B"
            label="CAMARO"
            project="RD4_0421"
            flag="CAMARO_EXECUTOR_ENFORCE_ENABLED"
            on={data.enforce_flags.camaro_executor_enforce_enabled}
            note="Allows Camaro's executor to act on shadow rows."
            testid="flag-camaro"
          />
          <FlagCard
            color="#10B981"
            label="CHEVELLE"
            project="2.1-APP"
            flag="CHEVELLE_AUTHORITY_ENABLED"
            on={data.enforce_flags.chevelle_authority_enabled}
            note="Grants Chevelle authority calls binding power."
            testid="flag-chevelle"
          />

          <Card className="md:col-span-2" testid="flags-doctrine">
            <div className="label-eyebrow mb-2">Doctrine</div>
            <p className="text-xs font-mono text-rd-muted leading-relaxed">
              {data.doctrine}. Promotion gates remain isolated per runtime — flipping one does not
              flip another. A runtime cannot promote itself; promotion requires an out-of-band
              operator action plus broker-level enablement.
            </p>
          </Card>
        </div>
      )}
    </div>
  );
}

function FlagCard({ color, label, project, flag, on, note, testid }) {
  return (
    <Card accentColor={color} testid={testid}>
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <div className="font-display text-xl font-black tracking-tighter" style={{ color }}>
            {label}
          </div>
          <div className="label-eyebrow mt-1">{project}</div>
        </div>
        <Badge color={on ? "#10B981" : "#71717A"}>{on ? "ENABLED" : "DISABLED"}</Badge>
      </div>
      <div className="font-mono text-xs text-rd-text mb-2">{flag}</div>
      <div className="text-xs text-rd-muted leading-relaxed">{note}</div>
    </Card>
  );
}
