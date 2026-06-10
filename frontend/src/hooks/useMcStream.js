import { useEffect, useRef, useState, useCallback } from "react";
import { API, getToken } from "@/lib/api";

/**
 * Doctrine pin (2026-06-10, P2 / P1 toast follow-up):
 *
 * Single SHARED EventSource across all consumers in the app. The previous
 * implementation opened one EventSource per `useMcStream()` call — which
 * was fine when 1 card per page used it, but now that the misread toast
 * host is mounted at the Layout level AND multiple cards on the same
 * dashboard also subscribe, we'd end up with 3-4 concurrent SSE
 * connections per page hammering the backend.
 *
 * Architecture:
 *   * `_singleton` holds the live EventSource + a Set of subscribers.
 *   * First subscriber opens the connection; last unsubscriber closes it.
 *   * Every event is fanned out to every subscriber via their callback.
 *   * Same auto-reconnect with exponential backoff as before.
 *
 * Public API:
 *   * `useMcStream({cap})` — React hook returning `{byType, lastEvent,
 *     connected, lastError, currentRegime}`.
 *   * `subscribeMcStream({onEvent, onStatus})` — imperative subscribe
 *     used by side-effect consumers (e.g., the misread toast host)
 *     that DO NOT need the latest-N event buffer in component state.
 */

const _singleton = {
  es: null,
  subscribers: new Set(),
  connected: false,
  lastError: null,
  reconnectDelay: 1000,
  reconnectTimer: null,
  closedByApp: false,
};

const NAMED_EVENTS = ["hello", "intent", "broker_fill", "position_misread", "regime", "heartbeat", "error"];

function _notify() {
  // Snapshot status so subscribers see the same connected/error pair.
  const status = {
    connected: _singleton.connected,
    lastError: _singleton.lastError,
  };
  for (const sub of _singleton.subscribers) {
    try {
      sub.onStatus(status);
    } catch (err) {
      // Subscriber callback shouldn't crash the stream; log and continue.
      console.warn("[useMcStream] subscriber.onStatus threw", err);
    }
  }
}

function _dispatch(eventName, payload) {
  for (const sub of _singleton.subscribers) {
    try {
      sub.onEvent(eventName, payload);
    } catch (err) {
      console.warn("[useMcStream] subscriber.onEvent threw", err);
    }
  }
}

function _openConnection() {
  if (_singleton.es) return; // already open
  const token = getToken();
  if (!token) {
    _singleton.lastError = { kind: "no_token", detail: "Not logged in" };
    _notify();
    return;
  }
  const url = `${API}/mc-connection/stream?token=${encodeURIComponent(token)}`;
  const es = new EventSource(url);
  _singleton.es = es;

  es.onopen = () => {
    _singleton.connected = true;
    _singleton.lastError = null;
    _singleton.reconnectDelay = 1000;
    _notify();
  };

  NAMED_EVENTS.forEach((name) => {
    es.addEventListener(name, (e) => {
      try {
        const data = JSON.parse(e.data);
        _dispatch(name, data);
      } catch (err) {
        console.warn("[useMcStream] parse error", name, err);
      }
    });
  });

  es.onerror = () => {
    _singleton.connected = false;
    es.close();
    _singleton.es = null;
    if (_singleton.closedByApp) return;
    if (_singleton.subscribers.size === 0) return;
    const delay = Math.min(_singleton.reconnectDelay, 30000);
    _singleton.lastError = { kind: "disconnected", detail: `retrying in ${Math.round(delay / 1000)}s` };
    _notify();
    if (_singleton.reconnectTimer) clearTimeout(_singleton.reconnectTimer);
    _singleton.reconnectTimer = setTimeout(() => {
      _singleton.reconnectTimer = null;
      _singleton.reconnectDelay = Math.min(_singleton.reconnectDelay * 2, 30000);
      _openConnection();
    }, delay);
  };
}

function _closeIfIdle() {
  if (_singleton.subscribers.size > 0) return;
  _singleton.closedByApp = true;
  if (_singleton.reconnectTimer) {
    clearTimeout(_singleton.reconnectTimer);
    _singleton.reconnectTimer = null;
  }
  if (_singleton.es) {
    _singleton.es.close();
    _singleton.es = null;
  }
  _singleton.connected = false;
}

function _subscribe(sub) {
  _singleton.closedByApp = false;
  _singleton.subscribers.add(sub);
  if (!_singleton.es) {
    _openConnection();
  } else {
    // Push current status to the new subscriber so it doesn't sit in
    // limbo waiting for the next event.
    sub.onStatus({
      connected: _singleton.connected,
      lastError: _singleton.lastError,
    });
  }
  return () => {
    _singleton.subscribers.delete(sub);
    if (_singleton.subscribers.size === 0) {
      _closeIfIdle();
    }
  };
}

/**
 * Imperative subscribe — preferred for consumers that react to NEW
 * events (e.g., toasts) and don't want the state-buffer overhead of
 * the React hook. Returns an unsubscribe function.
 */
export function subscribeMcStream({ onEvent, onStatus } = {}) {
  return _subscribe({
    onEvent: onEvent || (() => {}),
    onStatus: onStatus || (() => {}),
  });
}

export function useMcStream({ cap = 50 } = {}) {
  const [byType, setByType] = useState({});
  const [lastEvent, setLastEvent] = useState(null);
  const [connected, setConnected] = useState(_singleton.connected);
  const [lastError, setLastError] = useState(_singleton.lastError);
  const [currentRegime, setCurrentRegime] = useState(null);

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

  const capRef = useRef(cap);
  useEffect(() => { capRef.current = cap; }, [cap]);

  useEffect(() => {
    const sub = {
      onEvent: (name, payload) => pushEvent(name, payload),
      onStatus: ({ connected: c, lastError: e }) => {
        setConnected(c);
        setLastError(e);
      },
    };
    const unsub = _subscribe(sub);
    return unsub;
  }, [pushEvent]);

  return { byType, lastEvent, connected, lastError, currentRegime };
}
