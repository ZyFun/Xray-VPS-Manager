"""Модели Caddy и чистые операции над конфигурацией."""

import re
from dataclasses import dataclass
from pathlib import Path

DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+$")
TLS_VERSIONS = {"default", "tls1.2", "tls1.3"}
REVERSE_PROXY_RE = re.compile(
    r"reverse_proxy(?:\s+@[A-Za-z0-9_.-]+)?\s+(?P<scheme>h2c://)?127\.0\.0\.1:(?P<port>[0-9]+)"
)
PATH_MATCHER_RE = re.compile(r"^\s*@[A-Za-z0-9_.-]+\s+path\s+(?P<path>/\S+)\s*(?:#.*)?$", re.MULTILINE)
PROTOCOLS_RE = re.compile(r"protocols\s+(?P<min>tls1\.[23])\s+(?P<max>tls1\.[23])")
PROTOCOLS_LINE_RE = re.compile(r"^\s*protocols\s+tls1\.[23]\s+tls1\.[23]\s*(?:#.*)?$")
ROOT_DIRECTIVE_RE = re.compile(r"(?m)^\s*root\s+(?:\*\s+)?(?P<path>/\S+)")


@dataclass(frozen=True)
class SiteConfig:
    path: Path
    domain: str
    local_port: int | None
    tls_min_version: str
    tls_max_version: str
    modified_at: str = ""
    upstream_transport: str = "xhttp"
    match_path: str = ""


@dataclass(frozen=True)
class TlsVersionChoice:
    key: str
    label: str
    tls_min_version: str
    tls_max_version: str


@dataclass(frozen=True)
class SiteWriteResult:
    path: Path
    backup: Path | None


TLS_VERSION_CHOICES: tuple[TlsVersionChoice, ...] = (
    TlsVersionChoice("default", "Caddy default", "default", "default"),
    TlsVersionChoice("tls12", "TLS 1.2", "tls1.2", "tls1.2"),
    TlsVersionChoice("tls12_13", "TLS 1.2 + TLS 1.3", "tls1.2", "tls1.3"),
    TlsVersionChoice("tls13", "TLS 1.3", "tls1.3", "tls1.3"),
)


def validate_domain(value: str) -> str:
    domain = (value or "").strip().lower()
    if not domain or "/" in domain or ":" in domain or not DOMAIN_RE.fullmatch(domain):
        raise ValueError("TLS domain must be a domain without https://, path, or port.")
    return domain


def validate_http_path(value: str | None) -> str:
    path = (value or "").strip()
    if not path.startswith("/") or any(char.isspace() for char in path) or len(path) > 256:
        raise ValueError("HTTP path must start with /, contain no spaces, and be at most 256 chars.")
    return path


def caddy_fallback_block(domain: str) -> str:
    if domain.startswith("api."):
        return (
            "    handle {\n"
            "        header Content-Type application/json\n"
            "        respond \"{\\\"error\\\":\\\"not_found\\\"}\" 404\n"
            "    }\n"
        )
    return (
        "    handle {\n"
        "        root * /usr/share/caddy\n"
        "        try_files {path} /index.html\n"
        "        file_server\n"
        "    }\n"
    )


def normalize_tls_version(value: str | None, default: str = "tls1.2") -> str:
    version = (value or default).strip().lower()
    if version not in TLS_VERSIONS:
        raise ValueError("TLS version must be one of: default, tls1.2, tls1.3")
    return version


def normalize_tls_version_pair(tls_min_version: str | None, tls_max_version: str | None) -> tuple[str, str]:
    tls_min = normalize_tls_version(tls_min_version, "tls1.2")
    tls_max = normalize_tls_version(tls_max_version, tls_min)
    if "default" in (tls_min, tls_max) and (tls_min, tls_max) != ("default", "default"):
        raise ValueError("TLS default must be selected for both min and max versions.")
    if (tls_min, tls_max) == ("tls1.3", "tls1.2"):
        raise ValueError("TLS max version must not be lower than TLS min version.")
    return tls_min, tls_max


