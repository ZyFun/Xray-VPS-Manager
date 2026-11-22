"""Server settings helpers used by the Telegram admin panel."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xray_vps_manager.caddy import model as caddy_model
from xray_vps_manager.caddy import service as caddy_service
from xray_vps_manager.caddy import sites as caddy_sites


def tls_site_rows() -> list[dict[str, Any]]:
    rows = []
    for site in caddy_sites.list_site_configs():
        label = caddy_model.tls_version_label(site.tls_min_version, site.tls_max_version)
        rows.append(
            {
                "domain": site.domain,
                "path": str(site.path),
                "localPort": site.local_port,
                "tlsMinVersion": site.tls_min_version,
                "tlsMaxVersion": site.tls_max_version,
                "tlsChoice": caddy_model.tls_version_choice_key(site.tls_min_version, site.tls_max_version),
                "tlsLabel": label,
                "modifiedAt": site.modified_at or "-",
            }
        )
    return rows


def set_tls_site_version(
    domain: str,
    local_port: int,
    choice_key: str,
    site_path: str | Path | None = None,
    *,
    runner: caddy_service.CaddyCommandRunner | None = None,
) -> caddy_model.SiteWriteResult:
    choice = caddy_model.tls_version_choice(choice_key)
    if site_path:
        return caddy_service.update_site_tls_config(
            Path(site_path),
            tls_min_version=choice.tls_min_version,
            tls_max_version=choice.tls_max_version,
            runner=runner,
        )
    return caddy_service.update_site_config(
        domain,
        local_port,
        tls_min_version=choice.tls_min_version,
        tls_max_version=choice.tls_max_version,
        runner=runner,
    )
