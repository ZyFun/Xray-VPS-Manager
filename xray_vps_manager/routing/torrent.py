"""Изменение доступа к BitTorrent под блокировкой менеджера."""

from xray_vps_manager.core.locks import manager_lock
from xray_vps_manager.xray import torrent
from xray_vps_manager.xray.apply import ApplyResult, apply_config
from xray_vps_manager.xray.config import load_config


def set_blocked(blocked: bool) -> ApplyResult:
    with manager_lock(purpose="torrent"):
        config = load_config()
        if not torrent.set_blocked(config, blocked):
            return ApplyResult(False)
        return apply_config(config)
