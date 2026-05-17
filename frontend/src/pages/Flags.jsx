import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { PageHeader, Card, Badge, LoadingRow } from "@/components/ui-bits";
import { Lock } from "@phosphor-icons/react";

/**
 * Flags page — system-wide runtime flags only.
 *
 * Doctrine pin (2026-02-17, rev3):
 *   Authority lives on SEATS, not brains. Brain-named enforce flags
 *   (PHASE6_ENFORCE_ENABLED, CAMARO_EXECUTOR_ENFORCE_ENABLED,
 *   CHEVELLE_AUTHORITY_ENABLED, REDEYE_OPPONENT_ENFORCE_ENABLED) have
 *   been retired — they were the source of brain-name-locked
 *   restrictions that caused authority-by-identity contamination.
 *   This page now surfaces ONLY system-wide flags + the seat doctrine
 *   restatement. Per-seat doctrine lives at /admin/doctrine.
 */
export default function Flags() {
  const [data, setData] = useState(null);
  useEffect(() => {
    (async () => {
      const res = await api.get("/admin/flags");
      setData(res.data);
    })();
  }, []);

  return (
    <div className="reveal" data-testid="flags-page">
      <PageHeader
        eyebrow="Admin · Runtime flags"
        title="System-wide execution flags"
        sub="Flags scope to the SYSTEM, not to a brain. Per-seat doctrine and authority lives in the roster + the doctrine layer; this page is the master-switch view."
        testid="flags-header"
      />

      {!data && <LoadingRow />}

      {data && (
        <div className="grid grid-cols-1 gap-4 md:gap-6">
          <Card testid="flags-broker">
            <div className="flex items-start gap-3">
              <Lock size={20} weight="bold" className="text-rd-warn mt-1" />
              <div className="flex-1">
                <div className="label-eyebrow mb-1">Broker control (global)</div>
                <div className="font-mono text-sm mb-2">BROKER_LIVE_ORDER_ENABLED</div>
                <div className="text-xs text-rd-muted leading-relaxed">
                  Master switch on live order submission. When false,
                  the execution gate refuses to submit live broker
                  orders system-wide. Seat policy is unaffected — a
                  seat holder remains the deciding authority for every
                  intent regardless of this flag.
                </div>
              </div>
              <Badge color={data.broker_live_order_enabled ? "#10B981" : "#71717A"}>
                {data.broker_live_order_enabled ? "TRUE · LIVE" : "FALSE · DISABLED"}
              </Badge>
            </div>
          </Card>

          <Card testid="flags-doctrine">
            <div className="label-eyebrow mb-2">Doctrine</div>
            <p className="text-xs font-mono text-rd-muted leading-relaxed">
              {data.doctrine}
            </p>
          </Card>

          <Card testid="flags-deploy-mode">
            <div className="flex items-baseline justify-between">
              <div>
                <div className="label-eyebrow mb-1">Deploy mode</div>
                <div className="font-mono text-sm">DEPLOY_MODE</div>
              </div>
              <Badge color="#FBBF24">{data.deploy_mode}</Badge>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}
