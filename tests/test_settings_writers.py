"""Наблюдаемые результаты писателей настроек до перевода на update_env."""
import unittest
from xray_vps_manager.activity import controls as activity
from xray_vps_manager.traffic import controls as traffic
from xray_vps_manager.commands import menu_status
from tests.support.sandbox import Sandbox


class SettingsWriterTests(unittest.TestCase):
    def test_traffic_updates_preserve_unknown_keys_and_defaults(self):
        with Sandbox() as box:
            box.paths.server_env.write_text('CUSTOM=keep\n')
            traffic.set_total_multiplier('1.5')
            self.assertEqual(box.paths.server_env.read_text(), 'TRAFFIC_TOTAL_MULTIPLIER_ENABLED=false\nTRAFFIC_TOTAL_MULTIPLIER=1.5\nCUSTOM=keep\n')
            traffic.set_total_multiplier_enabled(True)
            self.assertEqual(box.paths.server_env.read_text(), 'TRAFFIC_TOTAL_MULTIPLIER_ENABLED=true\nTRAFFIC_TOTAL_MULTIPLIER=1.5\nCUSTOM=keep\n')

    def test_activity_update_keeps_values_and_populates_same_defaults(self):
        with Sandbox() as box:
            box.paths.server_env.write_text('CUSTOM=keep\nACTIVITY_RETENTION_DAYS=42\n')
            activity.set_enabled(True)
            self.assertEqual(box.paths.server_env.read_text(),
                'ACTIVITY_LOGGING_ENABLED=true\nACTIVITY_RETENTION_DAYS=42\n'
                'ACTIVITY_RISK_BURST_EVENTS=1000\nACTIVITY_RISK_BURST_WINDOW_MINUTES=15\n'
                'ACTIVITY_RISK_UNIQUE_HOSTS=500\nACTIVITY_RISK_UNIQUE_PORTS=20\n'
                'ACTIVITY_XRAY_GEOIP_WARNING_CODE=\nACTIVITY_ALERTS_ENABLED=true\n'
                'ACTIVITY_ALERT_RETENTION_DAYS=90\nXRAY_ERROR_EVENT_RETENTION_DAYS=180\n'
                'XRAY_ACCESS_LOG_RETENTION_DAYS=180\nXRAY_ERROR_LOG_RETENTION_DAYS=180\n'
                'XRAY_RAW_LOG_ROTATE_TIME=03:00\nCUSTOM=keep\n')

    def test_security_audit_keeps_menu_required_keys_and_order(self):
        with Sandbox(now='2026-06-12T08:00:00Z') as box:
            box.paths.server_env.write_text('CUSTOM=keep\n')
            menu_status.record_security_audit_run()
            self.assertEqual(box.paths.server_env.read_text(),
                'SERVER_ADDR=\nSERVER_NAME=\nPORT=\nREALITY_SNI=\nREALITY_DEST=\nFINGERPRINT=\n'
                'MANAGER_TIMEZONE=\nSECURITY_AUDIT_LAST_RUN=2026-06-12T08:00:00Z\nCUSTOM=keep\n')
