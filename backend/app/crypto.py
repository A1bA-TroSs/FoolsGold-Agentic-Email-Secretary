"""Symmetric encryption for OAuth tokens and API keys at rest.

This protects the data from casual reading (backups, sync folders, other users
on the machine). The key sits next to the database at 0600 -- an attacker who
already has your shell can still read it. macOS Keychain is the upgrade path.
"""
from __future__ import annotations

import os
import stat

from cryptography.fernet import Fernet, InvalidToken

from .config import DATA_DIR, KEY_PATH


def _load_or_create_key() -> bytes:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes()
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    os.chmod(KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return key


_fernet: Fernet | None = None


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt(plaintext: str) -> bytes:
    return _cipher().encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes | None) -> str:
    """Returns "" rather than raising when the blob is missing or the key was
    rotated -- a corrupt token should mean 'log in again', not a crashed app."""
    if not token:
        return ""
    try:
        return _cipher().decrypt(bytes(token)).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""
