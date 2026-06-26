"""Parsers for Xray outbound links."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

from xray_vps_manager.xray import config as xray_config


def one(params: dict[str, list[str]], key: str, default: str = "") -> str:
    value = params.get(key, [default])
    return value[0] if value else default


def parse_vless_outbound(uri: str, tag: str) -> tuple[dict, str]:
    parsed = urlparse(uri.strip())
    if parsed.scheme.lower() != "vless":
        raise ValueError("Only vless:// links are supported.")
    if not parsed.username or not parsed.hostname:
        raise ValueError("The VLESS link must include UUID and host.")

    params = parse_qs(parsed.query, keep_blank_values=True)
    port = parsed.port or 443
    network = (one(params, "type", "tcp") or "tcp").lower()
    security = (one(params, "security", "none") or "none").lower()
    encryption = one(params, "encryption", "none") or "none"
    flow = one(params, "flow", "")

    outbound = {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": parsed.hostname,
                    "port": port,
                    "users": [
                        {
                            "id": unquote(parsed.username),
                            "encryption": encryption,
                        }
                    ],
                }
            ]
        },
        "streamSettings": {
            "network": network,
            "security": security,
        },
    }

    user = outbound["settings"]["vnext"][0]["users"][0]
    if flow:
        user["flow"] = flow

    stream = outbound["streamSettings"]
    if security == "reality":
        public_key = one(params, "pbk")
        sni = one(params, "sni")
        short_id = one(params, "sid")
        if not public_key or not sni:
            raise ValueError("Reality VLESS requires pbk and sni parameters.")
        reality = {
            "serverName": sni,
            "publicKey": public_key,
            "fingerprint": one(params, "fp", "chrome") or "chrome",
        }
        if short_id:
            reality["shortId"] = short_id
        spider_x = one(params, "spx", "")
        if spider_x:
            reality["spiderX"] = unquote(spider_x)
        stream["realitySettings"] = reality
    elif security == "tls":
        tls = {}
        sni = one(params, "sni")
        if sni:
            tls["serverName"] = sni
        fp = one(params, "fp")
        if fp:
            tls["fingerprint"] = fp
        stream["tlsSettings"] = tls
    elif security in ("none", ""):
        stream["security"] = "none"
    else:
        raise ValueError(f"Unsupported security={security!r}; supported: reality, tls, none.")

    if network == "tcp":
        header_type = one(params, "headerType", "none") or "none"
        stream["tcpSettings"] = {"header": {"type": header_type}}
    elif network == "ws":
        ws = {}
        path = one(params, "path")
        host = one(params, "host")
        if path:
            ws["path"] = unquote(path)
        if host:
            ws["headers"] = {"Host": host}
        stream["wsSettings"] = ws
    elif network == "grpc":
        service_name = one(params, "serviceName")
        grpc = {}
        if service_name:
            grpc["serviceName"] = unquote(service_name)
        stream["grpcSettings"] = grpc
    elif network == "xhttp":
        path = one(params, "path")
        mode = one(params, "mode")
        extra = one(params, "extra")
        xhttp = {}
        if path:
            xhttp["path"] = unquote(path)
        if mode:
            xhttp["mode"] = mode
        if extra:
            xhttp["extra"] = xray_config.normalize_xhttp_extra_json(extra)
        stream["xhttpSettings"] = xhttp
    else:
        raise ValueError(f"Unsupported network type={network!r}; supported: tcp, ws, grpc, xhttp.")

    label = unquote(parsed.fragment) if parsed.fragment else f"{parsed.hostname}:{port}"
    return outbound, label
