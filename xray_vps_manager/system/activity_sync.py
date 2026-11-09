"""Периодический парсер и последующее согласование blocklist."""

from xray_vps_manager.activity.sync import sync_activity
from xray_vps_manager.core.errors import ManagerError
from xray_vps_manager.core.locks import activity_lock
from xray_vps_manager.routing.blocklist import reconcile_xray_config


def sync(log):
    with activity_lock(purpose="activity sync"):
        result = sync_activity(log)
    try:
        backup = reconcile_xray_config()
        if backup:
            log(f"Activity blocklist routing synced. Backup: {backup}")
    except ManagerError:
        # Граница CLI сохраняет код отмены и подробности даже при --quiet.
        raise
    except Exception as error:
        log(f"Activity blocklist routing sync failed: {error}")
        return 1
    return result
