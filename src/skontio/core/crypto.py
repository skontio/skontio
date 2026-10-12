"""Encryption of small secrets at rest (TOTP seeds now; SMTP/bank credentials later, plan §10).

AES-256-GCM with a key derived per purpose from SKONTIO_SECRET_KEY (HKDF-SHA256). The secret key
file therefore belongs into the backup of the password manager: without it sealed values are lost.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

VERSION = "v1"


class SealError(ValueError):
    pass


def _key(purpose: str) -> bytes:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"skontio-seal", info=f"{VERSION}:{purpose}".encode())
    return hkdf.derive(settings.SECRET_KEY.encode())


def seal(plaintext: str, *, purpose: str) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key(purpose)).encrypt(nonce, plaintext.encode(), purpose.encode())
    return f"{VERSION}:" + base64.urlsafe_b64encode(nonce + ciphertext).decode()


def unseal(token: str, *, purpose: str) -> str:
    version, _, payload = token.partition(":")
    if version != VERSION:
        raise SealError("Unbekanntes Format")
    raw = base64.urlsafe_b64decode(payload)
    try:
        return AESGCM(_key(purpose)).decrypt(raw[:12], raw[12:], purpose.encode()).decode()
    except InvalidTag as exc:
        raise SealError("Entschlüsselung fehlgeschlagen – anderer SKONTIO_SECRET_KEY?") from exc
