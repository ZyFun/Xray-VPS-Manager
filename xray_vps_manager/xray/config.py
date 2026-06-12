"""Xray config.json helpers."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xray_vps_manager.clients.models import client_name
from xray_vps_manager.core.paths import CONFIG_PATH

INBOUND_TAG = "vless-reality"
DEFAULT_CONNECTION_NAME = "default"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    return json.loads(path.read_text())


def save_config(config: dict[str, Any], path: Path = CONFIG_PATH) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    backup = path.with_name(f"{path.name}.bak.{timestamp}")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    shutil.chown(tmp, user="root", group="xray")
    os.chmod(tmp, 0o640)
    tmp.replace(path)
    return backup


def reality_inbounds(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        inbound
        for inbound in config.get("inbounds", [])
        if inbound.get("protocol") == "vless"
        and inbound.get("streamSettings", {}).get("security") == "reality"
    ]


def inbound_tag(inbound: dict[str, Any]) -> str:
    return inbound.get("tag") or INBOUND_TAG


def find_inbound(config: dict[str, Any]) -> dict[str, Any]:
    for inbound in config.get("inbounds", []):
        if inbound.get("tag") == INBOUND_TAG:
            return inbound
    for inbound in reality_inbounds(config):
        return inbound
    raise ValueError("VLESS Reality inbound not found.")


def find_inbound_by_tag(config: dict[str, Any], tag: str) -> dict[str, Any]:
    for inbound in reality_inbounds(config):
        if inbound_tag(inbound) == tag:
            return inbound
    raise ValueError(f"Reality connection not found: {tag}")


def default_connection_tag(config: dict[str, Any]) -> str:
    for inbound in reality_inbounds(config):
        if inbound_tag(inbound) == INBOUND_TAG:
            return INBOUND_TAG
    inbounds = reality_inbounds(config)
    if inbounds:
        return inbound_tag(inbounds[0])
    raise ValueError("VLESS Reality inbound not found.")


def connection_name_from_tag(tag: str) -> str:
    return DEFAULT_CONNECTION_NAME if tag == INBOUND_TAG else tag.replace("vless-reality-", "")


def reality_dest(sni: str) -> str:
    return f"{sni}:443" if sni else ""


def connection_settings_from_inbound(inbound: dict[str, Any]) -> dict[str, Any]:
    reality = inbound.get("streamSettings", {}).get("realitySettings", {})
    sni = (reality.get("serverNames") or [""])[0]
    port = int(inbound.get("port", 443))
    return {
        "tag": inbound_tag(inbound),
        "port": port,
        "sni": sni,
        "dest": reality.get("dest", reality_dest(sni)),
    }


def clients(inbound: dict[str, Any]) -> list[dict[str, Any]]:
    settings = inbound.setdefault("settings", {})
    return settings.setdefault("clients", [])


def active_client(inbound: dict[str, Any], name: str) -> dict[str, Any] | None:
    for item in clients(inbound):
        if client_name(item) == name:
            return item
    return None


def active_client_any(config: dict[str, Any], name: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    for inbound in reality_inbounds(config):
        item = active_client(inbound, name)
        if item is not None:
            return inbound, item
    return None, None
