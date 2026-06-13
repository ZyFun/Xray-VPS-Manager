"""Central filesystem paths used by the manager."""

from __future__ import annotations

from pathlib import Path

CONFIG_DIR = Path("/usr/local/etc/xray")
CONFIG_PATH = CONFIG_DIR / "config.json"
SERVER_ENV_PATH = CONFIG_DIR / "server.env"

XRAY_BIN = Path("/usr/local/bin/xray")
XRAY_ASSET_DIR = Path("/usr/local/share/xray")

SBIN_DIR = Path("/usr/local/sbin")
XRAY_CLIENT = SBIN_DIR / "xray-client"
XRAY_ACTIVITY = SBIN_DIR / "xray-activity"
XRAY_TELEGRAM = SBIN_DIR / "xray-telegram"
XRAY_TRAFFIC_SYNC = SBIN_DIR / "xray-traffic-sync"
XRAY_TEST = SBIN_DIR / "xray-test"

MANAGER_LIB_DIR = Path("/usr/local/lib/xray-vps-manager")
MANAGER_DB_PATH = CONFIG_DIR / "manager.db"
