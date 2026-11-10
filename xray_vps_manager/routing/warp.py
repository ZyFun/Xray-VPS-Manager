"""Пробы WARP и правила для управляемой записи Cloudflare API в /etc/hosts."""

from __future__ import annotations

from collections.abc import Callable
from itertools import islice
import socket
from subprocess import TimeoutExpired
import time

from xray_vps_manager.core import process
from xray_vps_manager.core.errors import ApplyConfigError, ConflictError, ManagerError
from xray_vps_manager.routing.cascade import apply_route_config
from xray_vps_manager.xray.apply import ApplyResult, apply_config_after_probe
from xray_vps_manager.xray.config import ensure_api_rule_first
from xray_vps_manager.xray.warp import (
    SOCKS_HOST, VERIFY_SOCKS_PORT, disabled_probe_config, endpoint_probe_config, endpoint_probe_ports,
    outbound_at_endpoint, remove_endpoint_probe_artifacts,
)


def ipv4_endpoint_candidates(value: str) -> list[str]:
    endpoint = value.strip()
    if not endpoint or endpoint.startswith("[") or endpoint.count(":") != 1:
        return []
    host, port = endpoint.rsplit(":", 1)
    try:
        socket.inet_aton(host)
        return []
    except OSError:
        pass
    candidates = []
    try:
        for item in socket.getaddrinfo(host, int(port), family=socket.AF_INET, type=socket.SOCK_DGRAM):
            ip = item[4][0]
            if ip:
                candidates.append(f"{ip}:{port}")
    except OSError:
        pass
    if host == "engage.cloudflareclient.com":
        candidates.extend(f"{ip}:{port}" for ip in ("162.159.192.1", "162.159.193.1"))
    return list(dict.fromkeys(candidates))


def _trace(port: int) -> tuple[str, str]:
    try:
        result = process.run(
            ["curl", "-4", "--proxy", f"socks5h://{SOCKS_HOST}:{port}", "--connect-timeout", "8",
             "--max-time", "20", "-sS", "https://www.cloudflare.com/cdn-cgi/trace"],
            timeout=25,
        )
    except (TimeoutExpired, OSError) as error:
        return "", str(error)
    output = result.stdout.strip()
    if result.returncode or not output:
        return "", result.stderr.strip() or output or f"curl exited with {result.returncode}"
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    state = fields.get("warp", "")
    return state, f"ip={fields.get('ip', '?')} warp={state or '?'}"


def trace_normal_route() -> tuple[bool, str, bool]:
    """Проверить обычный маршрут; подтверждением выключения служит только warp=off."""
    state, detail = _trace(VERIFY_SOCKS_PORT)
    if state in ("on", "plus"):
        return True, detail, True
    if state == "off":
        return True, detail, False
    return False, detail, False


def apply_disabled_route(config: dict, db) -> tuple[ApplyResult, tuple[bool, str, bool]]:
    """Проверить выключенный маршрут и сохранить его даже при отрицательном trace."""
    trace_result = (False, "No trace result.", False)

    def apply(candidate: dict) -> ApplyResult:
        def finalize() -> dict:
            nonlocal trace_result
            trace_result = trace_normal_route()
            return candidate

        def probe_failed(error: ApplyConfigError) -> dict:
            nonlocal trace_result
            trace_result = (False, f"Temporary Xray did not start: {error.detail}", False)
            return candidate

        return apply_config_after_probe(disabled_probe_config(candidate), finalize,
                                        on_probe_start_failure=probe_failed, reason="WARP disable")

    return apply_route_config(config, db, apply=apply), trace_result


def apply_endpoint_route(config: dict, outbound: dict, configure: Callable[[dict], None], *,
                         required: bool, progress: Callable[[str], None]) -> tuple[str, ApplyResult]:
    """Проверить профиль и IPv4 в одном запуске, применить только выбранный endpoint."""
    profile = outbound["settings"]["peers"][0]["endpoint"]
    endpoints = [profile, *ipv4_endpoint_candidates(profile)]
    remove_endpoint_probe_artifacts(config)
    available = (port for port in endpoint_probe_ports(config) if not process.tcp_port_in_use(port, SOCKS_HOST))
    ports = list(islice(available, len(endpoints)))
    if len(ports) != len(endpoints):
        raise ConflictError("No unused SOCKS ports for WARP endpoint probes.")
    probe = endpoint_probe_config(config, outbound, endpoints, ports)
    selected = profile

    def finalize() -> dict:
        nonlocal selected
        for index, (endpoint, port) in enumerate(zip(endpoints, ports, strict=True)):
            label = "endpoint from profile" if index == 0 else "IPv4 fallback endpoint"
            progress(f"Testing WARP {label}: {endpoint}")
            for attempt in range(2):
                state, detail = _trace(port)
                if state in ("on", "plus"):
                    progress(f"OK {endpoint} -> {detail}")
                    selected = endpoint
                    break
                if attempt == 0:
                    time.sleep(1.0)
            else:
                progress(f"FAIL {endpoint} -> {detail}")
                continue
            break
        else:
            message = "No working WARP endpoint found."
            if required:
                raise ManagerError(message)
            progress(f"WARN: {message} Keeping profile endpoint in config; do not enable WARP until test passes.")
        configure(outbound_at_endpoint(outbound, selected))
        ensure_api_rule_first(config)
        return config

    applied = apply_config_after_probe(probe, finalize, reason="WARP endpoint")
    return selected, applied


def hosts_line_contains_name(line: str, name: str) -> bool:
    """Host names are case-insensitive, as in the system resolver."""
    parts = line.split("#", 1)[0].split()
    return name.lower() in (part.lower() for part in parts[1:])


def is_managed_hosts_line(line: str, comment: str) -> bool:
    return line.rstrip().endswith(comment)


def foreign_hosts_lines(lines: list[str], name: str, comment: str) -> list[str]:
    """Lines resolving ``name`` that the manager did not write."""
    return [line for line in lines if hosts_line_contains_name(line, name) and not is_managed_hosts_line(line, comment)]


def managed_hosts_line(ip: str, name: str, comment: str) -> str:
    return f"{ip} {name} {comment}"


def unpin_hosts_lines(lines: list[str], comment: str) -> list[str]:
    return [line for line in lines if not is_managed_hosts_line(line, comment)]


def pin_hosts_lines(lines: list[str], ip: str, name: str, comment: str) -> list[str]:
    """Replace the managed pin with ``ip``; all other lines stay in place."""
    return [*unpin_hosts_lines(lines, comment), managed_hosts_line(ip, name, comment)]
