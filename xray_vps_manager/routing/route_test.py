"""Временная проверка маршрута под блокировкой менеджера."""

from contextlib import contextmanager

from xray_vps_manager.core.locks import manager_lock
from xray_vps_manager.xray.apply import temporary_config


@contextmanager
def probe_config(config):
    with manager_lock(purpose="route probe"), temporary_config(config):
        yield
