"""Fernet-based encryption for at-rest secrets (Kraken keys etc.).

Single key for the whole app, read from `CREDENTIALS_ENCRYPTION_KEY` in
the backend `.env`. We generate one on first import if the env-var is
missing AND we have write access to `.env` — this keeps local dev frictionless
while still failing safely in production: a redeployed container without
the env-var will not silently rotate the key and lose previously-encrypted
credentials.

Doctrine:
    - Plaintext secrets only exist in memory at the moment they're used.
    - The encryption key never leaves the backend process.
    - We do NOT round-trip ciphertext through any API — the only thing
      the operator ever sees back from the API is a redacted preview.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


ENV_KEY = "CREDENTIALS_ENCRYPTION_KEY"
_BACKEND_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _read_env_value(key: str) -> str | None:
    if not _BACKEND_ENV_PATH.exists():
        return None
    for raw in _BACKEND_ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if line.startswith(f"{key}="):
            v = line.split("=", 1)[1].strip()
            return v.strip('"').strip("'")
    return None


def _persist_to_dotenv(key: str, value: str) -> None:
    """Append a key to backend/.env (idempotent). Used only when we
    auto-generate an encryption key on first run in local dev."""
    lines = _BACKEND_ENV_PATH.read_text().splitlines() if _BACKEND_ENV_PATH.exists() else []
    if any(line.strip().startswith(f"{key}=") for line in lines):
        return
    lines.append(f'{key}="{value}"')
    _BACKEND_ENV_PATH.write_text("\n".join(lines) + "\n")


def _load_or_create_key() -> bytes:
    """Resolve the Fernet key. This is the ONE key that decrypts every
    at-rest secret in the stack — Kraken private key, Webull app secret,
    every future broker credential. If it rotates, ALL encrypted docs
    become undecryptable simultaneously, and Kraken looks "lost" while
    Webull looks "deactivated" (same silent failure, different broker).

    2026-07-14 hardening (iter-29d):
        Refuse to auto-generate a new key when we're plainly running
        inside a deploy container. The pre-existing behavior was to
        write a fresh key into `backend/.env` on first run — fine for
        local dev, catastrophic in production because Emergent's deploy
        pipeline does NOT carry `backend/.env` across container images.
        Every deploy generated a NEW key → every deploy Kraken +
        Webull creds looked corrupt.

        Detection heuristic: `RUNTIME_ENV=production` OR any of the
        infra-provided vars (`KUBERNETES_SERVICE_HOST`, `EMERGENT_APP_ID`)
        being set means we're in a container. In that case, missing key
        is FATAL — better a hard boot failure than a silent credential
        rotation that costs the operator a full day of re-entering
        broker keys.
    """
    val = os.environ.get(ENV_KEY) or _read_env_value(ENV_KEY)
    if val:
        return val.encode() if isinstance(val, str) else val

    in_container = any(os.environ.get(k) for k in (
        "KUBERNETES_SERVICE_HOST",
        "EMERGENT_APP_ID",
    )) or os.environ.get("RUNTIME_ENV", "").lower() == "production"

    if in_container:
        raise RuntimeError(
            f"{ENV_KEY} is not set in the container environment. "
            "This key MUST be persisted across deploys — set it via the "
            "deploy pipeline (Emergent app settings → Environment "
            "Variables), NOT in backend/.env (which is ephemeral in "
            "deployed containers). Auto-generating a new key here would "
            "rotate it every deploy and silently invalidate every "
            "encrypted broker credential in Mongo (Kraken + Webull would "
            "both appear 'lost' or 'deactivated'). Refusing to start."
        )

    # Local dev only: generate + persist to backend/.env.
    new_key = Fernet.generate_key().decode()
    try:
        _persist_to_dotenv(ENV_KEY, new_key)
        os.environ[ENV_KEY] = new_key
        return new_key.encode()
    except (OSError, PermissionError) as e:
        raise RuntimeError(
            f"{ENV_KEY} is not set and we cannot write to backend/.env: {e}. "
            "Set CREDENTIALS_ENCRYPTION_KEY via your deploy pipeline."
        ) from e


_fernet: Fernet | None = None


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string, returning the Fernet token (str)."""
    if not isinstance(plaintext, str):
        raise TypeError("encrypt expects a str")
    return _cipher().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    """Decrypt a Fernet token previously produced by `encrypt`. Raises
    on bad ciphertext / wrong key."""
    if not isinstance(token, str):
        raise TypeError("decrypt expects a str")
    try:
        return _cipher().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("encrypted credential is unreadable (wrong key?)") from e


def redact(value: str, keep: int = 4) -> str:
    """Format a redacted preview for UI display. Shows first+last `keep`
    chars, masks the middle. Safe for short strings — if the value is
    too short to redact meaningfully, returns all asterisks."""
    if not value:
        return ""
    if len(value) <= keep * 2 + 3:
        return "*" * max(len(value), 4)
    return f"{value[:keep]}{'*' * 8}{value[-keep:]}"
