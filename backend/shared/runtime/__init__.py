"""shared.runtime — portable survival layer (platform-agnostic).

This package holds doctrine-pinned infrastructure that must keep working
regardless of which hosting platform Mission Control runs on (Emergent,
Railway, Render, VPS, local). It does not import from anything Emergent-
specific and may not be hot-coupled to fastapi route handlers.
"""
