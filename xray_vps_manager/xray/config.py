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
XHTTP_XMUX_KEYS = (
    "maxConcurrency",
    "maxConnections",
    "cMaxReuseTimes",
    "hMaxRequestTimes",
    "hMaxReusableSecs",
    "hKeepAlivePeriod",
)
XHTTP_SERVER_EXTRA_KEYS = ("xPaddingBytes", "scStreamUpServerSecs")
XHTTP_EXTRA_KEYS = XHTTP_SERVER_EXTRA_KEYS + ("xmux",)
DEFAULT_XHTTP_ADVANCED_EXTRA = {
    "xPaddingBytes": "100-1000",
    "scStreamUpServerSecs": "20-80",
    "xmux": {
        "maxConcurrency": "16-32",
        "maxConnections": 0,
        "cMaxReuseTimes": 0,
        "hMaxRequestTimes": "600-900",
        "hMaxReusableSecs": "1800-3000",
        "hKeepAlivePeriod": 0,
    },
}
VISION_FLOW = "xtls-rprx-vision"
GRPC_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
NON_NEGATIVE_RANGE_RE = re.compile(r"^(0|[1-9][0-9]*)(?:-(0|[1-9][0-9]*))?$")
INTEGER_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")


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


def default_xhttp_advanced_extra() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_XHTTP_ADVANCED_EXTRA))


def _is_empty_extra_value(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _normalize_non_negative_range(
    value: Any,
    label: str,
    *,
    allow_negative_one: bool = False,
) -> int | str:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer or range like 100-1000.")
    if isinstance(value, int):
        if allow_negative_one and value == -1:
            return -1
        if value < 0:
            raise ValueError(f"{label} must be >= 0 or a range like 100-1000.")
        return value
    text = str(value).strip()
    if allow_negative_one and text == "-1":
        return -1
    integer_match = INTEGER_RE.fullmatch(text)
    if integer_match:
        number = int(text, 10)
        if number < 0:
            raise ValueError(f"{label} must be >= 0 or a range like 100-1000.")
        return number
    range_match = NON_NEGATIVE_RANGE_RE.fullmatch(text)
    if not range_match:
        raise ValueError(f"{label} must be an integer or range like 100-1000.")
    start = int(range_match.group(1), 10)
    end = int(range_match.group(2), 10)
    if start > end:
        raise ValueError(f"{label} range start must be <= range end.")
    return f"{start}-{end}"


def _normalize_integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer.")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not INTEGER_RE.fullmatch(text):
        raise ValueError(f"{label} must be an integer.")
    return int(text, 10)


def _range_is_active(value: int | str | None) -> bool:
    if value is None:
        return False
    if isinstance(value, int):
        return value > 0
    return value.strip() not in ("", "0")


def normalize_xhttp_extra(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError("XHTTP extra must be a JSON object.")
    unknown = sorted(str(key) for key in set(value) - set(XHTTP_EXTRA_KEYS))
    if unknown:
        raise ValueError("Unsupported XHTTP extra field(s): " + ", ".join(unknown))

    result: dict[str, Any] = {}
    if not _is_empty_extra_value(value.get("xPaddingBytes")):
        result["xPaddingBytes"] = _normalize_non_negative_range(value["xPaddingBytes"], "xPaddingBytes")
    if not _is_empty_extra_value(value.get("scStreamUpServerSecs")):
        result["scStreamUpServerSecs"] = _normalize_non_negative_range(
            value["scStreamUpServerSecs"],
            "scStreamUpServerSecs",
            allow_negative_one=True,
        )

    raw_xmux = value.get("xmux")
    if not _is_empty_extra_value(raw_xmux):
        if not isinstance(raw_xmux, dict):
            raise ValueError("xmux must be a JSON object.")
        unknown_xmux = sorted(str(key) for key in set(raw_xmux) - set(XHTTP_XMUX_KEYS))
        if unknown_xmux:
            raise ValueError("Unsupported XMUX field(s): " + ", ".join(unknown_xmux))
        xmux: dict[str, Any] = {}
        for key in XHTTP_XMUX_KEYS:
            if _is_empty_extra_value(raw_xmux.get(key)):
                continue
            if key == "hKeepAlivePeriod":
                xmux[key] = _normalize_integer(raw_xmux[key], key)
            else:
                xmux[key] = _normalize_non_negative_range(raw_xmux[key], key)
        if _range_is_active(xmux.get("maxConcurrency")) and _range_is_active(xmux.get("maxConnections")):
            raise ValueError("XMUX maxConcurrency and maxConnections conflict; set only one of them above 0.")
        if xmux:
            result["xmux"] = xmux

    return result


def normalize_xhttp_extra_json(value: str | None) -> dict[str, Any]:
    text = (value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"XHTTP extra JSON is invalid: {exc}") from exc
    return normalize_xhttp_extra(parsed)


def xhttp_server_extra(extra: dict[str, Any] | None) -> dict[str, Any]:
    normalized = normalize_xhttp_extra(extra)
    return {key: normalized[key] for key in XHTTP_SERVER_EXTRA_KEYS if key in normalized}


def xhttp_extra_json(extra: dict[str, Any] | None) -> str:
    normalized = normalize_xhttp_extra(extra)
    if not normalized:
        return ""
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def reality_transport_settings_from_stream(stream: dict[str, Any]) -> dict[str, Any]:
    transport = normalize_reality_transport(stream.get("network", DEFAULT_REALITY_TRANSPORT))
    settings: dict[str, Any] = {"transport": transport}
    if transport == "grpc":
        grpc = stream.get("grpcSettings") or {}
        settings["grpcServiceName"] = normalize_grpc_service_name(grpc.get("serviceName"))
    elif transport == "xhttp":
        xhttp = stream.get("xhttpSettings") or {}
        settings["xhttpPath"] = normalize_xhttp_path(xhttp.get("path"))
        settings["xhttpMode"] = normalize_xhttp_mode(xhttp.get("mode"))
        extra = normalize_xhttp_extra(xhttp.get("extra") or {})
        if extra:
            settings["xhttpExtra"] = extra
    return settings


def connection_transport_settings_from_stream(stream: dict[str, Any]) -> dict[str, Any]:
    return reality_transport_settings_from_stream(stream)


def reality_transport_settings_from_inbound(inbound: dict[str, Any]) -> dict[str, Any]:
    return reality_transport_settings_from_stream(inbound.get("streamSettings", {}))


def connection_transport_settings_from_inbound(inbound: dict[str, Any]) -> dict[str, Any]:
    return connection_transport_settings_from_stream(inbound.get("streamSettings", {}))


def apply_reality_transport(
    stream: dict[str, Any],
    transport: str | None = None,
    *,
    grpc_service_name: str | None = None,
    xhttp_path: str | None = None,
    xhttp_mode: str | None = None,
    xhttp_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = normalize_reality_transport(transport)
    previous_xhttp = stream.get("xhttpSettings") or {}
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
        full_extra = normalize_xhttp_extra(previous_xhttp.get("extra") or {}) if xhttp_extra is None else normalize_xhttp_extra(xhttp_extra)
        settings = {"path": path, "mode": mode}
        server_extra = xhttp_server_extra(full_extra)
        if server_extra:
            settings["extra"] = server_extra
        stream["xhttpSettings"] = settings
        result: dict[str, Any] = {"transport": normalized, "xhttpPath": path, "xhttpMode": mode}
        if full_extra:
            result["xhttpExtra"] = full_extra
        return result
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
