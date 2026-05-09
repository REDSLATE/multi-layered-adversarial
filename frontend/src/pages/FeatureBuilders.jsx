import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";

export default function FeatureBuilders() {
  const [data, setData] = useState(null);
  useEffect(() => {
    (async () => {
      const { data } = await api.get("/shared/feature-builders");
      setData(data);
    })();
  }, []);

  return (
    <div className="reveal" data-testid="features-page">
      <PageHeader
        eyebrow="Shared · Feature builders"
        title="Deterministic feature recipes"
        sub="A shared catalog of deterministic feature-engineering recipes. All runtimes can opt in. Recipes are pure by contract — same input, same output."
        testid="features-header"
      />

      {!data && <LoadingRow />}
      {data && data.items.length === 0 && <EmptyState testid="features-empty" />}

      {data && data.items.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 md:gap-6" data-testid="features-grid">
          {data.items.map((f) => (
            <Card key={f.name} testid={`feature-card-${f.name}`}>
              <div className="flex items-center justify-between mb-2">
                <div className="font-mono text-sm text-rd-text">{f.name}</div>
                <Badge color="#A1A1AA">{f.version}</Badge>
              </div>
              <div className="flex items-center gap-2 mb-3">
                <Badge color="#FBBF24">{f.kind}</Badge>
                {f.deterministic && <Badge color="#10B981">deterministic</Badge>}
              </div>
              <div className="text-xs text-rd-muted leading-relaxed">{f.description}</div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
