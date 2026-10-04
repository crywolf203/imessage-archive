from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from lxml import etree


CLASS_MESSAGE = {"message", "announcement"}


class ArchiveCatalog:
    def __init__(self, database: Path, exports_root: Path, chunk_messages: int = 300) -> None:
        self.database = database
        self.exports_root = exports_root
        self.chunk_messages = max(50, min(1000, chunk_messages))
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    path TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    missing_attachments INTEGER NOT NULL DEFAULT 0,
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    indexed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    conversation_path TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    message_start INTEGER NOT NULL,
                    message_end INTEGER NOT NULL,
                    message_count INTEGER NOT NULL,
                    file_size INTEGER NOT NULL,
                    PRIMARY KEY (conversation_path, chunk_index),
                    FOREIGN KEY (conversation_path) REFERENCES conversations(path) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    conversation_path TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    timestamp TEXT,
                    sender TEXT,
                    body TEXT NOT NULL,
                    FOREIGN KEY (conversation_path) REFERENCES conversations(path) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS messages_conversation_idx
                    ON messages(conversation_path, sequence);
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    body,
                    sender,
                    conversation_path UNINDEXED,
                    message_id UNINDEXED,
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TABLE IF NOT EXISTS diagnostics (
                    backup_path TEXT PRIMARY KEY,
                    output TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backup_runs (
                    id INTEGER PRIMARY KEY,
                    device_id TEXT,
                    mode TEXT NOT NULL,
                    network INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    detail TEXT
                );
                CREATE TABLE IF NOT EXISTS job_runs (
                    id INTEGER PRIMARY KEY,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    elapsed_seconds INTEGER NOT NULL
                );
                """
            )

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.exports_root.resolve()).as_posix()

    def conversations(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT path, title, file_size, message_count, missing_attachments,
                       chunk_count, indexed_at
                FROM conversations
                ORDER BY title COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def conversation(self, path: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE path = ?", (path,)
            ).fetchone()
        return dict(row) if row else None

    def chunks(self, conversation_path: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT chunk_index, path, message_start, message_end, message_count, file_size
                FROM chunks WHERE conversation_path = ? ORDER BY chunk_index
                """,
                (conversation_path,),
            ).fetchall()
        return [dict(row) for row in rows]

    def chunk(self, conversation_path: str, chunk_index: int) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT chunk_index, path, message_start, message_end, message_count, file_size
                FROM chunks WHERE conversation_path = ? AND chunk_index = ?
                """,
                (conversation_path, chunk_index),
            ).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS conversations,
                       COALESCE(SUM(message_count), 0) AS messages,
                       COALESCE(SUM(missing_attachments), 0) AS missing_attachments,
                       COALESCE(SUM(file_size), 0) AS html_bytes
                FROM conversations
                """
            ).fetchone()
        return dict(row)

    def search(self, query: str, limit: int = 100) -> list[dict]:
        tokens = re.findall(r"[\w@.+-]+", query, flags=re.UNICODE)
        if not tokens:
            return []
        expression = " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"*' for token in tokens[:10])
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.conversation_path, m.chunk_index, m.sequence, m.timestamp,
                       m.sender, snippet(messages_fts, 0, '[', ']', '...', 24) AS excerpt
                FROM messages_fts AS f
                JOIN messages AS m ON m.id = f.rowid
                WHERE messages_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (expression, max(1, min(500, limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def iter_messages(self, conversation_path: str):
        connection = self.connect()
        try:
            cursor = connection.execute(
                """
                SELECT sequence, timestamp, sender, body, chunk_index
                FROM messages
                WHERE conversation_path = ?
                ORDER BY sequence
                """,
                (conversation_path,),
            )
            for row in cursor:
                yield dict(row)
        finally:
            connection.close()

    def save_diagnostics(self, backup_path: str, output: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO diagnostics(backup_path, output, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(backup_path) DO UPDATE SET
                    output=excluded.output, updated_at=excluded.updated_at
                """,
                (backup_path, output, datetime.now().isoformat(timespec="seconds")),
            )

    def diagnostics(self, backup_path: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT output, updated_at FROM diagnostics WHERE backup_path = ?",
                (backup_path,),
            ).fetchone()
        return dict(row) if row else None

    def start_backup_run(self, device_id: str, mode: str, network: bool) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO backup_runs(device_id, mode, network, status, started_at)
                VALUES (?, ?, ?, 'running', ?)
                """,
                (device_id, mode, int(network), datetime.now().isoformat(timespec="seconds")),
            )
            return int(cursor.lastrowid)

    def finish_backup_run(self, run_id: int, status: str, detail: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE backup_runs
                SET status = ?, finished_at = ?, detail = ?
                WHERE id = ?
                """,
                (status, datetime.now().isoformat(timespec="seconds"), detail, run_id),
            )

    def backup_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT device_id, mode, network, status, started_at, finished_at, detail
                FROM backup_runs ORDER BY id DESC LIMIT ?
                """,
                (max(1, min(100, limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_job(
        self,
        label: str,
        status: str,
        started_at: str,
        finished_at: str,
        elapsed_seconds: int,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO job_runs(label, status, started_at, finished_at, elapsed_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                (label, status, started_at, finished_at, max(0, elapsed_seconds)),
            )
            connection.execute(
                """
                DELETE FROM job_runs WHERE id NOT IN (
                    SELECT id FROM job_runs ORDER BY id DESC LIMIT 200
                )
                """
            )

    def job_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT label, status, started_at, finished_at, elapsed_seconds
                FROM job_runs ORDER BY id DESC LIMIT ?
                """,
                (max(1, min(100, limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def sync(
        self,
        conversation_paths: list[str],
        progress: Callable[[int, int, str], None] | None = None,
        item_progress: Callable[[str, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict:
        paths = sorted(set(conversation_paths))
        indexed = 0
        skipped = 0
        for position, relative in enumerate(paths, 1):
            if cancelled and cancelled():
                raise RuntimeError("Library indexing was cancelled")
            source = (self.exports_root / relative).resolve()
            if not source.is_file() or source.suffix.lower() != ".html":
                continue
            stat = source.stat()
            existing = self.conversation(relative)
            if (
                existing
                and existing["file_size"] == stat.st_size
                and existing.get("modified_ns") == stat.st_mtime_ns
                and existing["chunk_count"] > 0
            ):
                skipped += 1
            else:
                self._index_conversation(
                    source,
                    relative,
                    item_progress=item_progress,
                    cancelled=cancelled,
                )
                indexed += 1
            if progress:
                progress(position, len(paths), relative)

        with self.connect() as connection:
            known = {row[0] for row in connection.execute("SELECT path FROM conversations")}
            removed = known - set(paths)
            for relative in removed:
                self._delete_conversation(connection, relative)
        return {"indexed": indexed, "skipped": skipped, "total": len(paths)}

    def _delete_conversation(
        self, connection: sqlite3.Connection, relative: str, delete_files: bool = True
    ) -> None:
        row = connection.execute(
            "SELECT path FROM chunks WHERE conversation_path = ? LIMIT 1", (relative,)
        ).fetchone()
        if row and delete_files:
            chunk_root = (self.exports_root / row[0]).parent
            if chunk_root.is_dir() and self.exports_root.resolve() in chunk_root.resolve().parents:
                shutil.rmtree(chunk_root, ignore_errors=True)
        connection.execute("DELETE FROM messages_fts WHERE conversation_path = ?", (relative,))
        connection.execute("DELETE FROM messages WHERE conversation_path = ?", (relative,))
        connection.execute("DELETE FROM chunks WHERE conversation_path = ?", (relative,))
        connection.execute("DELETE FROM conversations WHERE path = ?", (relative,))

    def _index_conversation(
        self,
        source: Path,
        relative: str,
        item_progress: Callable[[str, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        fingerprint = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]
        library_root = self.exports_root / ".library"
        library_root.mkdir(parents=True, exist_ok=True)
        final_directory = library_root / fingerprint
        previous_directory = library_root / f".{fingerprint}-previous"
        swapped_directories = False
        temporary_directory = Path(
            tempfile.mkdtemp(prefix=f".{fingerprint}-", dir=library_root)
        )
        header = self._chunk_header(source, relative)
        title = source.stem
        stat = source.stat()
        message_count = 0
        missing_count = 0
        chunk_index = 1
        chunk_start = 1
        chunk_parts: list[str] = []
        chunk_rows: list[tuple] = []
        message_spool = tempfile.SpooledTemporaryFile(
            max_size=4 * 1024 * 1024,
            mode="w+t",
            encoding="utf-8",
            newline="\n",
        )

        try:
            context = etree.iterparse(
                str(source),
                events=("end",),
                tag="div",
                html=True,
                recover=True,
                huge_tree=True,
            )
            for _, element in context:
                parent = element.getparent()
                classes = set((element.get("class") or "").split())
                if parent is None or str(parent.tag).lower() != "body" or not classes.intersection(CLASS_MESSAGE):
                    continue
                message_count += 1
                if message_count % 100 == 0 and cancelled and cancelled():
                    raise RuntimeError("Library indexing was cancelled")
                markup = etree.tostring(element, method="html", encoding="unicode")
                chunk_parts.append(markup)
                sender = self._class_text(element, "sender")
                timestamp = self._class_text(element, "timestamp")
                bodies = self._class_nodes(element, "message_part")
                body = " ".join(self._clean_text(node) for node in bodies).strip()
                if not body:
                    body = self._clean_text(element)
                missing = len(self._class_nodes(element, "attachment_error"))
                missing_count += missing
                message_spool.write(
                    json.dumps(
                        (relative, chunk_index, message_count, timestamp, sender, body),
                        ensure_ascii=False,
                    )
                    + "\n"
                )

                if len(chunk_parts) >= self.chunk_messages:
                    chunk_rows.append(
                        self._write_chunk(
                            temporary_directory,
                            relative,
                            chunk_index,
                            chunk_start,
                            message_count,
                            header,
                            chunk_parts,
                        )
                    )
                    if item_progress:
                        item_progress(relative, message_count)
                    chunk_index += 1
                    chunk_start = message_count + 1
                    chunk_parts = []
                element.clear()
                while element.getprevious() is not None:
                    del parent[0]
            if chunk_parts or not chunk_rows:
                chunk_rows.append(
                    self._write_chunk(
                        temporary_directory,
                        relative,
                        chunk_index,
                        chunk_start,
                        message_count,
                        header,
                        chunk_parts,
                    )
                )

            if previous_directory.exists():
                shutil.rmtree(previous_directory)
            if final_directory.exists():
                final_directory.rename(previous_directory)
            temporary_directory.rename(final_directory)
            swapped_directories = True
            normalized_chunks = []
            for row in chunk_rows:
                part_name = Path(row[1]).name
                chunk_relative = (final_directory / part_name).relative_to(self.exports_root).as_posix()
                normalized_chunks.append((row[0], chunk_relative, *row[2:]))

            with self.connect() as connection:
                self._delete_conversation(connection, relative, delete_files=False)
                connection.execute(
                    """
                    INSERT INTO conversations(
                        path, title, file_size, modified_ns, message_count,
                        missing_attachments, chunk_count, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        relative,
                        title,
                        stat.st_size,
                        stat.st_mtime_ns,
                        message_count,
                        missing_count,
                        len(normalized_chunks),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO chunks(
                        chunk_index, path, message_start, message_end,
                        message_count, file_size, conversation_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [(*row, relative) for row in normalized_chunks],
                )
                message_spool.seek(0)
                next_message_id = connection.execute(
                    "SELECT COALESCE(MAX(id), 0) + 1 FROM messages"
                ).fetchone()[0]
                message_batch = []
                search_batch = []
                inserted_count = 0

                def flush_messages() -> None:
                    if not message_batch:
                        return
                    connection.executemany(
                        """
                        INSERT INTO messages(
                            id, conversation_path, chunk_index, sequence,
                            timestamp, sender, body
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        message_batch,
                    )
                    connection.executemany(
                        """
                        INSERT INTO messages_fts(
                            rowid, body, sender, conversation_path, message_id
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        search_batch,
                    )
                    message_batch.clear()
                    search_batch.clear()

                for serialized in message_spool:
                    if inserted_count % 1000 == 0 and cancelled and cancelled():
                        raise RuntimeError("Library indexing was cancelled")
                    row = json.loads(serialized)
                    message_id = next_message_id
                    next_message_id += 1
                    inserted_count += 1
                    message_batch.append((message_id, *row))
                    search_batch.append(
                        (message_id, row[5], row[4] or "", relative, message_id)
                    )
                    if len(message_batch) >= 1000:
                        flush_messages()
                flush_messages()
            shutil.rmtree(previous_directory, ignore_errors=True)
        except BaseException:
            if swapped_directories:
                shutil.rmtree(final_directory, ignore_errors=True)
                if previous_directory.exists():
                    previous_directory.rename(final_directory)
            shutil.rmtree(temporary_directory, ignore_errors=True)
            raise
        finally:
            message_spool.close()

    def _write_chunk(
        self,
        directory: Path,
        conversation_path: str,
        chunk_index: int,
        start: int,
        end: int,
        header: str,
        parts: list[str],
    ) -> tuple:
        filename = f"part-{chunk_index:05d}.html"
        path = directory / filename
        path.write_text(
            header + "\n" + "\n".join(parts) + "\n</body>\n</html>\n",
            encoding="utf-8",
        )
        return (
            chunk_index,
            f".library/pending/{filename}",
            start,
            end,
            max(0, end - start + 1),
            path.stat().st_size,
        )

    def _chunk_header(self, source: Path, relative: str) -> str:
        with source.open("rb") as handle:
            beginning = handle.read(4 * 1024 * 1024)
        lowered = beginning.lower()
        end = lowered.find(b"</head>")
        if end >= 0:
            header = beginning[: end + len(b"</head>")].decode("utf-8", errors="replace")
        else:
            header = '<html><head><meta charset="UTF-8"></head>'
        parent = Path(relative).parent.as_posix()
        export_base = "/exports/" + (quote(parent.strip("/") + "/") if parent != "." else "")
        base = f'<base href="{html.escape(export_base, quote=True)}">'
        return header.replace("</head>", base + "</head>") + "\n<body>"

    @staticmethod
    def _class_nodes(element, class_name: str) -> list:
        return element.xpath(
            ".//*[contains(concat(' ', normalize-space(@class), ' '), $class_name)]",
            class_name=f" {class_name} ",
        )

    @classmethod
    def _class_text(cls, element, class_name: str) -> str:
        nodes = cls._class_nodes(element, class_name)
        return cls._clean_text(nodes[0]) if nodes else ""

    @staticmethod
    def _clean_text(element) -> str:
        return re.sub(r"\s+", " ", " ".join(element.itertext())).strip()