def tls_version_choice(value: str) -> TlsVersionChoice:
    key = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "caddy": "default",
        "auto": "default",
        "tls1.2": "tls12",
        "tls12": "tls12",
        "tls_12": "tls12",
        "tls1.2+tls1.3": "tls12_13",
        "tls1.2_1.3": "tls12_13",
        "tls12_13": "tls12_13",
        "tls1.3": "tls13",
        "tls13": "tls13",
        "tls_13": "tls13",
    }
    key = aliases.get(key, key)
    for choice in TLS_VERSION_CHOICES:
        if choice.key == key:
            return choice
    raise ValueError("TLS choice must be one of: " + ", ".join(choice.key for choice in TLS_VERSION_CHOICES))


def tls_version_label(tls_min_version: str | None, tls_max_version: str | None) -> str:
    tls_min, tls_max = normalize_tls_version_pair(tls_min_version, tls_max_version)
    for choice in TLS_VERSION_CHOICES:
        if (choice.tls_min_version, choice.tls_max_version) == (tls_min, tls_max):
            return choice.label
    return f"{tls_min}..{tls_max}"


def tls_version_choice_key(tls_min_version: str | None, tls_max_version: str | None) -> str:
    tls_min, tls_max = normalize_tls_version_pair(tls_min_version, tls_max_version)
    for choice in TLS_VERSION_CHOICES:
        if (choice.tls_min_version, choice.tls_max_version) == (tls_min, tls_max):
            return choice.key
    return ""


def line_brace_delta(line: str) -> int:
    return line.count("{") - line.count("}")


def first_site_block_line_span(lines: list[str]) -> tuple[int, int]:
    start = None
    depth = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if start is None:
            if not stripped or stripped.startswith("#"):
                continue
            if "{" in stripped:
                start = index
                depth = line_brace_delta(line)
                if depth <= 0:
                    return start, index
            continue
        depth += line_brace_delta(line)
        if depth <= 0:
            return start, index
    raise ValueError("Caddy site block not found.")


def top_level_tls_block_line_span(lines: list[str], site_start: int, site_end: int) -> tuple[int, int] | None:
    depth = line_brace_delta(lines[site_start])
    index = site_start + 1
    while index < site_end:
        stripped = lines[index].strip()
        if depth == 1 and re.fullmatch(r"tls\s*\{\s*(?:#.*)?", stripped):
            tls_start = index
            tls_depth = depth + line_brace_delta(lines[index])
            tls_end = index
            while tls_depth > 1 and tls_end < site_end:
                tls_end += 1
                tls_depth += line_brace_delta(lines[tls_end])
            if tls_depth == 1:
                return tls_start, tls_end
            raise ValueError("Caddy TLS block could not be parsed.")
        depth += line_brace_delta(lines[index])
        index += 1
    return None


def set_site_tls_versions_in_text(text: str, tls_min_version: str | None, tls_max_version: str | None) -> str:
    tls_min, tls_max = normalize_tls_version_pair(tls_min_version, tls_max_version)
    lines = text.splitlines(keepends=True)
    if not lines:
        raise ValueError("Caddy site config is empty.")

    site_start, site_end = first_site_block_line_span(lines)
    tls_span = top_level_tls_block_line_span(lines, site_start, site_end)
    tls_indent = "    "
    protocols_indent = "        "
    protocols_line = f"{protocols_indent}protocols {tls_min} {tls_max}\n"

    if tls_span is None:
        if tls_min == "default":
            return text
        lines[site_start + 1 : site_start + 1] = [
            f"{tls_indent}tls {{\n",
            protocols_line,
            f"{tls_indent}}}\n",
            "\n",
        ]
        return "".join(lines)

    tls_start, tls_end = tls_span
    if tls_min != "default":
        for index in range(tls_start + 1, tls_end):
            if PROTOCOLS_LINE_RE.fullmatch(lines[index].strip()):
                lines[index] = protocols_line
                return "".join(lines)
        lines[tls_start + 1 : tls_start + 1] = [protocols_line]
        return "".join(lines)

    block = list(lines[tls_start : tls_end + 1])
    block = [line for index, line in enumerate(block) if index in (0, len(block) - 1) or not PROTOCOLS_LINE_RE.fullmatch(line.strip())]
    meaningful_inner = [line for line in block[1:-1] if line.strip() and not line.strip().startswith("#")]
    if meaningful_inner:
        lines[tls_start : tls_end + 1] = block
        return "".join(lines)

    del lines[tls_start : tls_end + 1]
    if tls_start < len(lines) and not lines[tls_start].strip():
        del lines[tls_start]
    return "".join(lines)


