"""Xray credential generation helpers."""

from __future__ import annotations

import re
import secrets
import subprocess
from typing import Any

# X25519-ключ — 32 байта в base64url без паддинга.
X25519_KEY_RE = re.compile(r"[A-Za-z0-9_-]{43}")
# Значения, похожие на ключ, скрываются в тексте ошибки во всех строках, кроме публичных.
KEY_LIKE_RE = re.compile(r"[A-Za-z0-9_+/=-]{16,}")
PUBLIC_LABEL_WORDS = ("public", "password", "hash32")


def xray_uuid() -> str:
    return subprocess.check_output(["/usr/local/bin/xray", "uuid"], text=True).strip()


def is_x25519_key(value: Any) -> bool:
    return isinstance(value, str) and X25519_KEY_RE.fullmatch(value) is not None


def parse_x25519_output(output: str) -> tuple[str, str]:
    """Ключи из вывода `xray x25519 [-i KEY]`; пустая строка — ключ не найден.

    Формат вывода менялся между версиями Xray (XR-REV-051), поэтому метки ищутся по слову:
    `Private` — приватный ключ, `Public`/`Password` — публичный; строка `Hash32` пропускается.
    """
    private_key = ""
    public_key = ""
    for line in output.splitlines():
        label, separator, value = line.partition(":")
        label = label.lower()
        value = value.strip()
        if not separator or "hash32" in label or not is_x25519_key(value):
            continue
        if "private" in label:
            private_key = private_key or value
        elif "public" in label or "password" in label:
            public_key = public_key or value
    return private_key, public_key


def masked_x25519_output(output: str, secret: str = "") -> str:
    """Сырой вывод `xray x25519` для текста ошибки без приватного ключа."""
    lines = []
    for line in output.splitlines():
        if secret:
            line = line.replace(secret, "<hidden>")
        label, separator, value = line.partition(":")
        lowered = label.lower()
        if separator and "private" in lowered:
            line = f"{label}: <hidden>" if value.strip() else line
        elif not separator or not any(word in lowered for word in PUBLIC_LABEL_WORDS):
            line = KEY_LIKE_RE.sub("<hidden>", line)
        lines.append(line)
    return "\n".join(lines) if lines else "(empty)"


def x25519_output_error(message: str, command: str, output: str, secret: str = "") -> RuntimeError:
    return RuntimeError(f"{message} Unrecognized `{command}` output:\n{masked_x25519_output(output, secret)}")


def xray_x25519_keys() -> tuple[str, str]:
    output = subprocess.check_output(["/usr/local/bin/xray", "x25519"], text=True)
    private_key, public_key = parse_x25519_output(output)
    if not private_key or not public_key:
        raise x25519_output_error("Failed to generate Reality key pair.", "xray x25519", output)
    return private_key, public_key


def random_short_id() -> str:
    return subprocess.check_output(["openssl", "rand", "-hex", "8"], text=True).strip()


def random_trojan_password() -> str:
    return secrets.token_urlsafe(32)


def reality_public_key(private_key: str) -> str:
    output = subprocess.check_output(["/usr/local/bin/xray", "x25519", "-i", private_key], text=True)
    _private_key, public_key = parse_x25519_output(output)
    if not public_key:
        raise x25519_output_error("Failed to derive Reality public key.", "xray x25519 -i", output, private_key)
    return public_key
