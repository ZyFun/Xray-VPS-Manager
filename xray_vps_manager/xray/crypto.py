"""Xray credential generation helpers."""

from __future__ import annotations

import secrets
import subprocess


def xray_uuid() -> str:
    return subprocess.check_output(["/usr/local/bin/xray", "uuid"], text=True).strip()


def xray_x25519_keys() -> tuple[str, str]:
    output = subprocess.check_output(["/usr/local/bin/xray", "x25519"], text=True)
    private_key = ""
    public_key = ""
    for line in output.splitlines():
        if line.startswith("PrivateKey:") or line.startswith("Private key:"):
            private_key = line.split(": ", 1)[1].strip()
        if line.startswith("Password (PublicKey):") or line.startswith("PublicKey:") or line.startswith("Public key:"):
            public_key = line.split(": ", 1)[1].strip()
    if not private_key or not public_key:
        raise RuntimeError("Failed to generate Reality key pair.")
    return private_key, public_key


def random_short_id() -> str:
    return subprocess.check_output(["openssl", "rand", "-hex", "8"], text=True).strip()


def random_trojan_password() -> str:
    return secrets.token_urlsafe(32)


def reality_public_key(private_key: str) -> str:
    output = subprocess.check_output(["/usr/local/bin/xray", "x25519", "-i", private_key], text=True)
    for line in output.splitlines():
        if line.startswith("Password (PublicKey):"):
            return line.split(": ", 1)[1].strip()
        if line.startswith("PublicKey:") or line.startswith("Public key:"):
            return line.split(": ", 1)[1].strip()
    raise RuntimeError("Failed to derive Reality public key.")
