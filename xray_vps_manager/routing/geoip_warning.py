"""Настройка GeoIP-предупреждений с компенсацией server.env."""

from xray_vps_manager.core import settings
from xray_vps_manager.core.compensation import compensate
from xray_vps_manager.core.locks import manager_lock
from xray_vps_manager.xray import bypass
from xray_vps_manager.xray.apply import apply_config, restore_config_backup
from xray_vps_manager.xray.config import ensure_api_rule_first, load_config


def set_region(code: str, *, required_env_keys=()):
    with manager_lock(purpose="GeoIP warning"):
        config = load_config()
        original_env = settings.env_bytes()
        values = settings.read_env()
        previous = values.get("ACTIVITY_XRAY_GEOIP_WARNING_CODE", "")
        if previous and previous.upper() != code.upper() and not bypass.configured_bypass_for_warning(config, previous):
            bypass.remove_geoip_warning_route(config, previous)
        if code:
            code = bypass.normalize_region_code(code)
            bypass.ensure_geoip_domain_strategy(config, values)
            source = bypass.warning_source_outbound(config, code)
            bypass.ensure_geoip_warning_outbound(config, code, source)
            bypass.ensure_geoip_warning_rule(config, code)
        else:
            if not previous:
                for tag in bypass.warning_tags(config):
                    try:
                        region = bypass.region_from_geoip_warning_tag(tag)
                    except ValueError:
                        continue
                    if not bypass.configured_bypass_for_warning(config, region):
                        bypass.remove_geoip_warning_route(config, region)
            bypass.restore_geoip_domain_strategy_if_unused(config, values)
        values["ACTIVITY_XRAY_GEOIP_WARNING_CODE"] = code
        for key in required_env_keys:
            values.setdefault(key, "")
        ensure_api_rule_first(config)
        applied = apply_config(config)
        try:
            settings.write_env(values)
        except BaseException as error:
            compensate(error, [
                ("Конфигурация Xray", lambda: restore_config_backup(applied.backup)),
                ("server.env", lambda: settings.restore_env(original_env)),
            ])
        return applied
