import json
from pathlib import Path

from xray_vps_manager.db import json_import


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def make_json_state_paths(root: Path) -> json_import.JsonStatePaths:
    return json_import.JsonStatePaths(
        clients=root / "clients.json",
        traffic=root / "traffic.json",
        activity=root / "activity.json",
        activity_exceptions=root / "activity-exceptions.json",
        activity_dir=root / "activity",
        client_activity_dir=root / "activity" / "clients",
        telegram=root / "telegram-bot.json",
    )


def write_json_state_fixture(root: Path) -> json_import.JsonStatePaths:
    paths = make_json_state_paths(root)
    write_json(
        paths.clients,
        {
            "connections": {
                "vless-reality": {
                    "tag": "vless-reality",
                    "name": "default",
                    "created": "2026-06-12T08:00:00Z",
                    "port": 443,
                    "sni": "example.com",
                    "dest": "example.com:443",
                    "fingerprint": "chrome",
                    "publicKey": "pub",
                    "shortId": "abcd",
                }
            },
            "clients": {
                "alice": {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "created": "2026-06-12T08:01:00Z",
                    "enabled": True,
                    "connection": "vless-reality",
                    "paymentType": "paid",
                    "trafficLimit": {
                        "period": "daily",
                        "bytes": 1073741824,
                        "setAt": "2026-06-12T08:02:00Z",
                    },
                    "client": {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "flow": "xtls-rprx-vision",
                        "email": "alice|created=2026-06-12T08:01:00Z",
                    },
                }
            },
        },
    )
    write_json(
        paths.traffic,
        {
            "version": 2,
            "historyRetentionMonths": 6,
            "updated": "2026-06-12T08:03:00Z",
            "accessLog": {
                "path": "/var/log/xray/access.log",
                "inode": 1,
                "offset": 123,
                "updated": "2026-06-12T08:03:00Z",
            },
            "clients": {
                "alice": {
                    "email": "alice|created=2026-06-12T08:01:00Z",
                    "incoming": 100,
                    "outgoing": 200,
                    "last": {"uplink": 100, "downlink": 200},
                    "history": {"2026-06-12": {"08": {"incoming": 100, "outgoing": 200}}},
                },
                "stale": {"incoming": 1, "outgoing": 1},
            },
        },
    )
    write_json(
        paths.activity,
        {
            "version": 1,
            "enabled": True,
            "retentionDays": 365,
            "lastSync": "2026-06-12T08:04:00Z",
            "clients": {"alice": {"totalEvents": 1}},
            "accessLog": {
                "path": "/var/log/xray/access.log",
                "inode": 2,
                "offset": 456,
                "updated": "2026-06-12T08:04:00Z",
            },
        },
    )
    write_json(
        paths.activity_exceptions,
        {
            "version": 1,
            "items": [
                {"value": "*.example.com", "kind": "mask", "source": "manual", "createdAt": "2026-06-12T08:05:00Z"}
            ],
        },
    )
    paths.client_activity_dir.mkdir(parents=True, exist_ok=True)
    (paths.client_activity_dir / "alice.jsonl").write_text(
        json.dumps(
            {
                "time": "2026-06-12T08:06:00Z",
                "client": "alice",
                "email": "alice|created=2026-06-12T08:01:00Z",
                "connection": "vless-reality",
                "host": "example.com",
                "port": "443",
                "outbound": "cascade-upstream",
                "risks": ["xray-geoip:RU"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
        + json.dumps({"time": "2026-06-12T08:07:00Z", "client": "stale"}, separators=(",", ":"))
        + "\n"
    )
    write_json(
        paths.telegram,
        {
            "version": 1,
            "enabled": True,
            "token": "secret",
            "botName": "Vireika",
            "chatId": "123",
            "chatLabel": "owner",
            "routeMode": "direct",
            "paymentTotalAmount": "500",
            "paymentCurrency": "₽",
            "paymentRoundingMode": "none",
            "paymentRoundingStep": "10",
            "geoipState": {"sentIds": []},
            "clientSubscriptionState": {"userUpdateOffset": 10},
            "dailySummaryState": {"lastSentDay": "2026-06-11"},
            "adminState": {"mode": "idle"},
            "clientSubscriptions": {
                "123": {
                    "client": "alice",
                    "clientId": "00000000-0000-0000-0000-000000000001",
                    "connection": "vless-reality",
                    "chatLabel": "owner",
                    "linkHash": "hash",
                    "subscribedAt": "2026-06-12T08:08:00Z",
                    "enabled": True,
                }
            },
        },
    )
    return paths
