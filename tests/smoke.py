import json
import os
import plistlib
import sqlite3
import sys
import tempfile
import time
import zipfile
from contextlib import closing
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
TEMPORARY = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
ROOT = Path(TEMPORARY.name)
os.environ.update(
    {
        "BACKUPS_DIR": str(ROOT / "backups"),
        "EXPORTS_DIR": str(ROOT / "exports"),
        "PDFS_DIR": str(ROOT / "pdfs"),
        "CONFIG_DIR": str(ROOT / "config"),
        "APP_PASSWORD": "change-me-before-lan-use",
        "LIBRARY_CHUNK_MESSAGES": "50",
        "SCHEDULE_ENABLED": "0",
    }
)
sys.path.insert(0, str(REPOSITORY / "app"))

import app as module
from pypdf import PdfReader, PdfWriter


def wait_for_job() -> dict:
    for _ in range(500):
        snapshot = module.jobs.snapshot()
        if not snapshot["busy"]:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("background job did not finish")


export_root = module.CURRENT_EXPORT_DIR
attachments = export_root / "attachments"
attachments.mkdir(parents=True, exist_ok=True)
(attachments / "test.jpg").write_bytes(b"test image bytes")
messages = "".join(
    f"""<div class="message"><div class="sent">
    <span class="timestamp">Jan {index % 28 + 1}, 2025</span>
    <span class="sender">Me</span>
    <div class="message_part"><span class="bubble">Indexed test message {index}</span></div>
    {'<img src="attachments/test.jpg">' if index == 0 else ''}
    </div></div>"""
    for index in range(125)
)
(export_root / "large.html").write_text(
    '<html><head><style>.message{display:block}</style></head><body>'
    + messages
    + "</body></html>",
    encoding="utf-8",
)

backup = module.BACKUPS_DIR / "latest" / "TEST-BACKUP"
backup.mkdir(parents=True, exist_ok=True)
for name, value in (
    ("Info.plist", {"Device Name": "Test iPhone", "Product Version": "18.0"}),
    ("Manifest.plist", {"IsEncrypted": False}),
    ("Status.plist", {"SnapshotState": "finished"}),
):
    with (backup / name).open("wb") as handle:
        plistlib.dump(value, handle)
with closing(sqlite3.connect(backup / "Manifest.db")) as connection:
    connection.execute("CREATE TABLE Files(fileID TEXT)")
    connection.execute("INSERT INTO Files(fileID) VALUES ('test')")
    connection.commit()

result = module.catalog.sync(["large.html"])
record = module.catalog.conversation("large.html")
assert result["indexed"] == 1
assert record and record["message_count"] == 125 and record["chunk_count"] == 3
assert module.catalog.search("message 124")[0]["chunk_index"] == 3
diagnostics = module.diagnostic_summary(
    """Total messages: 125
Date range: Jan 1, 2025 to Jan 2, 2025
Data present on disk: 1.5 GB
Missing files: 2 (1%)
Total chats: 1
Handles with resolved names: 1/1 (100%)"""
)
assert dict(diagnostics)["Contacts resolved"] == "1/1 (100%)"
assert dict(diagnostics)["Missing attachments"] == "2 (1%)"

module.list_devices = lambda network=False, refresh=False: []
client = module.app.test_client()
page = client.get("/?conversation=large.html&page=2&q=message+124")
assert page.status_code == 200
assert b"Part 2 of 3" in page.data
assert b"Reliable parts" in page.data
assert b"Indexed test [message] [124]" in page.data
assert client.get("/csv/large.html").data.startswith(b"\xef\xbb\xbfsequence")
client.post("/preflight")
assert wait_for_job()["phase"] == "Preflight complete"
client.post("/backup/verify", data={"backup_path": str(backup.resolve())})
verification = wait_for_job()
assert verification["status"] == "success"
assert "Manifest database: OK" in verification["log"]

render_calls = []
fail_second_part = {"enabled": True}


def fake_render(
    chromium,
    document_relative,
    resource_relative,
    output,
    include_images,
    optimize_images,
    pdf_profile,
    part_number,
    part_total,
):
    render_calls.append(part_number)
    if part_number == 2 and fail_second_part["enabled"]:
        raise RuntimeError("intentional part failure")
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with output.open("wb") as handle:
        writer.write(handle)
    writer.close()
    return 100, 0, 0, 0.01


module.render_pdf_part = fake_render
original_which = module.shutil.which
module.shutil.which = lambda name: (
    "/test/chromium" if name in {"chromium", "chromium-browser"} else original_which(name)
)
assert client.post("/pdf", data={"conversation": "large.html", "chunked": "on"}).status_code == 302
assert wait_for_job()["status"] == "error"
assert render_calls == [1, 2]
assert list((module.PDFS_DIR / ".resume").glob("*/part-00001.pdf"))

