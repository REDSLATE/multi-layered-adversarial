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
    """Resolve the Fernet key, generating + persisting one in local dev if
    none is configured. In production the env-var must be set explicitly
    by the deploy pipeline."""
    val = os.environ.get(ENV_KEY) or _read_env_value(ENV_KEY)
    if val:
        return val.encode() if isinstance(val, str) else val
    # First-run path: generate and persist to backend/.env. We refuse to
    # do this if the .env file isn't writable (read-only mount in prod).
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
