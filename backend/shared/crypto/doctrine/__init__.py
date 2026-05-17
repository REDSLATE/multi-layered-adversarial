"""Crypto doctrine package — twin of `shared.doctrine` for the crypto lane.

Doctrine: lanes are twin in seats and brains, but NOT in setup
features. Crypto looks at 24h volume / spread_bps / funding / open
interest / BTC regime alignment. Equity looks at gap_pct / float /
relative volume / pullback patterns. Mixing the two is a structural
failure mode the lane-isolation regression test guards against — see
`tests/test_lane_isolation.py`.

Public surface:
    label_crypto_snapshot(snapshot)             → CryptoDoctrineLabels
    build_crypto_brain_doctrine_packet(snap)    → dict (BRAIN packet)

Routed via `shared.doctrine.lane_doctrine_router.build_lane_doctrine_packet`
so the intent ingest path doesn't have to branch by lane.
"""