fail_second_part["enabled"] = False
render_calls.clear()
client.post("/pdf", data={"conversation": "large.html", "chunked": "on"})
assert wait_for_job()["status"] == "success"
assert render_calls == [2, 3]
assert len(PdfReader(module.PDFS_DIR / module.generated_pdfs()[0]).pages) == 3

client.post("/package", data={"conversation": "large.html"})
assert wait_for_job()["status"] == "success"
with zipfile.ZipFile(module.PACKAGES_DIR / module.generated_packages()[0]) as archive:
    names = archive.namelist()
assert "large.html" in names
assert "attachments/test.jpg" in names
assert any(name.startswith("pdf/") for name in names)

saved = client.post(
    "/settings",
    data={
        "schedule_enabled": "on",
        "schedule_interval_hours": "12",
        "schedule_network": "on",
        "schedule_device_id": "TEST-DEVICE-ID",
        "export_retention_days": "1",
        "pdf_retention_days": "1",
    },
)
assert saved.status_code == 302
assert module.effective_settings()["schedule_device_id"] == "TEST-DEVICE-ID"

old_export = module.EXPORTS_DIR / "archive-20200101-000000"
old_export.mkdir()
(old_export / "expired.txt").write_text("expired", encoding="utf-8")
old_pdf = module.PDFS_DIR / "expired.pdf"
old_pdf.write_bytes(b"expired")
old_time = time.time() - 3 * 86400
os.utime(old_export, (old_time, old_time))
os.utime(old_pdf, (old_time, old_time))
client.post("/storage/cleanup")
assert wait_for_job()["status"] == "success"
assert not old_export.exists() and not old_pdf.exists()
assert backup.exists() and (export_root / "large.html").exists()

commands = []
module.list_devices = lambda network=False, refresh=False: ["TEST-DEVICE-ID"]
module.jobs.run_process = lambda args, env=None, progress_label=None: commands.append(args)
module.run_iphone_backup(False, True, "TEST-DEVICE-ID")
assert commands[0][:5] == [
    "idevicebackup2",
    "-n",
    "-u",
    "TEST-DEVICE-ID",
    "backup",
]

commands.clear()
client.post(
    "/encryption",
    data={
        "device_target": "usb:TEST-DEVICE-ID",
        "backup_password": "one-value",
        "backup_password_confirm": "different-value",
    },
)
assert commands == []
client.post(
    "/encryption",
    data={
        "device_target": "usb:TEST-DEVICE-ID",
        "backup_password": "matching-value",
        "backup_password_confirm": "matching-value",
    },
)
assert wait_for_job()["status"] == "success"
assert commands[0][:6] == [
    "idevicebackup2",
    "-u",
    "TEST-DEVICE-ID",
    "-i",
    "encryption",
    "on",
]

module.write_schedule_state({"next_attempt": 0})
commands.clear()
assert module.scheduled_backup_tick(now=1000) == "started"
assert wait_for_job()["status"] == "success"
assert commands[0][2:5] == ["-u", "TEST-DEVICE-ID", "backup"]


class FakeWebhookResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, amount):
        return b""


webhooks = []
module.urllib.request.urlopen = lambda request, timeout: (
    webhooks.append(json.loads(request.data)) or FakeWebhookResponse()
)
notification_settings = module.read_app_settings()
notification_settings["notify_webhook_url"] = "https://secret.example.test/hook-token"
notification_settings["notify_on_success"] = True
module.write_app_settings(notification_settings)
module.send_job_notification("PDF rendering", "success", 42)
assert webhooks[-1] == {
    "application": "iMessage Archive",
    "job": "PDF rendering",
    "status": "success",
    "elapsed_seconds": 42,
}

client.post("/support-bundle")
assert wait_for_job()["status"] == "success"
support = next(module.PACKAGES_DIR.glob("support-*.zip"))
with zipfile.ZipFile(support) as archive:
    report = archive.read("support-report.json")
assert b"hook-token" not in report and b"Indexed test message" not in report

client.post(
    "/backup/delete",
    data={"backup_path": str(backup.resolve()), "confirm_backup": "wrong"},
)
assert backup.exists()
client.post(
    "/backup/delete",
    data={"backup_path": str(backup.resolve()), "confirm_backup": backup.name},
)
deletion = wait_for_job()
assert deletion["status"] == "success", deletion
assert not backup.exists()

progress = module.JobController()
progress.run_process(
    [sys.executable, "-c", "print('[===] 80% Finished'); print('100%')"],
    progress_label="Incremental backup",
)
assert progress.snapshot()["progress_current"] == 100
assert progress.snapshot()["progress_total"] == 100

print("core archive smoke tests passed")