def validate_port(value: int | str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("TLS random local port must be a number from 1 to 65535.") from exc
    if port < 1 or port > 65535:
        raise ValueError("TLS random local port must be a number from 1 to 65535.")
    return port


def site_filename(domain: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", validate_domain(domain)) + ".caddy"


def format_size(value: int) -> str:
    value = int(value or 0)
    if value < 1024:
        return f"{value}B"
    for suffix, size in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        next_size = size * 1024
        if value < next_size or suffix == "GB":
            return f"{value / size:.2f}{suffix}"
    return f"{value}B"


def first_site_address(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("{"):
            return stripped[:-1].strip()
    return ""


def first_path_matcher_path(text: str) -> str:
    inline_match = PATH_MATCHER_RE.search(text)
    if inline_match:
        return inline_match.group("path")

    lines = text.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not re.fullmatch(r"@[A-Za-z0-9_.-]+\s*\{", stripped):
            index += 1
            continue
        depth = line_brace_delta(lines[index])
        index += 1
        while index < len(lines) and depth > 0:
            inner = lines[index].strip()
            if inner.startswith("path "):
                parts = inner.split()
                if len(parts) >= 2:
                    return parts[1]
            depth += line_brace_delta(lines[index])
            index += 1
    return ""


def xhttp_route_match_path(route_path: str) -> str:
    path = validate_http_path(route_path)
    return path if path.endswith("*") else f"{path}*"


def normalized_site_match_path(path: str, upstream_transport: str) -> str:
    if upstream_transport == "xhttp" and path.endswith("*"):
        return path[:-1]
    return path


def format_site_config_conflict(site: SiteConfig) -> str:
    upstream = f"127.0.0.1:{site.local_port}" if site.local_port else "unknown upstream"
    route = f", path={site.match_path}" if site.match_path else ""
    return f"{site.path} ({upstream}, transport={site.upstream_transport}{route})"


def caddy_site_block(
    domain: str,
    local_port: int,
    *,
    tls_min_version: str = "tls1.2",
    tls_max_version: str = "tls1.2",
    upstream_transport: str = "xhttp",
    route_path: str = "",
) -> str:
    domain = validate_domain(domain)
    tls_min_version, tls_max_version = normalize_tls_version_pair(tls_min_version, tls_max_version)
    upstream_transport = (upstream_transport or "xhttp").strip().lower()
    if upstream_transport not in ("xhttp", "http"):
        raise ValueError("Caddy upstream transport must be xhttp or http.")
    if route_path:
        route_path = validate_http_path(route_path)
    tls_block = ""
    if tls_min_version != "default":
        tls_block = (
            "    tls {\n"
            f"        protocols {tls_min_version} {tls_max_version}\n"
            "    }\n\n"
        )
    upstream = f"h2c://127.0.0.1:{local_port}" if upstream_transport == "xhttp" else f"127.0.0.1:{local_port}"
    fallback_block = caddy_fallback_block(domain)
    if route_path and upstream_transport == "http":
        proxy_block = (
            "    @xray_path {\n"
            f"        path {route_path}\n"
            "        header Connection *Upgrade*\n"
            "        header Upgrade websocket\n"
            "    }\n"
            "    handle @xray_path {\n"
            f"        reverse_proxy {upstream}\n"
            "    }\n"
            "\n"
            f"{fallback_block}"
        )
    elif route_path:
        match_path = xhttp_route_match_path(route_path)
        proxy_block = (
            f"    @xray_path path {match_path}\n"
            "    handle @xray_path {\n"
            f"        reverse_proxy {upstream}\n"
            "    }\n"
            "\n"
            f"{fallback_block}"
        )
    else:
        proxy_block = f"    reverse_proxy {upstream}\n"
    return (
        f"{domain} {{\n"
        f"{tls_block}"
        f"{proxy_block}"
        "}\n"
    )
