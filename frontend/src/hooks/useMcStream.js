import { useEffect, useRef, useState, useCallback } from "react";
import { API, getToken } from "@/lib/api";

/**
 * Doctrine pin (2026-06-10, P2): single shared SSE connection to MC.
 *
 * Subscribes to `/api/mc-connection/stream` and surfaces:
 *   * `lastEvent`     — most recent event of any type
 *   * `byType`        — { intent: [...], position_misread: [...], ... }
 *                       capped at `cap` entries per type, newest first
 *   * `connected`     — boolean; true while EventSource is OPEN
 *   * `lastError`     — error event detail (null if clean)
 *   * `currentRegime` — last regime emitted by the server, or null
 *
 * Auto-reconnects with exponential backoff capped at 30s.
 * Cleans up on unmount — no leaked connections.
 */
export function useMcStream({ cap = 50 } = {}) {
  const [byType, setByType] = useState({});
  const [lastEvent, setLastEvent] = useState(null);
  const [connected, setConnected] = useState(false);
  const [lastError, setLastError] = useState(null);
  const [currentRegime, setCurrentRegime] = useState(null);

  const esRef = useRef(null);
  const reconnectDelayRef = useRef(1000);
  const closedRef = useRef(false);

  const pushEvent = useCallback((eventName, payload) => {
    const enriched = { ...payload, _eventName: eventName, _receivedAt: Date.now() };
    setLastEvent(enriched);
    setByType((prev) => {
      const list = prev[eventName] ? [enriched, ...prev[eventName]] : [enriched];
      return { ...prev, [eventName]: list.slice(0, cap) };
    });
    if (eventName === "regime" && payload?.regime) {
      setCurrentRegime(payload.regime);
    }
  }, [cap]);

  useEffect(() => {
    closedRef.current = false;
    let aborted = false;

    const connect = () => {
      if (aborted) return;
      const token = getToken();
      if (!token) {
        setLastError({ kind: "no_token", detail: "Not logged in" });
        return;
      }
      const url = `${API}/mc-connection/stream?token=${encodeURIComponent(token)}`;
      const es = new EventSource(url);
      esRef.current = es;

      es.onopen = () => {
        setConnected(true);
        setLastError(null);
        reconnectDelayRef.current = 1000; // reset backoff on successful open
      };

      const named = ["hello", "intent", "broker_fill", "position_misread", "regime", "heartbeat", "error"];
      named.forEach((name) => {
        es.addEventListener(name, (e) => {
          try {
            const data = JSON.parse(e.data);
            pushEvent(name, data);
          } catch (err) {
            // Malformed payload — surface but don't crash the stream.
            console.warn("[useMcStream] parse error", name, err);
          }
        });
      });

      es.onerror = () => {
        setConnected(false);
        es.close();
        if (closedRef.current || aborted) return;
        // Exponential backoff: 1s → 2s → 4s → ... → 30s cap.
        const delay = Math.min(reconnectDelayRef.current, 30000);
        setLastError({ kind: "disconnected", detail: `retrying in ${Math.round(delay / 1000)}s` });
        setTimeout(() => {
          reconnectDelayRef.current = Math.min(reconnectDelayRef.current * 2, 30000);
          connect();
        }, delay);
      };
    };

    connect();

    return () => {
      aborted = true;
      closedRef.current = true;
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
    };
  }, [pushEvent]);

  return { byType, lastEvent, connected, lastError, currentRegime };
}
