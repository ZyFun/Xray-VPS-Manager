"""Shared constants for activity logging."""

from __future__ import annotations

import re
from pathlib import Path

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
SERVER_ENV_PATH = Path("/usr/local/etc/xray/server.env")
EXPORT_DIR = Path("/root/xray_activity_exports")
LOCK_PATH = Path("/usr/local/etc/xray/activity.lock")
ACCESS_LOG_PATH = Path("/var/log/xray/access.log")
ERROR_LOG_PATH = Path("/var/log/xray/error.log")
XRAY_BIN = Path("/usr/local/bin/xray")
GEOIP_PATHS = [
    Path("/usr/local/share/xray/geoip.dat"),
    Path("/usr/share/xray/geoip.dat"),
    Path("/usr/local/share/v2ray/geoip.dat"),
    Path("/usr/share/v2ray/geoip.dat"),
]

INBOUND_TAG = "vless-reality"
XRAY_GEOIP_OUTBOUND_PREFIX = "geoip-warning-"
DEFAULT_RETENTION_DAYS = 365
DEFAULT_ALERT_RETENTION_DAYS = 90
DEFAULT_XRAY_ERROR_RETENTION_DAYS = 180
DEFAULT_XRAY_ACCESS_LOG_RETENTION_DAYS = 180
DEFAULT_XRAY_ERROR_LOG_RETENTION_DAYS = 180
DEFAULT_XRAY_RAW_LOG_ROTATE_TIME = "03:00"
DETAIL_MODE_OFF = "off"
DETAIL_MODE_ALL = "all"
DETAIL_MODE_SELECTED = "selected"
DETAIL_MODES = {DETAIL_MODE_OFF, DETAIL_MODE_ALL, DETAIL_MODE_SELECTED}
SMTP_PORTS = {"25", "465", "587", "2525"}
ADMIN_PORTS = {"22", "23", "135", "139", "445", "3389", "5900"}
DEFAULT_RISK_BURST_EVENTS = 1000
DEFAULT_RISK_BURST_WINDOW_MINUTES = 15
DEFAULT_RISK_UNIQUE_HOSTS = 500
DEFAULT_RISK_UNIQUE_PORTS = 20

ACCESS_RE = re.compile(
    r"^(?P<time>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?\s+(?P<body>.*?)\s+email:\s+(?P<email>.+)$"
)
ROUTE_RE = re.compile(r"\[([^\]]+)\]")
TARGET_RE = re.compile(r"\b(?P<status>accepted|rejected)\s+(?P<target>(?P<network>tcp|udp):\S+)")
NETWORK_TARGET_RE = re.compile(r"\b(?P<target>(?P<network>tcp|udp):\S+)")
EXCEPTION_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:*?/-]+$")
