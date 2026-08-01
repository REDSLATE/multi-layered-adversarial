# 2026-08-01 Trigger Watcher validation — learnings

- Router picks from LOCAL intent queue (`shared/hotpath/intent_queue`), NOT Mongo.
  Any code that creates intents outside the ingest path MUST call
  `intent_queue.enqueue_safe(doc)` or the intent silently expires unrouted.
- Organic intents have NO `snapshot.price` — only enriched bid/ask. Anything
  needing a frozen emit-time price must derive mid or bar-at-ingest.
- `policy_snapshot` refreshes from Atlas every 20s; hotpath consumers must
  check `broker_frozen` (bool), never reason-string truthiness.
- Entry timing admin routes live under `/api/admin/universe/entry-timing[...]`.
- Preview cannot pass balance/broker gates (no Kraken/Webull keys): sizer
  fails `no_balance_no_trade` then broker `adapter not configured`. To drive
  the full chain in a test process, prime `shared.risk_sizer.balance._cache`.
- trading_controls doc is recreated `first_boot_default_disabled` on fork.
- pytest leaves freeze/thaw residue in `broker_freeze_state` in preview DB.
