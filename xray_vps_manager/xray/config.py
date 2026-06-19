"""Xray config.json helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xray_vps_manager.clients.models import client_name
from xray_vps_manager.core.paths import CONFIG_PATH

INBOUND_TAG = "vless-reality"
TLS_INBOUND_TAG = "vless-tls"
DEFAULT_CONNECTION_NAME = "default"
REALITY_TRANSPORTS = ("tcp", "grpc", "xhttp")
DEFAULT_REALITY_TRANSPORT = "tcp"
DEFAULT_GRPC_SERVICE_NAME = "vless-grpc"
DEFAULT_XHTTP_PATH = "/vless-xhttp"
DEFAULT_XHTTP_MODE = "auto"
DEFAULT_XHTTP_TLS_LOCAL_PORT = 10000
DEFAULT_XHTTP_TLS_PUBLIC_PORT = 443
XHTTP_MODES = ("auto", "packet-up", "stream-up", "stream-one")
VISION_FLOW = "xtls-rprx-vision"
GRPC_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


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


def tls_xhttp_inbounds(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        inbound
        for inbound in config.get("inbounds", [])
        if inbound.get("protocol") == "vless"
        and inbound_tag(inbound).startswith(TLS_INBOUND_TAG)
        and inbound.get("streamSettings", {}).get("network") == "xhttp"
    ]


def vless_connection_inbounds(config: dict[str, Any]) -> list[dict[str, Any]]:
    return reality_inbounds(config) + [
        inbound
        for inbound in tls_xhttp_inbounds(config)
        if inbound not in reality_inbounds(config)
    ]


def inbound_tag(inbound: dict[str, Any]) -> str:
    return inbound.get("tag") or INBOUND_TAG


def find_inbound(config: dict[str, Any]) -> dict[str, Any]:
    for inbound in config.get("inbounds", []):
        if inbound.get("tag") == INBOUND_TAG:
            return inbound
    for inbound in reality_inbounds(config):
        return inbound
    for inbound in vless_connection_inbounds(config):
        return inbound
    raise ValueError("VLESS connection inbound not found.")


def find_inbound_by_tag(config: dict[str, Any], tag: str) -> dict[str, Any]:
    for inbound in vless_connection_inbounds(config):
        if inbound_tag(inbound) == tag:
            return inbound
    raise ValueError(f"VLESS connection not found: {tag}")


def default_connection_tag(config: dict[str, Any]) -> str:
    for inbound in reality_inbounds(config):
        if inbound_tag(inbound) == INBOUND_TAG:
            return INBOUND_TAG
    inbounds = vless_connection_inbounds(config)
    if inbounds:
        return inbound_tag(inbounds[0])
    raise ValueError("VLESS connection inbound not found.")


def connection_name_from_tag(tag: str) -> str:
    if tag == INBOUND_TAG:
        return DEFAULT_CONNECTION_NAME
    if tag == TLS_INBOUND_TAG:
        return "tls"
    if tag.startswith("vless-reality-"):
        return tag.replace("vless-reality-", "")
    if tag.startswith("vless-tls-"):
        return tag.replace("vless-tls-", "")
    return tag


def reality_dest(sni: str) -> str:
    return f"{sni}:443" if sni else ""


def normalize_reality_transport(value: str | None) -> str:
    transport = (value or DEFAULT_REALITY_TRANSPORT).strip().lower()
    if transport not in REALITY_TRANSPORTS:
        raise ValueError("REALITY_TRANSPORT must be one of: " + ", ".join(REALITY_TRANSPORTS))
    return transport


def normalize_grpc_service_name(value: str | None) -> str:
    service_name = (value or DEFAULT_GRPC_SERVICE_NAME).strip()
    if not GRPC_SERVICE_NAME_RE.fullmatch(service_name):
        raise ValueError("GRPC serviceName must be 1-128 chars: A-Z a-z 0-9 _ . -")
    return service_name


def normalize_xhttp_path(value: str | None) -> str:
    path = (value or DEFAULT_XHTTP_PATH).strip()
    if not path.startswith("/") or any(char.isspace() for char in path) or len(path) > 256:
        raise ValueError("XHTTP path must start with /, contain no spaces, and be at most 256 chars.")
    return path


def normalize_xhttp_mode(value: str | None) -> str:
    mode = (value or DEFAULT_XHTTP_MODE).strip().lower()
    if mode not in XHTTP_MODES:
        raise ValueError("XHTTP mode must be one of: " + ", ".join(XHTTP_MODES))
    return mode


def reality_transport_settings_from_stream(stream: dict[str, Any]) -> dict[str, str]:
    transport = normalize_reality_transport(stream.get("network", DEFAULT_REALITY_TRANSPORT))
    settings = {"transport": transport}
    if transport == "grpc":
        grpc = stream.get("grpcSettings") or {}
        settings["grpcServiceName"] = normalize_grpc_service_name(grpc.get("serviceName"))
    elif transport == "xhttp":
        xhttp = stream.get("xhttpSettings") or {}
        settings["xhttpPath"] = normalize_xhttp_path(xhttp.get("path"))
        settings["xhttpMode"] = normalize_xhttp_mode(xhttp.get("mode"))
    return settings


def connection_transport_settings_from_stream(stream: dict[str, Any]) -> dict[str, str]:
    return reality_transport_settings_from_stream(stream)


def reality_transport_settings_from_inbound(inbound: dict[str, Any]) -> dict[str, str]:
    return reality_transport_settings_from_stream(inbound.get("streamSettings", {}))


def connection_transport_settings_from_inbound(inbound: dict[str, Any]) -> dict[str, str]:
    return connection_transport_settings_from_stream(inbound.get("streamSettings", {}))


def apply_reality_transport(
    stream: dict[str, Any],
    transport: str | None = None,
    *,
    grpc_service_name: str | None = None,
    xhttp_path: str | None = None,
    xhttp_mode: str | None = None,
) -> dict[str, str]:
    normalized = normalize_reality_transport(transport)
    stream["network"] = normalized
    stream.pop("tcpSettings", None)
    stream.pop("grpcSettings", None)
    stream.pop("xhttpSettings", None)
    if normalized == "grpc":
        service_name = normalize_grpc_service_name(grpc_service_name)
        stream["grpcSettings"] = {"serviceName": service_name}
        return {"transport": normalized, "grpcServiceName": service_name}
    if normalized == "xhttp":
        path = normalize_xhttp_path(xhttp_path)
        mode = normalize_xhttp_mode(xhttp_mode)
        stream["xhttpSettings"] = {"path": path, "mode": mode}
        return {"transport": normalized, "xhttpPath": path, "xhttpMode": mode}
    return {"transport": normalized}


def client_flow_for_transport(transport: str | None) -> str:
    return VISION_FLOW if normalize_reality_transport(transport) == "tcp" else ""


def apply_client_transport(client: dict[str, Any], transport: str | None) -> dict[str, Any]:
    flow = client_flow_for_transport(transport)
    if flow:
        client.setdefault("flow", flow)
    else:
        client.pop("flow", None)
    return client


def connection_settings_from_inbound(inbound: dict[str, Any]) -> dict[str, Any]:
    stream = inbound.get("streamSettings", {})
    port = int(inbound.get("port", 443))
    security = stream.get("security") or "none"
    settings = {"tag": inbound_tag(inbound), "security": security, "port": port, "sni": "", "dest": ""}
    if security == "reality":
        reality = stream.get("realitySettings", {})
        sni = (reality.get("serverNames") or [""])[0]
        settings.update(
            {
                "sni": sni,
                "dest": reality.get("dest", reality_dest(sni)),
            }
        )
    settings.update(connection_transport_settings_from_stream(stream))
    return settings


def clients(inbound: dict[str, Any]) -> list[dict[str, Any]]:
    settings = inbound.setdefault("settings", {})
    return settings.setdefault("clients", [])


def active_client(inbound: dict[str, Any], name: str) -> dict[str, Any] | None:
    for item in clients(inbound):
        if client_name(item) == name:
            return item
    return None


def active_client_any(config: dict[str, Any], name: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    for inbound in vless_connection_inbounds(config):
        item = active_client(inbound, name)
        if item is not None:
            return inbound, item
    return None, None
