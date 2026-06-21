"""Server settings helpers used by the Telegram admin panel."""

from __future__ import annotations

import subprocess
from typing import Any

from xray_vps_manager.xray import caddy


def tls_site_rows() -> list[dict[str, Any]]:
    rows = []
    for site in caddy.list_site_configs():
        label = caddy.tls_version_label(site.tls_min_version, site.tls_max_version)
        rows.append(
            {
                "domain": site.domain,
                "path": str(site.path),
                "localPort": site.local_port,
                "tlsMinVersion": site.tls_min_version,
                "tlsMaxVersion": site.tls_max_version,
                "tlsChoice": caddy.tls_version_choice_key(site.tls_min_version, site.tls_max_version),
                "tlsLabel": label,
                "modifiedAt": site.modified_at or "-",
            }
        )
    return rows


def set_tls_site_version(domain: str, local_port: int, choice_key: str) -> caddy.SiteWriteResult:
    choice = caddy.tls_version_choice(choice_key)
    return caddy.update_site_config(
        domain,
        local_port,
        tls_min_version=choice.tls_min_version,
        tls_max_version=choice.tls_max_version,
        runner=subprocess.run,
    )
