"""Wireless setup, transport isolation, authorization and backup safety regressions."""

import asyncio
import os
import plistlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


REPOSITORY = Path(__file__).resolve().parents[1]
TEMPORARY = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
ROOT = Path(TEMPORARY.name)
os.environ.update({
    "BACKUPS_DIR": str(ROOT / "backups"), "EXPORTS_DIR": str(ROOT / "exports"),
    "PDFS_DIR": str(ROOT / "pdfs"), "CONFIG_DIR": str(ROOT / "config"),
    "APP_PASSWORD": "change-me-before-lan-use", "SCHEDULE_ENABLED": "0",
})
sys.path.insert(0, str(REPOSITORY / "app"))
import app as module
import wifi_setup

UDID = "00008140-TEST000000000000"
RECORD = {"HostID": "test-host", "SystemBUID": "test-buid",
          "HostCertificate": b"test-cert", "HostPrivateKey": b"private-test-key"}


def wait_job():
    for _ in range(500):
        result = module.jobs.snapshot()
        if not result["busy"]:
            return result
        time.sleep(0.01)
    raise AssertionError("Wi-Fi job did not finish")


class DeviceTests(unittest.TestCase):
    def setUp(self):
        module.clear_device_cache()

    def test_network_list_excludes_usb_and_suffixes(self):
        with patch.object(module, "WIFI_USBMUXD_SOCKET", "/test/wifi.sock"), \
             patch.object(module, "quick_command", return_value=(0, f"{UDID}\n{UDID}\nUSB-ID (USB)\nnoise!")) as run:
            self.assertEqual(module.list_devices(True), [UDID])
            self.assertEqual(run.call_args.args[0], ["idevice_id", "-n"])
            self.assertEqual(run.call_args.kwargs["env"]["USBMUXD_SOCKET_ADDRESS"], "UNIX:/test/wifi.sock")
            module.list_devices(True)
            self.assertEqual(run.call_count, 1)

    def test_usb_keeps_existing_mux_and_separate_cache(self):
        with patch.object(module, "WIFI_USBMUXD_SOCKET", "/test/wifi.sock"), \
             patch.object(module, "quick_command", return_value=(0, UDID)) as run:
            module.list_devices(False)
            module.list_devices(True)
            self.assertEqual(run.call_args_list[0].args[0], ["idevice_id", "-l"])
            self.assertIsNone(run.call_args_list[0].kwargs["env"])
            self.assertEqual(run.call_count, 2)

    def test_missing_sidecar_does_not_return_usb(self):
        with patch.object(module, "quick_command", return_value=(124, "timeout")):
            self.assertEqual(module.list_devices(True), [])

    def test_environment_does_not_mutate_password_or_global_env(self):
        env = {"BACKUP_PASSWORD": "secret-test-password", "USBMUXD_SOCKET_ADDRESS": "USB-SOCKET"}
        with patch.object(module, "WIFI_USBMUXD_SOCKET", "/test/wifi.sock"):
            result = module.device_environment(True, env)
            self.assertEqual(result["BACKUP_PASSWORD"], env["BACKUP_PASSWORD"])
            self.assertEqual(env["USBMUXD_SOCKET_ADDRESS"], "USB-SOCKET")
            self.assertIs(module.device_environment(False, env), env)

    def test_enable_usb_only_and_does_not_start_backup(self):
        client = module.app.test_client()
        before = list(module.BACKUPS_DIR.rglob("*"))
        with patch.object(module, "list_devices", side_effect=lambda network=False, refresh=False: [] if network else [UDID]), \
             patch.object(module, "WIFI_SETUP_PYTHON", Path(sys.executable)), \
             patch.object(module.jobs, "run_process") as run:
            self.assertEqual(client.post("/device/wifi/enable").status_code, 302)
            self.assertEqual(wait_job()["status"], "success")
            args = run.call_args.args[0]
            self.assertEqual(args[-3:], ["enable", "--udid", UDID])
            self.assertNotIn("idevicebackup2", args)
            self.assertEqual(client.post("/device/wifi/enable", data={"device_id": "other"}).status_code, 400)
        self.assertEqual(list(module.BACKUPS_DIR.rglob("*")), before)

    def test_ambiguous_usb_requires_selection(self):
        with patch.object(module, "list_devices", return_value=[UDID, "OTHER-DEVICE"]):
            self.assertEqual(module.app.test_client().post("/device/wifi/enable").status_code, 400)

    def test_busy_job_rejects_wifi_setup(self):
        with patch.object(module, "list_devices", return_value=[UDID]), \
             patch.object(module, "WIFI_SETUP_PYTHON", Path(sys.executable)), \
             patch.object(module.jobs, "start", return_value=False), \
             patch.object(module.jobs, "run_process") as run:
            module.app.test_client().post("/device/wifi/enable")
            run.assert_not_called()

    def test_network_trust_check_is_read_only_and_no_usb_fallback(self):
        with patch.object(module, "list_devices", return_value=[UDID]), \
             patch.object(module, "WIFI_USBMUXD_SOCKET", "/test/wifi.sock"), \
             patch.object(module, "quick_command", return_value=(0, "26.0")) as run, \
             patch.object(module.jobs, "run_process") as backup:
            module.app.test_client().post("/device/wifi/check")
            self.assertEqual(wait_job()["status"], "success")
            self.assertEqual(run.call_args.args[0], ["ideviceinfo", "-n", "-u", UDID, "-k", "ProductVersion"])
            self.assertEqual(run.call_args.kwargs["timeout"], 15)
            self.assertEqual(run.call_args.kwargs["env"]["USBMUXD_SOCKET_ADDRESS"], "UNIX:/test/wifi.sock")
            backup.assert_not_called()

    def test_no_network_device_cannot_check_usb(self):
        with patch.object(module, "list_devices", return_value=[]), \
             patch.object(module, "quick_command") as run:
            module.app.test_client().post("/device/wifi/check", data={"device_id": UDID})
            run.assert_not_called()

    def test_failed_network_check_is_not_reported_as_success(self):
        with patch.object(module, "list_devices", return_value=[UDID]), \
             patch.object(module, "quick_command", return_value=(1, "locked")):
            module.app.test_client().post("/device/wifi/check")
            self.assertEqual(wait_job()["status"], "error")

    def test_backup_and_encryption_use_network_socket(self):
        with patch.object(module, "list_devices", return_value=[UDID]), \
             patch.object(module, "WIFI_USBMUXD_SOCKET", "/test/wifi.sock"), \
             patch.object(module.jobs, "run_process") as run:
            module.run_iphone_backup(False, True, UDID)
            self.assertEqual(run.call_args.kwargs["env"]["USBMUXD_SOCKET_ADDRESS"], "UNIX:/test/wifi.sock")
            module.app.test_client().post("/encryption", data={
                "device_target": "network:" + UDID,
                "backup_password": "password", "backup_password_confirm": "password",
            })
            self.assertEqual(wait_job()["status"], "success")
            self.assertEqual(run.call_args.kwargs["env"]["BACKUP_PASSWORD"], "password")
            self.assertEqual(run.call_args.kwargs["env"]["USBMUXD_SOCKET_ADDRESS"], "UNIX:/test/wifi.sock")


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.records = ROOT / "records"
        self.records.mkdir(exist_ok=True)
        self.path = self.records / (UDID + ".plist")
        self.path.write_bytes(plistlib.dumps(RECORD))
        self.before = self.path.read_bytes()
        self.client = AsyncMock()
        self.client.__aenter__.return_value = self.client
        self.client.udid = UDID
        self.client.paired = True
        self.client.get_enable_wifi_connections.return_value = True
        self.factory = AsyncMock(return_value=self.client)

    async def test_enable_changes_only_wifi_flag_and_keeps_trust_record(self):
        self.assertTrue(await wifi_setup.configure(UDID, True, self.records, self.factory))
        self.client.set_enable_wifi_connections.assert_awaited_once_with(True)
        options = self.factory.call_args.kwargs
        self.assertEqual(options["connection_type"], "USB")
        self.assertFalse(options["autopair"])
        self.assertEqual(options["pair_record"], RECORD)
        self.assertFalse(options["pairing_records_cache_folder"].exists())
        self.assertEqual(self.path.read_bytes(), self.before)
        self.client.__aexit__.assert_awaited_once()

    async def test_status_does_not_write_phone(self):
        await wifi_setup.configure(UDID, False, self.records, self.factory)
        self.client.set_enable_wifi_connections.assert_not_awaited()

    async def test_untrusted_phone_cannot_enable(self):
        self.client.paired = False
        with self.assertRaisesRegex(ValueError, "trust validation failed"):
            await wifi_setup.configure(UDID, True, self.records, self.factory)
        self.client.set_enable_wifi_connections.assert_not_awaited()

    async def test_wrong_phone_cannot_enable(self):
        self.client.udid = "OTHER-DEVICE"
        with self.assertRaises(ValueError):
            await wifi_setup.configure(UDID, True, self.records, self.factory)
        self.client.set_enable_wifi_connections.assert_not_awaited()

    async def test_missing_record_does_not_pair(self):
        with self.assertRaisesRegex(ValueError, "No saved USB trust"):
            await wifi_setup.configure("UNKNOWN-DEVICE", True, self.records, self.factory)
        self.factory.assert_not_awaited()

    async def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            await wifi_setup.configure("../other", True, self.records, self.factory)
        self.factory.assert_not_awaited()

    async def test_failed_flag_readback_is_error(self):
        self.client.get_enable_wifi_connections.return_value = False
        with self.assertRaisesRegex(ValueError, "did not enable"):
            await wifi_setup.configure(UDID, True, self.records, self.factory)


if __name__ == "__main__":
    unittest.main()
