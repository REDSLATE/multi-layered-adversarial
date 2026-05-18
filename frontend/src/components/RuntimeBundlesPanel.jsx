import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, LoadingRow } from "@/components/ui-bits";

/**
 * RuntimeBundlesPanel — operator-side extractor for the platform
 * survival kit (and any future portable patch kit).
 *
 * The preview pod is NOT proof of PROD. The kit must be downloadable
 * so the operator can drop it into each brain stack's repo, which
 * lives outside Emergent. This panel:
 *   - lists every registered bundle with its sha256 + size
 *   - exposes a "download" button that triggers a JWT-authed browser
 *     download (uses the same access token the rest of the dashboard
 *     uses, attached as a Bearer header on a fetch → blob → anchor)
 *   - shows the doctrine note next to each bundle
 */

export default function RuntimeBundlesPanel() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [downloading, setDownloading] = useState({});

  const load = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/runtime-bundles");
      setData(data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const download = useCallback(async (filename) => {
    setDownloading((m) => ({ ...m, [filename]: true }));
    try {
      // Hit the endpoint as a blob so the browser saves the binary
      // with the right filename + content-type.
      const resp = await api.get(`/admin/runtime-bundles/${filename}`, {
        responseType: "blob",
      });
      const url = URL.createObjectURL(resp.data);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setDownloading((m) => ({ ...m, [filename]: false }));
    }
  }, []);

  return (
    <Card className="p-0 overflow-hidden" testid="runtime-bundles-panel">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 border-b border-rd-border bg-rd-bg3">
        <div className="label-eyebrow text-rd-dim">Portable patch kits</div>
        <span className="text-[10px] font-mono text-rd-dim">
          extract from preview → drop into each brain stack's repo
        </span>
        <button
          type="button"
          onClick={load}
          data-testid="runtime-bundles-reload"
          className="ml-auto px-2 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text"
        >
          reload
        </button>
      </div>

      {err && (
        <div className="px-4 py-2 text-xs font-mono text-rd-danger border-b border-rd-border">
          {err}
        </div>
      )}

      {!data && !err && <LoadingRow />}

      {data && (
        <div className="divide-y divide-rd-border">
          {(data.bundles || []).map((b) => (
            <div
              key={b.filename}
              className="px-4 py-3 flex flex-col md:flex-row md:items-center gap-3"
              data-testid={`runtime-bundle-row-${b.filename}`}
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-mono text-sm text-rd-text font-bold">
                    {b.filename}
                  </span>
                  {!b.present && (
                    <span className="text-[10px] font-mono uppercase text-rd-danger border border-rd-danger px-1">
                      missing
                    </span>
                  )}
                </div>
                {b.present && (
                  <div className="text-[10px] font-mono text-rd-dim">
                    <span className="text-rd-text">{(b.bytes / 1024).toFixed(1)} KB</span>
                    {" · "}
                    sha256{" "}
                    <span
                      className="text-rd-text break-all"
                      data-testid={`runtime-bundle-sha-${b.filename}`}
                    >
                      {b.sha256}
                    </span>
                  </div>
                )}
                <div className="text-[11px] text-rd-muted mt-1 leading-relaxed">
                  {b.doctrine_note}
                </div>
              </div>
              <div className="flex-shrink-0">
                <button
                  type="button"
                  disabled={!b.present || downloading[b.filename]}
                  onClick={() => download(b.filename)}
                  data-testid={`runtime-bundle-download-${b.filename}`}
                  className="px-3 py-1.5 text-[11px] font-mono uppercase tracking-wider border border-rd-border text-rd-text hover:bg-rd-bg disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {downloading[b.filename] ? "..." : "download"}
                </button>
              </div>
            </div>
          ))}
          {(data.bundles || []).length === 0 && (
            <div className="px-4 py-6 text-center text-rd-dim font-mono text-xs">
              no bundles registered
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
