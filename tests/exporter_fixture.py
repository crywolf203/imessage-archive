"""Exercise the real exporter against a generated, unencrypted iOS backup."""

import hashlib
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path


sys.path.insert(0, "/app")
from catalog import ArchiveCatalog


def run(*arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def verify_exporter():
    with tempfile.TemporaryDirectory(prefix="synthetic-ios-") as temporary:
        root = Path(temporary)
        backup = root / "backup"
        backup.mkdir()
        lockdown = {
            "BuildVersion": "22A3354", "DeviceName": "Synthetic CI iPhone",
            "ProductType": "iPhone16,1", "ProductVersion": "18.0",
            "SerialNumber": "SYNTHETIC", "UniqueDeviceID": "SYNTHETIC-CI",
        }
        for name, data in (
            ("Manifest.plist", {"IsEncrypted": False, "Lockdown": lockdown, "Applications": {}}),
            ("Info.plist", {"Device Name": "Synthetic CI iPhone", "Product Version": "18.0"}),
            ("Status.plist", {"SnapshotState": "finished"}),
        ):
            with (backup / name).open("wb") as output:
                plistlib.dump(data, output)
        message_id = hashlib.sha1(b"HomeDomain-Library/SMS/sms.db").hexdigest()
        database = backup / message_id[:2] / message_id
        database.parent.mkdir()
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript("""
                CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, person_centric_id TEXT);
                CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, service_name TEXT, display_name TEXT);
                CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
                CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER, message_date INTEGER);
                CREATE TABLE chat_recoverable_message_join (chat_id INTEGER, message_id INTEGER, delete_date INTEGER);
                CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
                CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, guid TEXT, filename TEXT, uti TEXT,
                    mime_type TEXT, transfer_name TEXT, total_bytes INTEGER, is_sticker INTEGER DEFAULT 0,
                    hide_attachment INTEGER DEFAULT 0);
                CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, service TEXT,
                    handle_id INTEGER, date INTEGER, date_read INTEGER DEFAULT 0, date_delivered INTEGER DEFAULT 0,
                    is_from_me INTEGER, is_read INTEGER DEFAULT 1, item_type INTEGER DEFAULT 0,
                    associated_message_guid TEXT, associated_message_type INTEGER DEFAULT 0,
                    thread_originator_guid TEXT, message_summary_info BLOB, attributedBody BLOB);
                INSERT INTO handle VALUES (1, 'fixture@example.invalid', NULL), (2, 'excluded@example.invalid', NULL);
                INSERT INTO chat VALUES (1, 'fixture@example.invalid', 'iMessage', 'Exporter fixture'),
                    (2, 'excluded@example.invalid', 'iMessage', 'Excluded fixture');
                INSERT INTO chat_handle_join VALUES (1, 1), (2, 2);
            """)
            for index in range(1, 7):
                connection.execute(
                    "INSERT INTO message (ROWID,guid,text,service,handle_id,date,is_from_me) VALUES (?,?,?,?,?,?,?)",
                    (index, f"fixture-{index}", f"Exporter fixture sentinel {index} <literal> & archive",
                     "iMessage", 1, 757382400000000000 + index * 1000000000, index % 2),
                )
                connection.execute("INSERT INTO chat_message_join VALUES (1, ?, 0)", (index,))
            connection.execute(
                "INSERT INTO message (ROWID,guid,text,service,handle_id,date,is_from_me) VALUES (7,?,?,?,?,?,0)",
                ("excluded-guid", "Excluded private sentinel", "iMessage", 2, 757382410000000000),
            )
            connection.execute("INSERT INTO chat_message_join VALUES (2, 7, 0)")
            connection.commit()
        with closing(sqlite3.connect(backup / "Manifest.db")) as connection:
            connection.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)")
            connection.execute("INSERT INTO Files VALUES (?, 'HomeDomain', 'Library/SMS/sms.db', 1, NULL)", (message_id,))
            connection.commit()
        original_hash = hashlib.sha256(database.read_bytes()).hexdigest()
        html_root = root / "html"
        text_root = root / "text"
        for format_name, destination in (("html", html_root), ("txt", text_root)):
            run("imessage-exporter", "-f", format_name, "-p", str(backup), "-a", "iOS",
                "-o", str(destination), "--no-progress", "-t", "fixture@example.invalid")
        html_files = list(html_root.glob("*.html"))
        assert html_files, "Exporter created no conversation HTML"
        combined = "\n".join(path.read_text(encoding="utf-8") for path in html_files)
        assert "Exporter fixture sentinel 1" in combined and "Exporter fixture sentinel 6" in combined
        assert "Excluded private sentinel" not in combined, "Contact filtering regressed"
        assert "&lt;literal&gt;" in combined and "&amp; archive" in combined, "HTML escaping regressed"
        text = "\n".join(path.read_text(encoding="utf-8") for path in text_root.glob("*.txt"))
        assert "Exporter fixture sentinel 6" in text and "Excluded private sentinel" not in text
        catalog = ArchiveCatalog(root / "catalog.sqlite3", html_root, chunk_messages=50)
        catalog.sync([path.name for path in html_files])
        assert catalog.stats()["messages"] == 6, "Viewer cannot parse the exporter's actual HTML"
        assert catalog.search("sentinel 6"), "Actual exported messages were not indexed"
        diagnostics = run("imessage-exporter", "-d", "-p", str(backup), "-a", "iOS")
        assert "messages" in diagnostics.lower()
        assert hashlib.sha256(database.read_bytes()).hexdigest() == original_hash
        return {"status": "passed", "messages": 6, "contact_filter": True, "html_text": True,
                "viewer_index": True, "diagnostics": True, "source_unchanged": True}


if __name__ == "__main__":
    print(verify_exporter())
