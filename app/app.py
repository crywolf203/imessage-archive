from __future__ import annotations

import hmac
import hashlib
import csv
import io
import json
import os
import platform
import plistlib
import re
import secrets
import shutil
import signal
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from collections import deque
from collections.abc import Callable
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit

import pexpect
from bs4 import BeautifulSoup
from catalog import ArchiveCatalog
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
)
from lxml import etree


BACKUPS_DIR = Path(os.environ.get("BACKUPS_DIR", "/data/backups"))
EXPORTS_DIR = Path(os.environ.get("EXPORTS_DIR", "/data/exports"))
CURRENT_EXPORT_DIR = EXPORTS_DIR / "current"
CURRENT_TEXT_DIR = EXPORTS_DIR / "current-text"
PDFS_DIR = Path(os.environ.get("PDFS_DIR", "/data/pdfs"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/data/config"))
PACKAGES_DIR = EXPORTS_DIR / "packages"
LOG_LINES = max(50, int(os.environ.get("LOG_LINES", "300")))
PDF_TIMEOUT_SECONDS = max(60, int(os.environ.get("PDF_TIMEOUT_SECONDS", "900")))
PDF_IMAGE_MAX_EDGE = max(800, int(os.environ.get("PDF_IMAGE_MAX_EDGE", "1800")))
PDF_IMAGE_QUALITY = min(95, max(55, int(os.environ.get("PDF_IMAGE_QUALITY", "80"))))
PDF_IMAGE_WORKERS = min(4, max(1, int(os.environ.get("PDF_IMAGE_WORKERS", "2"))))
PDF_OPTIMIZE_MIN_BYTES = max(0, int(os.environ.get("PDF_OPTIMIZE_MIN_BYTES", "524288")))
PDF_NICE_LEVEL = min(19, max(0, int(os.environ.get("PDF_NICE_LEVEL", "5"))))
PDF_RENDERER_PROCESSES = min(8, max(1, int(os.environ.get("PDF_RENDERER_PROCESSES", "2"))))
LIBRARY_CHUNK_MESSAGES = max(50, int(os.environ.get("LIBRARY_CHUNK_MESSAGES", "300")))
SCHEDULE_ENABLED = os.environ.get("SCHEDULE_ENABLED", "0") == "1"
SCHEDULE_INTERVAL_HOURS = max(1, int(os.environ.get("SCHEDULE_INTERVAL_HOURS", "24")))
SCHEDULE_NETWORK = os.environ.get("SCHEDULE_NETWORK", "1") == "1"
SCHEDULE_FULL = os.environ.get("SCHEDULE_FULL", "0") == "1"
SCHEDULE_STATE_FILE = CONFIG_DIR / "schedule-state.json"
APP_SETTINGS_FILE = CONFIG_DIR / "settings.json"
EXPORT_RETENTION_DAYS = max(0, int(os.environ.get("EXPORT_RETENTION_DAYS", "0")))
PDF_RETENTION_DAYS = max(0, int(os.environ.get("PDF_RETENTION_DAYS", "0")))
PDF_HISTORY_FILE = PDFS_DIR / ".render-history.json"
PRINT_TOKEN = secrets.token_urlsafe(24)

for directory in (BACKUPS_DIR, EXPORTS_DIR, PDFS_DIR, CONFIG_DIR, PACKAGES_DIR):
    directory.mkdir(parents=True, exist_ok=True)

settings_lock = threading.RLock()


def read_app_settings() -> dict:
    with settings_lock:
        try:
            value = json.loads(APP_SETTINGS_FILE.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}


def write_app_settings(settings: dict) -> None:
    with settings_lock:
        temporary = APP_SETTINGS_FILE.with_name(APP_SETTINGS_FILE.name + ".tmp")
        temporary.write_text(json.dumps(settings, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(APP_SETTINGS_FILE)
        APP_SETTINGS_FILE.chmod(0o600)


def effective_settings() -> dict:
    saved = read_app_settings()

    def bounded_integer(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(saved.get(name, default))
        except (TypeError, ValueError):
            value = default
        return min(maximum, max(minimum, value))

    return {
        "schedule_enabled": bool(saved.get("schedule_enabled", SCHEDULE_ENABLED)),
        "schedule_interval_hours": bounded_integer(
            "schedule_interval_hours", SCHEDULE_INTERVAL_HOURS, 1, 720
        ),
        "schedule_network": bool(saved.get("schedule_network", SCHEDULE_NETWORK)),
        "schedule_full": bool(saved.get("schedule_full", SCHEDULE_FULL)),
        "schedule_device_id": str(saved.get("schedule_device_id", "")).strip(),
        "notify_webhook_url": str(
            saved.get("notify_webhook_url", os.environ.get("NOTIFY_WEBHOOK_URL", ""))
        ).strip(),
        "notify_on_success": bool(
            saved.get("notify_on_success", os.environ.get("NOTIFY_ON_SUCCESS", "1") == "1")
        ),
        "export_retention_days": bounded_integer(
            "export_retention_days", EXPORT_RETENTION_DAYS, 0, 3650
        ),
        "pdf_retention_days": bounded_integer(
            "pdf_retention_days", PDF_RETENTION_DAYS, 0, 3650
        ),
    }

catalog = ArchiveCatalog(
    CONFIG_DIR / "archive-catalog.db",
    CURRENT_EXPORT_DIR,
    chunk_messages=LIBRARY_CHUNK_MESSAGES,
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
)


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return response


class JobController:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.busy = False
        self.label = "Idle"
        self.status = "idle"
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.started_monotonic: float | None = None
        self.lines: deque[str] = deque(maxlen=LOG_LINES)
        self.total_lines = 0
        self.active_process = None
        self.cancel_event = threading.Event()
        self.phase = "Idle"
        self.progress_current: int | None = None
        self.progress_total: int | None = None
        self.progress_detail = ""
        self.eta_seconds: float | None = None
        self.progress_updated_monotonic: float | None = None

    def append(self, text: str) -> None:
        clean = text.replace("\r", "")
        with self.lock:
            for line in clean.splitlines():
                self.lines.append(line)
                self.total_lines += 1

    def set_process(self, process) -> None:
        with self.lock:
            self.active_process = process

    def update_progress(
        self,
        phase: str,
        current: int | None = None,
        total: int | None = None,
        eta_seconds: float | None = None,
        detail: str = "",
    ) -> None:
        with self.lock:
            self.phase = phase
            self.progress_current = current
            self.progress_total = total
            self.progress_detail = detail
            self.eta_seconds = eta_seconds
            self.progress_updated_monotonic = time.monotonic()

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def start(self, label: str, target) -> bool:
        with self.lock:
            if self.busy:
                return False
            self.busy = True
            self.label = label
            self.status = "running"
            self.started_at = datetime.now().isoformat(timespec="seconds")
            self.finished_at = None
            self.started_monotonic = time.monotonic()
            self.lines.clear()
            self.total_lines = 0
            self.active_process = None
            self.cancel_event.clear()
            self.phase = label
            self.progress_current = None
            self.progress_total = None
            self.progress_detail = ""
            self.eta_seconds = None
            self.progress_updated_monotonic = self.started_monotonic
        threading.Thread(target=self._run, args=(target,), daemon=True).start()
        return True

    def _run(self, target) -> None:
        try:
            target()
        except Exception as exc:
            self.append(f"ERROR: {exc}")
            status = "error"
        else:
            status = "success"
        finally:
            with self.lock:
                self.busy = False
                self.status = status
                self.finished_at = datetime.now().isoformat(timespec="seconds")
                self.active_process = None
                self.eta_seconds = 0 if status == "success" else None
                label = self.label
                elapsed = (
                    int(time.monotonic() - self.started_monotonic)
                    if self.started_monotonic is not None
                    else 0
                )
                started_at = self.started_at or datetime.now().isoformat(timespec="seconds")
                finished_at = self.finished_at
            try:
                catalog.record_job(
                    label,
                    status,
                    started_at,
                    finished_at,
                    elapsed,
                )
            except Exception as exc:
                self.append(f"Unable to save job history: {exc}")
            send_job_notification(label, status, elapsed)

    def cancel(self) -> bool:
        with self.lock:
            process = self.active_process
            if not self.busy:
                return False
            self.cancel_event.set()
            self.lines.append("Cancellation requested...")
            self.total_lines += 1
        if process is None:
            return True
        try:
            if isinstance(process, subprocess.Popen):
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    process.terminate()
            else:
                process.terminate(force=True)
        except Exception as exc:
            self.append(f"Unable to cancel cleanly: {exc}")
        return True

    def snapshot(self) -> dict:
        with self.lock:
            now = time.monotonic()
            elapsed = 0
            if self.started_monotonic is not None:
                elapsed = max(0, int(now - self.started_monotonic))
            eta = self.eta_seconds
            if self.busy and eta is not None and self.progress_updated_monotonic is not None:
                eta = max(0, int(eta - (now - self.progress_updated_monotonic)))
            return {
                "busy": self.busy,
                "label": self.label,
                "status": self.status,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "log": "\n".join(self.lines),
                "truncated": max(0, self.total_lines - len(self.lines)),
                "phase": self.phase,
                "progress_current": self.progress_current,
                "progress_total": self.progress_total,
                "progress_detail": self.progress_detail,
                "elapsed_seconds": elapsed,
                "eta_seconds": eta,
            }

    def run_process(
        self,
        args: list[str],
        env: dict | None = None,
        progress_label: str | None = None,
    ) -> None:
        self.append("$ " + " ".join(args))
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=True,
        )
        self.set_process(process)
        assert process.stdout is not None
        for line in process.stdout:
            self.append(line)
            if progress_label:
                match = re.search(r"(?:\]|\s)(\d{1,3})%\b", line)
                if match:
                    percent = min(100, int(match.group(1)))
                    elapsed = self.snapshot()["elapsed_seconds"]
                    eta = (
                        (elapsed / percent) * (100 - percent)
                        if percent > 0 and elapsed > 0
                        else None
                    )
                    self.update_progress(
                        progress_label,
                        percent,
                        100,
                        eta,
                        f"{percent}% complete",
                    )
        code = process.wait()
        if code != 0:
            raise RuntimeError(f"Command exited with status {code}")

    def run_process_with_password(self, args: list[str], password: str) -> None:
        self.append("$ " + " ".join(args))

        class LogReader:
            def write(_, data: str) -> None:
                self.append(data.replace(password, "[redacted]"))

            def flush(_) -> None:
                return None

        child = pexpect.spawn(
            args[0],
            args[1:],
            encoding="utf-8",
            timeout=120,
            env=os.environ.copy(),
        )
        child.logfile_read = LogReader()
        self.set_process(child)
        while True:
            match = child.expect(
                [r"(?i)(password|passphrase)[^\r\n:]*:", pexpect.EOF, pexpect.TIMEOUT]
            )
            if match == 0:
                child.sendline(password)
            elif match == 1:
                break
            else:
                continue
        child.close()
        if child.signalstatus is not None:
            raise RuntimeError("Command was terminated")
        if child.exitstatus not in (0, None):
            raise RuntimeError(f"Command exited with status {child.exitstatus}")

    def run_bounded(self, args: list[str], timeout: int) -> None:
        displayed = re.sub(r"([?&]token=)[^&\s]+", r"\1[redacted]", " ".join(args))
        self.append("$ " + displayed)
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.set_process(process)
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise RuntimeError(f"PDF rendering exceeded the {timeout // 60}-minute limit")
        self.append(output or "")
        if process.returncode != 0:
            raise RuntimeError(f"Command exited with status {process.returncode}")


jobs = JobController()
print_asset_lock = threading.RLock()
print_asset_sets: dict[str, tuple[Path, dict[str, str]]] = {}
device_cache_lock = threading.RLock()
device_cache: dict[bool, tuple[float, list[str]]] = {}


def send_job_notification(label: str, status: str, elapsed_seconds: int) -> None:
    settings = effective_settings()
    webhook = settings["notify_webhook_url"]
    if not webhook:
        return
    if status == "success" and not settings["notify_on_success"]:
        return
    payload = json.dumps(
        {
            "application": "iMessage Archive",
            "job": label,
            "status": status,
            "elapsed_seconds": elapsed_seconds,
        }
    ).encode("utf-8")
    try:
        request_object = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request_object, timeout=10) as response:
            response.read(1)
    except Exception as exc:
        jobs.append(f"Notification failed: {exc}")


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def application_version() -> str:
    for candidate in (
        Path(__file__).resolve().with_name("VERSION"),
        Path(__file__).resolve().parents[1] / "VERSION",
    ):
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "unknown"


def load_pdf_history() -> list[dict]:
    try:
        data = json.loads(PDF_HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    return [item for item in data if isinstance(item, dict)][-20:] if isinstance(data, list) else []


def save_pdf_timing(html_bytes: int, image_bytes: int, image_count: int, seconds: float) -> None:
    history = load_pdf_history()
    history.append(
        {
            "html_bytes": html_bytes,
            "image_bytes": image_bytes,
            "image_count": image_count,
            "seconds": round(seconds, 2),
        }
    )
    try:
        temporary = PDF_HISTORY_FILE.with_name(PDF_HISTORY_FILE.name + ".tmp")
        temporary.write_text(json.dumps(history[-20:]), encoding="utf-8")
        temporary.replace(PDF_HISTORY_FILE)
    except OSError:
        pass


def pdf_work_units(html_bytes: int, image_bytes: int, image_count: int) -> float:
    html_mb = html_bytes / (1024 * 1024)
    image_mb = image_bytes / (1024 * 1024)
    return max(1.0, html_mb * 4 + image_mb + image_count * 0.04)


def estimate_pdf_seconds(html_bytes: int, image_bytes: int, image_count: int) -> int:
    work = pdf_work_units(html_bytes, image_bytes, image_count)
    rates = []
    for sample in load_pdf_history():
        try:
            sample_work = pdf_work_units(
                int(sample["html_bytes"]),
                int(sample["image_bytes"]),
                int(sample["image_count"]),
            )
            rates.append(float(sample["seconds"]) / sample_work)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    if rates:
        estimate = work * statistics.median(rates[-8:])
    else:
        estimate = 18 + work * 0.7
    return int(min(PDF_TIMEOUT_SECONDS * 0.9, max(15, estimate)))


def resolve_export_resource(source: Path, value: str) -> tuple[Path, str] | None:
    if not value or value.startswith(("data:", "http://", "https://", "#")):
        return None
    parsed = urlsplit(value)
    candidate = Path(unquote(parsed.path))
    resolved = candidate.resolve() if candidate.is_absolute() else (source.parent / candidate).resolve()
    root = CURRENT_EXPORT_DIR.resolve()
    if resolved != root and root not in resolved.parents:
        return None
    return resolved, resolved.relative_to(root).as_posix()


def collect_export_resources(
    source: Path,
    resource_source: Path | None = None,
    image_only: bool = False,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Path]:
    resources: dict[str, Path] = {}
    resource_source = resource_source or source
    attributes = {
        "img": ("src",),
        "source": ("src",),
        "video": ("src", "poster"),
        "audio": ("src",),
        "link": ("href",),
    }
    tags = ("img",) if image_only else tuple(attributes)
    scanned = 0
    context = etree.iterparse(
        str(source),
        events=("end",),
        tag=tags,
        html=True,
        recover=True,
        huge_tree=True,
    )
    for _, element in context:
        scanned += 1
        tag = str(element.tag).lower()
        for attribute in attributes.get(tag, ()):
            resolved = resolve_export_resource(resource_source, element.get(attribute, ""))
            if resolved and resolved[0].is_file():
                resources[resolved[1]] = resolved[0]
        if progress and scanned % 100 == 0:
            progress(scanned, len(resources))
        if cancelled and scanned % 100 == 0 and cancelled():
            raise RuntimeError("Resource scan was cancelled")
        parent = element.getparent()
        element.clear()
        if parent is not None:
            while element.getprevious() is not None:
                del parent[0]
    return resources


def collect_pdf_images(source: Path, resource_source: Path | None = None) -> dict[str, Path]:
    return collect_export_resources(source, resource_source, image_only=True)


def prepare_optimized_images(
    source: Path,
    images: dict[str, Path],
    part_label: str = "",
    max_edge: int = PDF_IMAGE_MAX_EDGE,
    quality: int = PDF_IMAGE_QUALITY,
) -> tuple[Path | None, dict[str, str], int]:
    magick = shutil.which("magick") or shutil.which("convert")
    if not magick or not images:
        return None, {}, sum(path.stat().st_size for path in images.values())

    native = {".jpg", ".jpeg", ".png", ".webp"}
    candidates = [
        (relative, path)
        for relative, path in images.items()
        if path.suffix.lower() not in native or path.stat().st_size >= PDF_OPTIMIZE_MIN_BYTES
    ]
    if not candidates:
        return None, {}, sum(path.stat().st_size for path in images.values())

    directory = Path(tempfile.mkdtemp(prefix="imessage-pdf-images-"))
    optimized: dict[str, str] = {}
    failed = 0
    started = time.monotonic()
    estimated_render = estimate_pdf_seconds(
        source.stat().st_size,
        sum(path.stat().st_size for path in images.values()),
        len(images),
    )
    jobs.append(
        f"Optimizing {len(candidates)} large or non-browser image(s) for PDF; originals are unchanged"
    )

    def convert_image(relative: str, path: Path) -> tuple[str, str, int]:
        name = hashlib.sha256(relative.encode("utf-8")).hexdigest() + ".jpg"
        destination = directory / name
        input_path = f"{path}[0]" if path.suffix.lower() in {".gif", ".heic", ".heif"} else str(path)
        result = subprocess.run(
            [
                magick,
                "-limit",
                "thread",
                "1",
                "-limit",
                "memory",
                "384MiB",
                input_path,
                "-auto-orient",
                "-thumbnail",
                f"{max_edge}x{max_edge}>",
                "-background",
                "white",
                "-alpha",
                "remove",
                "-alpha",
                "off",
                "-strip",
                "-quality",
                str(quality),
                str(destination),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
            check=False,
        )
        if result.returncode != 0 or not destination.is_file():
            raise RuntimeError(relative)
        return relative, name, destination.stat().st_size

    completed = 0
    optimized_bytes = 0
    try:
        with ThreadPoolExecutor(max_workers=PDF_IMAGE_WORKERS) as pool:
            futures = [pool.submit(convert_image, relative, path) for relative, path in candidates]
            for future in as_completed(futures):
                completed += 1
                try:
                    relative, name, size = future.result()
                    optimized[relative] = name
                    optimized_bytes += size
                except Exception:
                    failed += 1
                elapsed = time.monotonic() - started
                remaining_prep = (elapsed / completed) * (len(candidates) - completed)
                jobs.update_progress(
                    f"Preparing PDF images{part_label}",
                    completed,
                    len(candidates),
                    remaining_prep + estimated_render,
                    f"{completed} of {len(candidates)} · {human_size(optimized_bytes)} prepared",
                )
                if jobs.is_cancelled():
                    for pending in futures:
                        pending.cancel()
                    raise RuntimeError("PDF rendering was cancelled")
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise

    if failed:
        jobs.append(f"{failed} image(s) could not be optimized; using their original files")
    served_bytes = optimized_bytes + sum(
        path.stat().st_size for relative, path in images.items() if relative not in optimized
    )
    jobs.append(
        f"Prepared {len(optimized)} image(s) at {human_size(served_bytes)} for Chromium"
    )
    return directory, optimized, served_bytes


def internal_request_is_valid() -> bool:
    remote = request.remote_addr or ""
    token = request.args.get("token", "")
    return remote in {"127.0.0.1", "::1"} and hmac.compare_digest(token, PRINT_TOKEN)


@app.before_request
def require_auth():
    g.auth_via_basic = False
    if request.endpoint in {"print_view", "internal_export", "internal_pdf_asset"} and internal_request_is_valid():
        return None
    if request.endpoint in {"login", "healthz"}:
        return None
    expected_user = os.environ.get("APP_USER", "aaron")
    expected_password = os.environ.get("APP_PASSWORD", "")
    if not expected_password or expected_password == "change-me-before-lan-use":
        g.role = "admin"
        return None
    role = session.get("role")
    supplied = request.authorization
    if (
        supplied
        and hmac.compare_digest(supplied.username or "", expected_user)
        and hmac.compare_digest(supplied.password or "", expected_password)
    ):
        role = "admin"
        g.auth_via_basic = True
    if role not in {"admin", "viewer"}:
        if request.path.startswith("/api/"):
            return jsonify({"error": "authentication required"}), 401
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    g.role = role
    if role == "viewer" and request.method == "POST" and request.endpoint != "logout":
        abort(403, "This account has read-only access.")
    if request.method == "POST" and not g.auth_via_basic:
        supplied_token = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
        expected_token = session.get("csrf_token", "")
        if not expected_token or not hmac.compare_digest(supplied_token, expected_token):
            abort(403, "The form expired. Reload the page and try again.")
    return None


def session_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


LOGIN_PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sign in · iMessage Archive</title>
  <style>
    :root { color-scheme: light dark; font: 15px/1.45 system-ui, sans-serif; }
    body { display:grid; place-items:center; min-height:100vh; margin:0; background:#111513; color:#edf4f1; }
    main { width:min(360px, calc(100vw - 32px)); }
    h1 { font-size:22px; }
    label { display:block; margin:12px 0 5px; font-weight:650; }
    input, button { width:100%; min-height:42px; box-sizing:border-box; padding:9px 11px; border-radius:4px; }
    input { color:inherit; background:#181e1b; border:1px solid #34423c; }
    button { margin-top:14px; color:white; background:#2d9b87; border:0; font-weight:700; cursor:pointer; }
    .error { padding:9px; border-left:4px solid #cf603d; background:#202824; }
  </style>
</head>
<body><main>
  <h1>iMessage Archive</h1>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <input type="hidden" name="next" value="{{ next_url }}">
    <label>Username</label><input name="username" autocomplete="username" autofocus>
    <label>Password</label><input type="password" name="password" autocomplete="current-password">
    <button>Sign in</button>
  </form>
</main></body></html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    next_url = request.values.get("next", "")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("index")
    if request.method == "POST":
        supplied_token = request.form.get("csrf_token", "")
        expected_token = session.get("csrf_token", "")
        if not expected_token or not hmac.compare_digest(supplied_token, expected_token):
            abort(403)
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        app_user = os.environ.get("APP_USER", "aaron")
        app_password = os.environ.get("APP_PASSWORD", "")
        viewer_user = os.environ.get("VIEWER_USER", "viewer")
        viewer_password = os.environ.get("VIEWER_PASSWORD", "")
        if hmac.compare_digest(username, app_user) and hmac.compare_digest(password, app_password):
            session.clear()
            session["role"] = "admin"
            session["csrf_token"] = secrets.token_urlsafe(32)
            return redirect(next_url)
        if viewer_password and hmac.compare_digest(username, viewer_user) and hmac.compare_digest(password, viewer_password):
            session.clear()
            session["role"] = "viewer"
            session["csrf_token"] = secrets.token_urlsafe(32)
            return redirect(next_url)
        error = "The username or password was not recognized."
    return render_template_string(
        LOGIN_PAGE,
        error=error,
        next_url=next_url,
        csrf_token=session_csrf_token(),
    )


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/healthz")
def healthz():
    try:
        with catalog.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        for directory in (BACKUPS_DIR, EXPORTS_DIR, PDFS_DIR, CONFIG_DIR):
            if not directory.is_dir():
                raise OSError(f"Missing directory: {directory}")
    except Exception as exc:
        return jsonify({"status": "unhealthy", "error": str(exc)}), 503
    return jsonify({"status": "healthy"})


def quick_command(args: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, f"{args[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]} did not respond within {timeout} seconds"
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return result.returncode, output or "No output"


def list_devices(network: bool = False, refresh: bool = False) -> list[str]:
    with device_cache_lock:
        cached = device_cache.get(network)
        if not refresh and cached and time.monotonic() - cached[0] < 10:
            return list(cached[1])
    args = ["idevice_id"]
    if network:
        args.append("-n")
    args.append("-l")
    code, output = quick_command(args, timeout=5)
    if code != 0 or output == "No output":
        devices = []
    else:
        devices = [line.strip() for line in output.splitlines() if line.strip()]
    with device_cache_lock:
        device_cache[network] = (time.monotonic(), devices)
    return list(devices)


def clear_device_cache() -> None:
    with device_cache_lock:
        device_cache.clear()


def device_lists() -> tuple[list[str], list[str]]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        usb = pool.submit(list_devices, False)
        network = pool.submit(list_devices, True)
        return usb.result(), network.result()


def discover_backups() -> list[Path]:
    found: dict[str, Path] = {}
    for manifest_name in ("Manifest.db", "Manifest.plist"):
        candidates = [
            BACKUPS_DIR / manifest_name,
            *BACKUPS_DIR.glob(f"*/{manifest_name}"),
            *BACKUPS_DIR.glob(f"*/*/{manifest_name}"),
        ]
        for manifest in candidates:
            if not manifest.is_file():
                continue
            found[str(manifest.parent.resolve())] = manifest.parent
    return sorted(found.values(), key=lambda item: item.stat().st_mtime, reverse=True)


def read_plist(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            value = plistlib.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}


def backup_summaries() -> list[dict]:
    summaries = []
    for path in discover_backups():
        info = read_plist(path / "Info.plist")
        manifest = read_plist(path / "Manifest.plist")
        summaries.append(
            {
                "path": str(path.resolve()),
                "folder": path.name,
                "device": info.get("Device Name") or path.name,
                "ios": info.get("Product Version") or "Unknown",
                "serial": info.get("Serial Number") or "",
                "encrypted": bool(manifest.get("IsEncrypted")),
                "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(
                    timespec="minutes"
                ),
            }
        )
    return summaries


def storage_summaries() -> list[dict]:
    summaries = []
    for label, path in (
        ("Backups", BACKUPS_DIR),
        ("Exports", EXPORTS_DIR),
        ("PDFs", PDFS_DIR),
    ):
        usage = shutil.disk_usage(path)
        summaries.append(
            {
                "label": label,
                "free": usage.free,
                "total": usage.total,
                "percent": round((usage.used / usage.total) * 100) if usage.total else 0,
            }
        )
    return summaries


def diagnostic_summary(output: str) -> list[tuple[str, str]]:
    patterns = (
        ("Messages", r"Total messages:\s*([^\r\n]+)"),
        ("Conversations", r"Total chats:\s*([^\r\n]+)"),
        ("Missing attachments", r"Missing files:\s*([^\r\n]+)"),
        ("Attachment data present", r"Data present on disk:\s*([^\r\n]+)"),
        ("Contacts resolved", r"Handles with resolved names:\s*([^\r\n]+)"),
        ("Message dates", r"Date range:\s*([^\r\n]+)"),
    )
    summary = []
    for label, pattern in patterns:
        match = re.search(pattern, output, flags=re.IGNORECASE)
        if match:
            summary.append((label, match.group(1).strip()))
    return summary


def safe_path(root: Path, relative: str) -> Path:
    root_resolved = root.resolve()
    candidate = (root / relative).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        abort(404)
    return candidate


def rotate_export_directory(directory: Path, prefix: str) -> None:
    if not directory.exists():
        return
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = EXPORTS_DIR / f"{prefix}-{timestamp}"
    counter = 1
    while destination.exists():
        destination = EXPORTS_DIR / f"{prefix}-{timestamp}-{counter}"
        counter += 1
    directory.rename(destination)


def exported_conversations() -> list[str]:
    if not CURRENT_EXPORT_DIR.exists():
        return []
    candidates = [
        *CURRENT_EXPORT_DIR.glob("*.html"),
        *CURRENT_EXPORT_DIR.glob("*/*.html"),
    ]
    return sorted(
        path.relative_to(CURRENT_EXPORT_DIR).as_posix()
        for path in candidates
        if path.is_file()
    )


def generated_pdfs() -> list[str]:
    return sorted(
        (path.name for path in PDFS_DIR.glob("*.pdf") if path.is_file()),
        reverse=True,
    )


def generated_packages() -> list[str]:
    return sorted(
        (path.name for path in PACKAGES_DIR.glob("*.zip") if path.is_file()),
        reverse=True,
    )


def generated_text_exports() -> list[str]:
    if not CURRENT_TEXT_DIR.exists():
        return []
    return sorted(
        (
            path.relative_to(CURRENT_TEXT_DIR).as_posix()
            for path in CURRENT_TEXT_DIR.rglob("*.txt")
            if path.is_file()
        ),
        key=str.casefold,
    )


def index_current_library() -> dict:
    started = time.monotonic()

    def report(current: int, total: int, relative: str) -> None:
        elapsed = time.monotonic() - started
        eta = (elapsed / current) * (total - current) if current else None
        jobs.update_progress(
            "Indexing conversation library",
            current,
            total,
            eta,
            f"{current} of {total} · {relative}",
        )

    return catalog.sync(
        exported_conversations(),
        progress=report,
        item_progress=lambda relative, count: jobs.update_progress(
            "Indexing conversation",
            detail=f"{count:,} messages parsed · {relative}",
        ),
        cancelled=jobs.is_cancelled,
    )


@app.post("/library/index")
def start_library_index():
    def target() -> None:
        result = index_current_library()
        jobs.append(
            f"Library ready: {result['indexed']} indexed, {result['skipped']} unchanged"
        )

    if not jobs.start("Library indexing", target):
        flash("Another job is already running.", "error")
    return redirect(url_for("index"))


@app.post("/preflight")
def start_preflight():
    def target() -> None:
        checks = []
        usb = list_devices(False, refresh=True)
        network = list_devices(True, refresh=True)
        checks.append((bool(usb or network), f"Devices: {', '.join(usb + network) or 'none detected'}"))
        for device_id in usb:
            code, output = quick_command(["idevicepair", "-u", device_id, "validate"])
            checks.append((code == 0, f"Trust for {device_id}: {output}"))
        for command in ("imessage-exporter", "idevicebackup2", "magick", "ffmpeg", "chromium"):
            checks.append((bool(shutil.which(command)), f"Tool {command}: {'ready' if shutil.which(command) else 'missing'}"))
        for storage in storage_summaries():
            checks.append(
                (
                    storage["percent"] < 95,
                    f"{storage['label']}: {human_size(storage['free'])} free ({storage['percent']}% used)",
                )
            )
        for ok, detail in checks:
            jobs.append(f"{'PASS' if ok else 'CHECK'}: {detail}")
        failed = sum(1 for ok, _ in checks if not ok)
        jobs.update_progress(
            "Preflight complete",
            len(checks) - failed,
            len(checks),
            0,
            f"{failed} item(s) need attention" if failed else "All checks passed",
        )

    if not jobs.start("Preflight checks", target):
        flash("Another job is already running.", "error")
    return redirect(url_for("index"))


@app.post("/storage/cleanup")
def start_storage_cleanup():
    def target() -> None:
        settings = effective_settings()
        now = time.time()
        export_candidates: list[Path] = []
        pdf_candidates: list[Path] = []
        if settings["export_retention_days"]:
            cutoff = now - settings["export_retention_days"] * 86400
            for pattern in ("archive-*", "text-archive-*"):
                for path in EXPORTS_DIR.glob(pattern):
                    if not path.is_dir() or path.stat().st_mtime >= cutoff:
                        continue
                    resolved = path.resolve()
                    if EXPORTS_DIR.resolve() not in resolved.parents:
                        continue
                    export_candidates.append(path)
        if settings["pdf_retention_days"]:
            cutoff = now - settings["pdf_retention_days"] * 86400
            for path in PDFS_DIR.glob("*.pdf"):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    pdf_candidates.append(path)
        candidates = export_candidates + pdf_candidates
        storage_roots = {
            os.stat(path).st_dev: path for path in (EXPORTS_DIR, PDFS_DIR)
        }
        free_before = {
            device: shutil.disk_usage(path).free
            for device, path in storage_roots.items()
        }
        removed = 0
        for position, path in enumerate(candidates, 1):
            jobs.update_progress(
                "Removing expired archives",
                position - 1,
                len(candidates),
                detail=f"{position} of {len(candidates)} · {path.name}",
            )
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            removed += 1
            if jobs.is_cancelled():
                raise RuntimeError("Storage cleanup was cancelled")
        free_after = {
            device: shutil.disk_usage(path).free
            for device, path in storage_roots.items()
        }
        reclaimed = sum(max(0, free_after[key] - free_before[key]) for key in free_before)
        jobs.update_progress(
            "Storage cleanup complete",
            removed,
            removed or 1,
            0,
            f"Removed {removed} item(s) · reclaimed {human_size(reclaimed)}",
        )
        if not settings["export_retention_days"] and not settings["pdf_retention_days"]:
            jobs.append("Cleanup is disabled. Set retention days under Automation to enable it.")

    if not jobs.start("Storage cleanup", target):
        flash("Another job is already running.", "error")
    return redirect(url_for("index"))


@app.post("/settings")
def save_settings():
    def bounded_integer(name: str, default: int, maximum: int) -> int:
        try:
            return min(maximum, max(0, int(request.form.get(name, str(default)))))
        except ValueError:
            return default

    current = read_app_settings()
    webhook = request.form.get("notify_webhook_url", "").strip()
    if webhook:
        parsed = urlsplit(webhook)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            flash("The notification webhook must be a complete HTTP or HTTPS address.", "error")
            return redirect(url_for("index"))
        current["notify_webhook_url"] = webhook
    if request.form.get("clear_webhook") == "on":
        current["notify_webhook_url"] = ""
    current.update(
        {
            "schedule_enabled": request.form.get("schedule_enabled") == "on",
            "schedule_interval_hours": max(
                1, bounded_integer("schedule_interval_hours", SCHEDULE_INTERVAL_HOURS, 720)
            ),
            "schedule_network": request.form.get("schedule_network") == "on",
            "schedule_full": request.form.get("schedule_full") == "on",
            "schedule_device_id": request.form.get("schedule_device_id", "").strip(),
            "notify_on_success": request.form.get("notify_on_success") == "on",
            "export_retention_days": bounded_integer(
                "export_retention_days", EXPORT_RETENTION_DAYS, 3650
            ),
            "pdf_retention_days": bounded_integer(
                "pdf_retention_days", PDF_RETENTION_DAYS, 3650
            ),
        }
    )
    if current["schedule_device_id"] and not re.fullmatch(
        r"[A-Za-z0-9-]{1,128}", current["schedule_device_id"]
    ):
        flash("The preferred device identifier is not valid.", "error")
        return redirect(url_for("index"))
    write_app_settings(current)
    state = read_schedule_state()
    state["next_attempt"] = time.time() + 60
    state["last_status"] = (
        "enabled; next device check within one minute"
        if current["schedule_enabled"]
        else "disabled"
    )
    write_schedule_state(state)
    flash("Automation settings saved.", "success")
    return redirect(url_for("index"))


@app.post("/device/<action>")
def device_action(action: str):
    if action not in {"pair", "validate"}:
        abort(404)
    available = list_devices(False, refresh=True)
    selected = request.form.get("device_id", "").strip()
    if selected and selected not in available:
        abort(400, "Select a connected USB device.")
    if not selected and len(available) == 1:
        selected = available[0]
    args = ["idevicepair"]
    if selected:
        args.extend(["-u", selected])
    args.append(action)
    code, output = quick_command(args)
    clear_device_cache()
    flash(output, "success" if code == 0 else "error")
    return redirect(url_for("index"))


@app.post("/encryption")
def enable_encryption():
    password = request.form.get("backup_password", "")
    confirmation = request.form.get("backup_password_confirm", "")
    if not password:
        flash("Enter a backup encryption password.", "error")
        return redirect(url_for("index"))
    if password != confirmation:
        flash("The two backup encryption passwords do not match.", "error")
        return redirect(url_for("index"))
    device_id, network = selected_device_from_form("device_target")

    def target() -> None:
        env = os.environ.copy()
        env["BACKUP_PASSWORD"] = password
        args = ["idevicebackup2"]
        if network:
            args.append("-n")
        args.extend(["-u", device_id, "-i", "encryption", "on"])
        jobs.run_process(args, env=env)

    if not jobs.start("Enabling encrypted backups", target):
        flash("Another job is already running.", "error")
    return redirect(url_for("index"))


@app.post("/backup")
def start_backup():
    full = request.form.get("full") == "on"
    device_id, network = selected_device_from_form("device_target")

    def target() -> None:
        run_iphone_backup(full=full, network=network, device_id=device_id)

    if not jobs.start("iPhone backup", target):
        flash("Another job is already running.", "error")
    return redirect(url_for("index"))


@app.post("/backup/delete")
def start_backup_deletion():
    backup = selected_backup_from_form()
    confirmation = request.form.get("confirm_backup", "").strip()
    if confirmation != backup.name:
        flash(f"Type {backup.name} exactly to delete this backup.", "error")
        return redirect(url_for("index"))

    def target() -> None:
        resolved = backup.resolve()
        root = BACKUPS_DIR.resolve()
        if resolved == root or root not in resolved.parents:
            raise RuntimeError("Refusing to delete a path outside the backup directory")
        jobs.update_progress("Deleting backup", detail=backup.name)
        shutil.rmtree(resolved)
        jobs.update_progress("Backup deleted", 1, 1, 0, backup.name)

    if not jobs.start("Backup deletion", target):
        flash("Another job is already running.", "error")
    return redirect(url_for("index"))


def run_iphone_backup(full: bool, network: bool, device_id: str | None = None) -> None:
    devices = list_devices(network, refresh=True)
    if device_id and device_id not in devices:
        raise RuntimeError(f"The selected iPhone is no longer available: {device_id}")
    if not device_id:
        if not devices:
            raise RuntimeError("No iPhone is available for this backup")
        device_id = devices[0]
    mode = "full" if full else "incremental"
    run_id = catalog.start_backup_run(device_id, mode, network)
    args = ["idevicebackup2"]
    if network:
        args.append("-n")
    args.extend(["-u", device_id])
    args.append("backup")
    if full:
        args.append("--full")
    args.append(str(BACKUPS_DIR / "latest"))
    try:
        jobs.run_process(args, progress_label=f"{mode.title()} backup")
    except Exception as exc:
        catalog.finish_backup_run(run_id, "error", str(exc))
        raise
    else:
        catalog.finish_backup_run(run_id, "success", "Backup completed")


def selected_device_from_form(field: str) -> tuple[str, bool]:
    usb = list_devices(False, refresh=True)
    network = list_devices(True, refresh=True)
    allowed = {f"usb:{device_id}": (device_id, False) for device_id in usb}
    allowed.update(
        {f"network:{device_id}": (device_id, True) for device_id in network}
    )
    selected = request.form.get(field, "")
    if selected not in allowed:
        abort(400, "Select an available iPhone.")
    return allowed[selected]


def selected_backup_from_form() -> Path:
    selected = request.form.get("backup_path", "")
    allowed = {str(path.resolve()): path for path in discover_backups()}
    if selected not in allowed:
        abort(400, "Select a discovered backup.")
    return allowed[selected]


@app.post("/diagnostics")
def start_diagnostics():
    backup = selected_backup_from_form()
    password = request.form.get("backup_password", "")

    def target() -> None:
        args = [
            "imessage-exporter",
            "--diagnostics",
            "-p",
            str(backup),
            "-a",
            "iOS",
            "--no-progress",
        ]
        if password:
            jobs.run_process_with_password(args, password)
        else:
            jobs.run_process(args)
        catalog.save_diagnostics(str(backup.resolve()), jobs.snapshot()["log"])

    if not jobs.start("Backup diagnostics", target):
        flash("Another job is already running.", "error")
    return redirect(url_for("index"))


@app.post("/backup/verify")
def start_backup_verification():
    backup = selected_backup_from_form()

    def target() -> None:
        jobs.update_progress("Verifying backup", detail=str(backup))
        required = ("Info.plist", "Manifest.plist", "Manifest.db", "Status.plist")
        missing = [name for name in required if not (backup / name).is_file()]
        if missing:
            raise RuntimeError("Backup is missing: " + ", ".join(missing))
        info = read_plist(backup / "Info.plist")
        manifest = read_plist(backup / "Manifest.plist")
        status = read_plist(backup / "Status.plist")
        encrypted = bool(manifest.get("IsEncrypted"))
        jobs.append(f"Device: {info.get('Device Name', backup.name)}")
        jobs.append(f"iOS: {info.get('Product Version', 'unknown')}")
        jobs.append(f"Encryption: {'enabled' if encrypted else 'not enabled'}")
        jobs.append(f"Snapshot state: {status.get('SnapshotState', 'unknown')}")
        if encrypted:
            jobs.append("Manifest database is encrypted; structural SQLite check skipped")
        else:
            with closing(sqlite3.connect(backup / "Manifest.db", timeout=30)) as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise RuntimeError(f"Manifest database check failed: {result}")
                file_count = connection.execute("SELECT COUNT(*) FROM Files").fetchone()[0]
            jobs.append(f"Manifest database: OK - {file_count} file records")
        jobs.update_progress(
            "Backup verification complete",
            len(required),
            len(required),
            0,
            "Core manifests and database structure are readable",
        )

    if not jobs.start("Backup verification", target):
        flash("Another job is already running.", "error")
    return redirect(url_for("index"))


@app.post("/export")
def start_export():
    backup = selected_backup_from_form()
    password = request.form.get("backup_password", "")
    handle = request.form.get("handle", "").strip()
    copy_method = request.form.get("copy_method", "clone")
    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()
    no_lazy = request.form.get("no_lazy") == "on"
    export_formats = request.form.get("export_formats", "html")
    if copy_method not in {"clone", "basic", "full", "disabled"}:
        abort(400)
    if export_formats not in {"html", "txt", "both"}:
        abort(400)

    def target() -> None:
        def run_format(format_name: str, destination: Path, method: str) -> None:
            args = [
                "imessage-exporter",
                "-f",
                format_name,
                "-p",
                str(backup),
                "-a",
                "iOS",
                "-o",
                str(destination),
                "-c",
                method,
                "--no-progress",
            ]
            if handle:
                args.extend(["-t", handle])
            if start_date:
                args.extend(["--start-date", start_date])
            if end_date:
                args.extend(["--end-date", end_date])
            if format_name == "html" and no_lazy:
                args.append("--no-lazy")
            if password:
                jobs.run_process_with_password(args, password)
            else:
                jobs.run_process(args)

        if export_formats in {"html", "both"}:
            rotate_export_directory(CURRENT_EXPORT_DIR, "archive")
            run_format("html", CURRENT_EXPORT_DIR, copy_method)
            jobs.append("Finished HTML export")
            result = index_current_library()
            jobs.append(
                f"Library ready: {result['indexed']} indexed, {result['skipped']} unchanged"
            )
        if export_formats in {"txt", "both"}:
            rotate_export_directory(CURRENT_TEXT_DIR, "text-archive")
            text_method = "disabled" if export_formats == "both" else copy_method
            run_format("txt", CURRENT_TEXT_DIR, text_method)
            jobs.append("Finished text export")

    if not jobs.start("Message export", target):
        flash("Another job is already running.", "error")
    return redirect(url_for("index"))


@app.post("/cancel")
def cancel_job():
    flash("Cancellation requested." if jobs.cancel() else "No cancellable job is running.")
    return redirect(url_for("index"))


def rewrite_internal_resources(soup: BeautifulSoup, source: Path, asset_set: str = "") -> None:
    for tag, attribute in (("img", "src"), ("link", "href")):
        for element in soup.find_all(tag):
            value = element.get(attribute)
            resource = resolve_export_resource(source, value or "")
            if not resource:
                continue
            _, relative = resource
            optimized_name = ""
            if tag == "img" and asset_set:
                with print_asset_lock:
                    entry = print_asset_sets.get(asset_set)
                    if entry:
                        optimized_name = entry[1].get(relative, "")
            if optimized_name:
                element[attribute] = url_for(
                    "internal_pdf_asset",
                    asset_set=asset_set,
                    filename=optimized_name,
                    token=PRINT_TOKEN,
                )
                continue
            element[attribute] = url_for(
                "internal_export",
                filename=relative,
                token=PRINT_TOKEN,
            )


@app.get("/internal/print")
def print_view():
    if not internal_request_is_valid():
        abort(403)
    relative = request.args.get("file", "")
    resource_relative = request.args.get("resource_base", "") or relative
    include_images = request.args.get("images") == "1"
    asset_set = request.args.get("asset_set", "")
    source = safe_path(CURRENT_EXPORT_DIR, relative)
    resource_source = safe_path(CURRENT_EXPORT_DIR, resource_relative)
    if not source.is_file() or source.suffix.lower() != ".html":
        abort(404)
    if not resource_source.is_file() or resource_source.suffix.lower() != ".html":
        abort(404)
    soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for media in soup.find_all(["video", "audio"]):
        placeholder = soup.new_tag("span")
        placeholder.string = "[Media omitted from PDF]"
        media.replace_with(placeholder)
    if not include_images:
        for image in soup.find_all("img"):
            placeholder = soup.new_tag("span")
            placeholder.string = "[Image omitted from PDF]"
            image.replace_with(placeholder)
    rewrite_internal_resources(soup, resource_source, asset_set)
    style = soup.new_tag("style")
    style.string = """
        @page { margin: 12mm; }
        video, audio { display: none !important; }
        img { max-width: 100% !important; height: auto !important; break-inside: avoid; }
    """
    if soup.head:
        soup.head.append(style)
    else:
        soup.insert(0, style)
    return Response(str(soup), mimetype="text/html")


@app.get("/internal/export/<path:filename>")
def internal_export(filename: str):
    if not internal_request_is_valid():
        abort(403)
    path = safe_path(CURRENT_EXPORT_DIR, filename)
    if not path.is_file():
        abort(404)
    return send_from_directory(CURRENT_EXPORT_DIR, filename)


@app.get("/internal/pdf-assets/<asset_set>/<path:filename>")
def internal_pdf_asset(asset_set: str, filename: str):
    if not internal_request_is_valid():
        abort(403)
    with print_asset_lock:
        entry = print_asset_sets.get(asset_set)
    if not entry:
        abort(404)
    directory, _ = entry
    path = safe_path(directory, filename)
    if not path.is_file():
        abort(404)
    return send_from_directory(directory, filename)


def render_pdf_part(
    chromium: str,
    document_relative: str,
    resource_relative: str,
    output: Path,
    include_images: bool,
    optimize_images: bool,
    pdf_profile: str,
    part_number: int,
    part_total: int,
) -> tuple[int, int, int, float]:
    source = safe_path(CURRENT_EXPORT_DIR, document_relative)
    resource_source = safe_path(CURRENT_EXPORT_DIR, resource_relative)
    html_bytes = source.stat().st_size
    images: dict[str, Path] = {}
    image_bytes = 0
    asset_directory: Path | None = None
    asset_set = ""
    part_label = f" (part {part_number} of {part_total})" if part_total > 1 else ""
    try:
        if include_images:
            jobs.update_progress(
                f"Scanning PDF images{part_label}",
                part_number - 1,
                part_total,
                detail="Finding attachments for this part",
            )
            images = collect_pdf_images(source, resource_source)
            image_bytes = sum(path.stat().st_size for path in images.values())
            jobs.append(
                f"Part {part_number}: {len(images)} available image(s), {human_size(image_bytes)}"
            )
            if optimize_images:
                max_edge = 1200 if pdf_profile == "fast" else PDF_IMAGE_MAX_EDGE
                quality = 68 if pdf_profile == "fast" else PDF_IMAGE_QUALITY
                asset_directory, optimized, image_bytes = prepare_optimized_images(
                    source,
                    images,
                    part_label,
                    max_edge=max_edge,
                    quality=quality,
                )
                if asset_directory and optimized:
                    asset_set = secrets.token_urlsafe(12)
                    with print_asset_lock:
                        print_asset_sets[asset_set] = (asset_directory, optimized)

        render_estimate = estimate_pdf_seconds(html_bytes, image_bytes, len(images))
        jobs.update_progress(
            f"Rendering PDF{part_label}",
            part_number - 1,
            part_total,
            render_estimate,
            f"{len(images)} images · {human_size(image_bytes)}",
        )
        query = urlencode(
            {
                "file": document_relative,
                "resource_base": resource_relative,
                "images": "1" if include_images else "0",
                "asset_set": asset_set,
                "token": PRINT_TOKEN,
            }
        )
        page_url = f"http://127.0.0.1:8080/internal/print?{query}"
        args = [
            chromium,
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            f"--renderer-process-limit={PDF_RENDERER_PROCESSES}",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={output}",
            page_url,
        ]
        if shutil.which("nice") and PDF_NICE_LEVEL:
            args = ["nice", "-n", str(PDF_NICE_LEVEL), *args]
        render_started = time.monotonic()
        jobs.run_bounded(args, PDF_TIMEOUT_SECONDS)
        render_seconds = time.monotonic() - render_started
        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError(f"Chromium did not create PDF part {part_number}")
        save_pdf_timing(html_bytes, image_bytes, len(images), render_seconds)
        return html_bytes, image_bytes, len(images), render_seconds
    finally:
        if asset_set:
            with print_asset_lock:
                print_asset_sets.pop(asset_set, None)
        if asset_directory:
            shutil.rmtree(asset_directory, ignore_errors=True)


@app.post("/pdf")
def start_pdf():
    relative = request.form.get("conversation", "")
    include_images = request.form.get("include_images") == "on"
    pdf_profile = request.form.get("pdf_profile", "balanced")
    if pdf_profile not in {"fast", "balanced", "archive"}:
        abort(400)
    optimize_images = include_images and pdf_profile != "archive"
    use_chunks = request.form.get("chunked") == "on"
    source = safe_path(CURRENT_EXPORT_DIR, relative)
    if not source.is_file() or source.suffix.lower() != ".html":
        abort(404)
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip("-") or "conversation"
    output = PDFS_DIR / f"{safe_stem}-{datetime.now():%Y%m%d-%H%M%S}.pdf"
    indexed_chunks = catalog.chunks(relative) if use_chunks else []
    documents = (
        [(item["path"], relative) for item in indexed_chunks]
        if indexed_chunks
        else [(relative, relative)]
    )
    resume_material = {
        "conversation": relative,
        "include_images": include_images,
        "optimize_images": optimize_images,
        "pdf_profile": pdf_profile,
        "documents": [
            (
                document,
                safe_path(CURRENT_EXPORT_DIR, document).stat().st_size,
                safe_path(CURRENT_EXPORT_DIR, document).stat().st_mtime_ns,
            )
            for document, _ in documents
        ],
    }
    resume_key = hashlib.sha256(
        json.dumps(resume_material, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]

    def target() -> None:
        chromium = shutil.which("chromium") or shutil.which("chromium-browser")
        if not chromium:
            raise RuntimeError("Chromium is not installed")
        part_directory: Path | None = None
        part_outputs: list[Path] = []
        total_render_seconds = 0.0
        completed_successfully = False
        try:
            if len(documents) > 1:
                part_directory = PDFS_DIR / ".resume" / resume_key
                part_directory.mkdir(parents=True, exist_ok=True)
            for position, (document_relative, resource_relative) in enumerate(documents, 1):
                if jobs.is_cancelled():
                    raise RuntimeError("PDF rendering was cancelled")
                part_output = (
                    part_directory / f"part-{position:05d}.pdf"
                    if part_directory
                    else output
                )
                if part_directory and part_output.is_file() and part_output.stat().st_size:
                    try:
                        from pypdf import PdfReader

                        if len(PdfReader(part_output).pages) > 0:
                            part_outputs.append(part_output)
                            jobs.append(f"Resuming with completed PDF part {position} of {len(documents)}")
                            jobs.update_progress(
                                "Resuming PDF",
                                position,
                                len(documents),
                                detail=f"Reused part {position} of {len(documents)}",
                            )
                            continue
                    except Exception:
                        part_output.unlink(missing_ok=True)
                _, _, _, render_seconds = render_pdf_part(
                    chromium,
                    document_relative,
                    resource_relative,
                    part_output,
                    include_images,
                    optimize_images,
                    pdf_profile,
                    position,
                    len(documents),
                )
                total_render_seconds += render_seconds
                part_outputs.append(part_output)
                jobs.update_progress(
                    "PDF parts rendered",
                    position,
                    len(documents),
                    0 if position == len(documents) else None,
                    f"Completed part {position} of {len(documents)}",
                )

            if len(part_outputs) > 1:
                jobs.update_progress(
                    "Combining PDF parts",
                    len(part_outputs),
                    len(part_outputs),
                    detail=f"Merging {len(part_outputs)} reliable, bounded parts",
                )
                from pypdf import PdfWriter

                writer = PdfWriter()
                for part in part_outputs:
                    writer.append(str(part))
                with output.open("wb") as handle:
                    writer.write(handle)
                writer.close()

            if not output.exists() or output.stat().st_size == 0:
                raise RuntimeError("The final PDF was not created")
            jobs.update_progress(
                "PDF complete",
                len(documents),
                len(documents),
                0,
                f"{human_size(output.stat().st_size)} · {len(documents)} part(s) · {int(total_render_seconds)} render seconds",
            )
            jobs.append(f"PDF created: {output.name}")
            completed_successfully = True
        finally:
            if part_directory and completed_successfully:
                shutil.rmtree(part_directory, ignore_errors=True)
            elif part_directory:
                jobs.append(
                    f"Completed PDF parts were preserved for retry key {resume_key}"
                )

    if not jobs.start("PDF rendering", target):
        flash("Another job is already running.", "error")
    return redirect(url_for("index", conversation=relative))


@app.get("/exports/<path:filename>")
def exported_file(filename: str):
    path = safe_path(CURRENT_EXPORT_DIR, filename)
    if not path.is_file():
        abort(404)
    return send_from_directory(CURRENT_EXPORT_DIR, filename)


@app.get("/viewer/<path:filename>")
def conversation_view(filename: str):
    source = safe_path(CURRENT_EXPORT_DIR, filename)
    if not source.is_file() or source.suffix.lower() != ".html":
        abort(404)
    soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for image in soup.find_all("img"):
        image["loading"] = "lazy"
        image["decoding"] = "async"
    for media in soup.find_all(["video", "audio"]):
        media["preload"] = "none"
    base = soup.find("base")
    if not base:
        relative_parent = source.parent.relative_to(CURRENT_EXPORT_DIR).as_posix()
        base_path = "" if relative_parent == "." else f"{relative_parent}/"
        base = soup.new_tag("base")
        base["href"] = url_for("exported_file", filename=base_path)
        if soup.head:
            soup.head.insert(0, base)
        else:
            soup.insert(0, base)
    return Response(str(soup), mimetype="text/html")


@app.get("/pdfs/<path:filename>")
def pdf_file(filename: str):
    path = safe_path(PDFS_DIR, filename)
    if not path.is_file():
        abort(404)
    return send_from_directory(PDFS_DIR, filename, as_attachment=True)


@app.get("/text/<path:filename>")
def text_file(filename: str):
    path = safe_path(CURRENT_TEXT_DIR, filename)
    if not path.is_file() or path.suffix.lower() != ".txt":
        abort(404)
    return send_from_directory(CURRENT_TEXT_DIR, filename, as_attachment=True)


@app.get("/csv/<path:filename>")
def conversation_csv(filename: str):
    if not catalog.conversation(filename):
        abort(404)
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).stem).strip("-") or "conversation"

    def csv_safe(value) -> str:
        text = "" if value is None else str(value)
        return "'" + text if text.startswith(("=", "+", "-", "@")) else text

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["sequence", "timestamp", "sender", "message", "viewer_part"])
        yield "\ufeff" + buffer.getvalue()
        for row in catalog.iter_messages(filename):
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow(
                [
                    row["sequence"],
                    csv_safe(row["timestamp"]),
                    csv_safe(row["sender"]),
                    csv_safe(row["body"]),
                    row["chunk_index"],
                ]
            )
            yield buffer.getvalue()

    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe_stem}.csv"'},
    )


@app.get("/packages/<path:filename>")
def package_file(filename: str):
    path = safe_path(PACKAGES_DIR, filename)
    if not path.is_file() or path.suffix.lower() != ".zip":
        abort(404)
    return send_from_directory(PACKAGES_DIR, filename, as_attachment=True)


@app.post("/package")
def start_package():
    relative = request.form.get("conversation", "")
    source = safe_path(CURRENT_EXPORT_DIR, relative)
    if not source.is_file() or source.suffix.lower() != ".html":
        abort(404)
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip("-") or "conversation"
    output = PACKAGES_DIR / f"{safe_stem}-{datetime.now():%Y%m%d-%H%M%S}.zip"

    def target() -> None:
        resources: dict[str, Path] = {relative: source}
        resources.update(
            collect_export_resources(
                source,
                progress=lambda scanned, found: jobs.update_progress(
                    "Scanning archive resources",
                    detail=f"{scanned:,} links scanned · {found:,} local files found",
                ),
                cancelled=jobs.is_cancelled,
            )
        )
        pdf_candidates = sorted(
            PDFS_DIR.glob(f"{safe_stem}-*.pdf"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        total_size = sum(path.stat().st_size for path in resources.values())
        if pdf_candidates:
            total_size += pdf_candidates[0].stat().st_size
        free = shutil.disk_usage(PACKAGES_DIR).free
        if total_size > free * 0.9:
            raise RuntimeError(
                f"Package needs about {human_size(total_size)}, but only {human_size(free)} is free"
            )
        total_items = len(resources) + (1 if pdf_candidates else 0)
        compressed_suffixes = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".mp4", ".mov", ".zip", ".pdf"}
        try:
            with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
                completed = 0
                for archive_name, path in resources.items():
                    compression = (
                        zipfile.ZIP_STORED
                        if path.suffix.lower() in compressed_suffixes
                        else zipfile.ZIP_DEFLATED
                    )
                    archive.write(path, archive_name, compress_type=compression, compresslevel=1)
                    completed += 1
                    jobs.update_progress(
                        "Creating portable archive",
                        completed,
                        total_items,
                        detail=f"{completed} of {total_items} files · {archive_name}",
                    )
                    if jobs.is_cancelled():
                        raise RuntimeError("Archive creation was cancelled")
                if pdf_candidates:
                    pdf = pdf_candidates[0]
                    archive.write(pdf, f"pdf/{pdf.name}", compress_type=zipfile.ZIP_STORED)
            jobs.append(f"Portable archive created: {output.name}")
        except BaseException:
            output.unlink(missing_ok=True)
            raise

    if not jobs.start("Portable archive", target):
        flash("Another job is already running.", "error")
    return redirect(url_for("index", conversation=relative))


@app.post("/support-bundle")
def start_support_bundle():
    output = PACKAGES_DIR / f"support-{datetime.now():%Y%m%d-%H%M%S}.zip"

    def target() -> None:
        jobs.update_progress("Collecting support details", 0, 2, detail="Reading tool versions")
        versions = {}
        for command in ("imessage-exporter", "idevicebackup2", "usbmuxd", "magick", "ffmpeg", "chromium"):
            if not shutil.which(command):
                versions[command] = "not installed"
                continue
            _, result = quick_command([command, "--version"])
            versions[command] = result.splitlines()[0][:300]
        settings = effective_settings()
        report = {
            "application": "iMessage Archive",
            "version": application_version(),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "platform": platform.platform(),
            "tools": versions,
            "devices": {
                "usb_count": len(list_devices(False)),
                "network_count": len(list_devices(True)),
            },
            "storage": [
                {
                    "label": item["label"],
                    "free_bytes": item["free"],
                    "total_bytes": item["total"],
                    "used_percent": item["percent"],
                }
                for item in storage_summaries()
            ],
            "backups": [
                {
                    "device": item["device"],
                    "ios": item["ios"],
                    "encrypted": item["encrypted"],
                    "modified": item["modified"],
                }
                for item in backup_summaries()
            ],
            "library": catalog.stats(),
            "automation": {
                "schedule_enabled": settings["schedule_enabled"],
                "schedule_interval_hours": settings["schedule_interval_hours"],
                "schedule_network": settings["schedule_network"],
                "schedule_full": settings["schedule_full"],
                "preferred_device_configured": bool(settings["schedule_device_id"]),
                "notification_configured": bool(settings["notify_webhook_url"]),
                "notify_on_success": settings["notify_on_success"],
                "export_retention_days": settings["export_retention_days"],
                "pdf_retention_days": settings["pdf_retention_days"],
            },
            "job": {
                key: value
                for key, value in jobs.snapshot().items()
                if key not in {"log"}
            },
        }
        jobs.update_progress("Writing support bundle", 1, 2, detail=output.name)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("support-report.json", json.dumps(report, indent=2, sort_keys=True))
        jobs.update_progress("Support bundle complete", 2, 2, 0, output.name)
        jobs.append("Support bundle contains no messages, passwords, webhook addresses, or pairing records.")

    if not jobs.start("Support bundle", target):
        flash("Another job is already running.", "error")
    return redirect(url_for("index"))


@app.get("/api/job")
def job_status():
    snapshot = jobs.snapshot()
    snapshot["pdfs"] = [
        {"name": name, "url": url_for("pdf_file", filename=name)}
        for name in generated_pdfs()
    ]
    snapshot["packages"] = [
        {"name": name, "url": url_for("package_file", filename=name)}
        for name in generated_packages()
    ]
    snapshot["texts"] = [
        {"name": name, "url": url_for("text_file", filename=name)}
        for name in generated_text_exports()
    ]
    return jsonify(snapshot)


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>iMessage Archive</title>
  <script>
    (() => {
      const saved = localStorage.getItem('imessage-theme');
      const dark = saved ? saved === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
      document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    })();
  </script>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f5; --panel: #ffffff; --panel-2: #eef2f0; --text: #15201d;
      --muted: #5c6b66; --border: #d5ddda; --accent: #176f62; --accent-hover: #10594f;
      --danger: #a83a19; --danger-hover: #862f15; --log: #101817; --log-text: #dce9e5;
      --focus: #db8b20;
    }
    :root[data-theme="dark"] {
      color-scheme: dark;
      --bg: #111513; --panel: #181e1b; --panel-2: #202824; --text: #edf4f1;
      --muted: #a5b3ae; --border: #34423c; --accent: #2d9b87; --accent-hover: #43ad99;
      --danger: #cf603d; --danger-hover: #df7655; --log: #090c0b; --log-text: #dce9e5;
      --focus: #e7a447;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.45 system-ui, sans-serif; }
    header { height: 58px; display: flex; align-items: center; justify-content: space-between; padding: 0 18px; background: var(--panel); border-bottom: 1px solid var(--border); }
    h1 { margin: 0; font-size: 18px; letter-spacing: 0; }
    h2 { margin: 22px 0 10px; font-size: 12px; text-transform: uppercase; color: var(--muted); letter-spacing: 0; }
    .layout { display: grid; grid-template-columns: minmax(320px, 390px) 1fr; height: calc(100vh - 58px); min-height: 0; }
    aside { min-height: 0; padding: 16px; background: var(--panel); border-right: 1px solid var(--border); overflow: auto; }
    main { display: flex; min-width: 0; min-height: 0; padding: 16px; flex-direction: column; overflow: hidden; }
    form { margin: 0 0 10px; }
    label { display: block; margin: 8px 0 4px; font-weight: 650; }
    input, select { width: 100%; min-height: 38px; padding: 8px 10px; color: var(--text); background: var(--panel); border: 1px solid var(--border); border-radius: 4px; }
    input:focus, select:focus, button:focus-visible, a:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
    button { width: 100%; min-height: 38px; padding: 8px 12px; color: white; background: var(--accent); border: 0; border-radius: 4px; font-weight: 700; cursor: pointer; }
    button:hover { background: var(--accent-hover); }
    button:disabled, select:disabled { opacity: .55; cursor: not-allowed; }
    button.secondary { color: var(--text); background: var(--panel-2); border: 1px solid var(--border); }
    button.danger { background: var(--danger); }
    button.danger:hover { background: var(--danger-hover); }
    .check { display: flex; align-items: center; gap: 8px; font-weight: 500; }
    .check input { width: 16px; min-height: 16px; margin: 0; }
    .theme { display: flex; align-items: center; gap: 8px; font-weight: 600; }
    .theme input { width: 36px; min-height: 20px; accent-color: var(--accent); }
    .header-action { margin: 0; }
    .header-action button { width: auto; min-height: 32px; padding: 5px 9px; color: var(--text); background: transparent; border: 1px solid var(--border); }
    .device, .conversation, .pdf, .search-result { display: block; padding: 9px 10px; margin: 5px 0; color: var(--text); background: var(--panel-2); border: 1px solid var(--border); border-radius: 4px; text-decoration: none; overflow-wrap: anywhere; }
    .conversation.active { border-color: var(--accent); box-shadow: inset 3px 0 var(--accent); }
    .conversation small, .search-result small { display: block; margin-top: 3px; color: var(--muted); }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .actions form { margin: 0; }
    .summary { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin: 8px 0; }
    .metric { padding: 8px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 4px; }
    .metric strong, .metric span { display: block; }
    .metric span { color: var(--muted); font-size: 11px; }
    .backup-item { margin: 6px 0; padding: 9px; border: 1px solid var(--border); border-radius: 4px; }
    .backup-item strong, .backup-item span { display: block; }
    details { margin: 8px 0; }
    summary { color: var(--muted); cursor: pointer; }
    details.section { margin: 20px 0 10px; border-top: 1px solid var(--border); padding-top: 10px; }
    details.section > summary { font-size: 12px; font-weight: 700; text-transform: uppercase; }
    .badge { display: inline-block; width: fit-content; margin-top: 4px; padding: 2px 6px; border: 1px solid var(--border); border-radius: 999px; color: var(--muted); font-size: 11px; }
    .badge.good { color: var(--accent); border-color: var(--accent); }
    .pager { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
    .pager a { padding: 6px 9px; color: var(--text); background: var(--panel-2); border: 1px solid var(--border); border-radius: 4px; text-decoration: none; }
    .download-link { display:inline-block; margin-top:7px; color:var(--accent); }
    .notice { padding: 10px; margin-bottom: 10px; background: var(--panel-2); border-left: 4px solid var(--accent); }
    .notice.error { border-color: var(--danger); }
    .status { padding: 7px 10px; border: 1px solid var(--border); border-radius: 999px; color: var(--muted); font-size: 12px; }
    .job-progress { margin: 8px 0 12px; padding: 10px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 4px; }
    .job-progress strong, .job-progress span { display: block; }
    .job-progress progress { width: 100%; height: 10px; margin: 8px 0 4px; accent-color: var(--accent); }
    .job-progress .timing { margin-top: 3px; color: var(--muted); font-size: 12px; }
    pre { max-height: 280px; overflow: auto; margin: 8px 0; padding: 12px; color: var(--log-text); background: var(--log); border-radius: 4px; white-space: pre-wrap; overflow-wrap: anywhere; }
    iframe { width: 100%; min-height: 0; height: auto; flex: 1; background: white; border: 1px solid var(--border); border-radius: 4px; }
    .viewer-bar { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: end; margin-bottom: 12px; }
    .viewer-bar > * { min-width: 0; }
    .viewer-bar form, .pdf-options { display: flex; gap: 10px; align-items: center; margin: 0; }
    .viewer-bar button { width: auto; white-space: nowrap; }
    .viewer-bar .check { white-space: nowrap; }
    .pdf-options select { width: auto; min-width: 130px; }
    .muted { color: var(--muted); }
    hr { border: 0; border-top: 1px solid var(--border); margin: 16px 0; }
    @media (max-width: 820px) {
      .layout { grid-template-columns: 1fr; height: auto; }
      aside { border-right: 0; border-bottom: 1px solid var(--border); }
      main { display: block; min-height: 70vh; overflow: visible; }
      .viewer-bar { grid-template-columns: 1fr; }
      .viewer-bar form { align-items: flex-start; flex-direction: column; }
      .viewer-bar > div, .viewer-bar form, .pdf-options { width: 100%; min-width: 0; }
      .pager, .pdf-options { flex-wrap: wrap; }
      .pdf-options select { width: 100%; }
      iframe { height: 75vh; }
    }
  </style>
</head>
<body>
<header>
  <h1>iMessage Archive</h1>
  <div style="display:flex;align-items:center;gap:14px">
    <label class="theme"><input id="themeToggle" type="checkbox"> Dark mode</label>
    <span class="status" id="jobStatus">{{ job.phase }}{% if job.busy %} · running{% endif %}</span>
    {% if auth_enabled %}<form class="header-action" method="post" action="{{ url_for('logout') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button>Sign out</button></form>{% endif %}
  </div>
</header>
<div class="layout">
  <aside>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category, message in messages %}<div class="notice {{ category }}">{{ message }}</div>{% endfor %}
    {% endwith %}

    <div class="actions">
      <form method="post" action="{{ url_for('start_preflight') }}"><button class="secondary">Run preflight</button></form>
      <form method="post" action="{{ url_for('start_library_index') }}"><button class="secondary">Refresh library</button></form>
    </div>

    <h2>Device</h2>
    {% for device in usb_devices %}<div class="device">{{ device }} · USB</div>{% else %}<p class="muted">No USB device detected</p>{% endfor %}
    {% for device in network_devices %}<div class="device">{{ device }} · network</div>{% endfor %}
    <form method="post" action="{{ url_for('device_action', action='validate') }}">
      {% if usb_devices|length > 1 %}<select name="device_id" aria-label="USB device to validate">{% for device in usb_devices %}<option value="{{ device }}">{{ device }}</option>{% endfor %}</select>{% endif %}
      <button class="secondary"{% if not usb_devices %} disabled{% endif %}>Validate trust</button>
    </form>
    <form method="post" action="{{ url_for('device_action', action='pair') }}">
      {% if usb_devices|length > 1 %}<select name="device_id" aria-label="USB device to pair">{% for device in usb_devices %}<option value="{{ device }}">{{ device }}</option>{% endfor %}</select>{% endif %}
      <button{% if not usb_devices %} disabled{% endif %}>Pair device</button>
    </form>

    <h2>Backup</h2>
    <form method="post" action="{{ url_for('enable_encryption') }}">
      <label>iPhone</label>
      <select name="device_target"{% if not device_targets %} disabled{% endif %}>{% for device in device_targets %}<option value="{{ device.value }}">{{ device.label }}</option>{% endfor %}</select>
      <label>Backup encryption password</label>
      <input type="password" name="backup_password" autocomplete="new-password">
      <label>Confirm backup encryption password</label>
      <input type="password" name="backup_password_confirm" autocomplete="new-password">
      <button class="danger"{% if not device_targets %} disabled{% endif %}>Enable encrypted backups</button>
    </form>
    <form method="post" action="{{ url_for('start_backup') }}">
      <label>iPhone</label>
      <select name="device_target"{% if not device_targets %} disabled{% endif %}>{% for device in device_targets %}<option value="{{ device.value }}">{{ device.label }}</option>{% endfor %}</select>
      <label class="check"><input type="checkbox" name="full"> Force full backup</label>
      <button{% if not device_targets %} disabled{% endif %}>Start backup</button>
    </form>

    {% for backup in backup_items %}
      <div class="backup-item">
        <strong>{{ backup.device }}</strong>
        <span class="muted">iOS {{ backup.ios }} · {{ backup.modified }}</span>
        <span class="badge {% if backup.encrypted %}good{% endif %}">{{ 'Encrypted' if backup.encrypted else 'Not encrypted' }}</span>
        {% if backup.diagnostics %}
        <details>
          <summary>Message and attachment health</summary>
          {% for label, value in backup.diagnostics %}<div class="metric"><strong>{{ value }}</strong><span>{{ label }}</span></div>{% endfor %}
        </details>
        {% endif %}
        {% if can_write %}
        <details>
          <summary>Delete this backup</summary>
          <form method="post" action="{{ url_for('start_backup_deletion') }}">
            <input type="hidden" name="backup_path" value="{{ backup.path }}">
            <label>Type {{ backup.folder }} to confirm</label>
            <input name="confirm_backup" autocomplete="off">
            <button class="danger">Delete backup</button>
          </form>
        </details>
        {% endif %}
      </div>
    {% endfor %}
    <p class="muted">Scheduled backup: {{ schedule_status }}</p>
    {% for run in backup_runs[:3] %}
      <p class="muted">{{ run.started_at }} · {{ run.mode }}{% if run.network %} over Wi-Fi{% endif %} · {{ run.status }}</p>
    {% endfor %}

    <h2>Storage</h2>
    {% for storage in storage_items %}
      <div class="metric"><strong>{{ storage.free_display }} free</strong><span>{{ storage.label }} · {{ storage.percent }}% used</span></div>
    {% endfor %}
    <form method="post" action="{{ url_for('start_storage_cleanup') }}"><button class="secondary">Apply retention cleanup</button></form>

    {% if can_write %}
    <details class="section">
    <summary>Automation and support</summary>
    <form method="post" action="{{ url_for('save_settings') }}">
      <label class="check"><input type="checkbox" name="schedule_enabled"{% if settings.schedule_enabled %} checked{% endif %}> Scheduled backups</label>
      <label>Run every</label>
      <select name="schedule_interval_hours">
        {% for hours, label in [(6, '6 hours'), (12, '12 hours'), (24, 'Day'), (168, 'Week')] %}<option value="{{ hours }}"{% if settings.schedule_interval_hours == hours %} selected{% endif %}>{{ label }}</option>{% endfor %}
      </select>
      <label class="check"><input type="checkbox" name="schedule_network"{% if settings.schedule_network %} checked{% endif %}> Use paired Wi-Fi device</label>
      <label>Preferred iPhone</label>
      <select name="schedule_device_id">
        <option value="">Any available paired iPhone</option>
        {% for device_id in device_ids %}<option value="{{ device_id }}"{% if settings.schedule_device_id == device_id %} selected{% endif %}>{{ device_id }}</option>{% endfor %}
        {% if settings.schedule_device_id and settings.schedule_device_id not in device_ids %}<option value="{{ settings.schedule_device_id }}" selected>{{ settings.schedule_device_id }} · currently offline</option>{% endif %}
      </select>
      <label class="check"><input type="checkbox" name="schedule_full"{% if settings.schedule_full %} checked{% endif %}> Force full scheduled backup</label>
      <label>Notification webhook</label>
      <input type="url" name="notify_webhook_url" placeholder="{{ 'Configured; enter a new address to replace' if settings.notification_configured else 'Optional HTTPS address' }}">
      {% if settings.notification_configured %}<label class="check"><input type="checkbox" name="clear_webhook"> Remove saved webhook</label>{% endif %}
      <label class="check"><input type="checkbox" name="notify_on_success"{% if settings.notify_on_success %} checked{% endif %}> Notify when jobs succeed</label>
      <div class="row">
        <div><label>Export retention days</label><input type="number" min="0" max="3650" name="export_retention_days" value="{{ settings.export_retention_days }}"></div>
        <div><label>PDF retention days</label><input type="number" min="0" max="3650" name="pdf_retention_days" value="{{ settings.pdf_retention_days }}"></div>
      </div>
      <button class="secondary">Save automation</button>
    </form>
    <form method="post" action="{{ url_for('start_support_bundle') }}"><button class="secondary">Create support bundle</button></form>
    </details>
    {% endif %}

    {% if backups %}
    <h2>Export</h2>
    <form method="post" action="{{ url_for('start_diagnostics') }}">
      <label>Backup</label>
      <select name="backup_path">{% for backup in backups %}<option value="{{ backup }}">{{ backup }}</option>{% endfor %}</select>
      <label>Backup password</label>
      <input type="password" name="backup_password" autocomplete="current-password">
      <button class="secondary">Run diagnostics</button>
      <button class="secondary" formaction="{{ url_for('start_backup_verification') }}">Verify backup</button>
    </form>
    {% if diagnostics_items %}
      {% for label, value in diagnostics_items %}<div class="metric"><strong>{{ value }}</strong><span>{{ label }}</span></div>{% endfor %}
    {% endif %}
    <hr>
    <form method="post" action="{{ url_for('start_export') }}">
      <label>Backup</label>
      <select name="backup_path">{% for backup in backups %}<option value="{{ backup }}">{{ backup }}</option>{% endfor %}</select>
      <label>Backup password</label>
      <input type="password" name="backup_password" autocomplete="current-password">
      <label>Phone number, email, or chat filter</label>
      <input name="handle">
      <label>Export formats</label>
      <select name="export_formats">
        <option value="html">Browsable HTML</option>
        <option value="both">HTML and text</option>
        <option value="txt">Text only</option>
      </select>
      <label>Attachment handling</label>
      <select name="copy_method">
        <option value="clone">Original files</option>
        <option value="basic">Browser-compatible images</option>
        <option value="full">Convert images, audio, and video</option>
        <option value="disabled">No attachment copies</option>
      </select>
      <div class="row">
        <div><label>Start date</label><input type="date" name="start_date"></div>
        <div><label>End date</label><input type="date" name="end_date"></div>
      </div>
      <label class="check"><input type="checkbox" name="no_lazy" checked> PDF-ready images</label>
      <button>Start message export</button>
    </form>
    {% endif %}

    <h2>Message library</h2>
    <div class="summary">
      <div class="metric"><strong>{{ library_stats.conversations }}</strong><span>Chats</span></div>
      <div class="metric"><strong>{{ library_stats.messages }}</strong><span>Messages</span></div>
      <div class="metric"><strong>{{ library_stats.missing_attachments }}</strong><span>Missing</span></div>
    </div>
    <form method="get" action="{{ url_for('index') }}">
      <label>Search messages</label>
      <input type="search" name="q" value="{{ search_query }}" placeholder="Words, sender, phone, or email">
    </form>
    {% if search_query %}
      {% for result in search_results %}
        <a class="search-result" href="{{ url_for('index', conversation=result.conversation_path, page=result.chunk_index, q=search_query) }}">
          <strong>{{ result.sender or result.conversation_path }}</strong>
          <span>{{ result.excerpt }}</span>
          <small>{{ result.timestamp }}</small>
        </a>
      {% else %}<p class="muted">No indexed messages matched.</p>{% endfor %}
    {% endif %}

    <h2>Conversations</h2>
    {% for conversation in conversations %}
      {% if conversation.message_count is not none %}
      <a class="conversation {% if conversation.path == selected %}active{% endif %}" href="{{ url_for('index', conversation=conversation.path) }}">
        <strong>{{ conversation.title }}</strong>
        <small>{{ conversation.message_count }} messages · {{ conversation.chunk_count }} fast-loading parts{% if conversation.missing_attachments %} · {{ conversation.missing_attachments }} missing attachments{% endif %}</small>
      </a>
      {% else %}
      <div class="conversation"><strong>{{ conversation.title }}</strong><small>Refresh the library before opening this conversation</small></div>
      {% endif %}
    {% else %}<p class="muted">No exported conversations</p>{% endfor %}

    <h2>PDFs</h2>
    <div id="pdfList">{% for pdf in pdfs %}<a class="pdf" href="{{ url_for('pdf_file', filename=pdf) }}">{{ pdf }}</a>{% else %}<p class="muted">No PDFs generated</p>{% endfor %}</div>

    <h2>Text exports</h2>
    <div id="textList">{% for item in texts %}<a class="pdf" href="{{ url_for('text_file', filename=item) }}">{{ item }}</a>{% else %}<p class="muted">No text exports generated</p>{% endfor %}</div>

    <h2>Portable archives</h2>
    <div id="packageList">{% for package in packages %}<a class="pdf" href="{{ url_for('package_file', filename=package) }}">{{ package }}</a>{% else %}<p class="muted">No archives generated</p>{% endfor %}</div>

    <details class="section"{% if job.busy %} open{% endif %}>
    <summary>Job log</summary>
    <div class="job-progress" id="jobProgress">
      <strong id="jobPhase">{{ job.phase }}</strong>
      <progress id="jobProgressBar"{% if job.progress_total %} max="{{ job.progress_total }}" value="{{ job.progress_current or 0 }}"{% endif %}></progress>
      <span id="jobDetail">{{ job.progress_detail }}</span>
      <span class="timing" id="jobTiming"></span>
    </div>
    <p class="muted" id="logSummary"{% if not job.truncated %} hidden{% endif %}>Showing the newest {{ log_limit }} lines; <span id="hiddenLogCount">{{ job.truncated }}</span> older lines hidden.</p>
    <pre id="jobLog">{{ job.log or 'No job output' }}</pre>
    <form id="cancelForm" method="post" action="{{ url_for('cancel_job') }}"{% if not job.busy %} hidden{% endif %}><button class="danger">Cancel current job</button></form>
    {% for run in job_runs[:5] %}<p class="muted">{{ run.started_at }} · {{ run.label }} · {{ run.status }} · {{ run.elapsed_seconds }}s</p>{% endfor %}
    </details>
  </aside>
  <main>
    {% if selected %}
      <div class="viewer-bar">
        <div>
          <strong>{{ selected_record.title if selected_record else selected }}</strong>
          {% if selected_record and selected_record.chunk_count > 1 %}
            <div class="pager">
              {% if page > 1 %}<a href="{{ url_for('index', conversation=selected, page=page - 1) }}">Previous</a>{% endif %}
              <span>Part {{ page }} of {{ selected_record.chunk_count }}</span>
              {% if page < selected_record.chunk_count %}<a href="{{ url_for('index', conversation=selected, page=page + 1) }}">Next</a>{% endif %}
            </div>
          {% endif %}
          {% if selected_record %}<a class="download-link" href="{{ url_for('conversation_csv', filename=selected) }}">Download CSV</a>{% endif %}
          <form method="post" action="{{ url_for('start_package') }}"><input type="hidden" name="conversation" value="{{ selected }}"><button class="secondary">Create ZIP archive</button></form>
        </div>
        <form method="post" action="{{ url_for('start_pdf') }}">
          <input type="hidden" name="conversation" value="{{ selected }}">
          <div class="pdf-options">
            {% if selected_record and selected_record.chunk_count > 1 %}<label class="check"><input type="checkbox" name="chunked" checked> Reliable parts</label>{% endif %}
            <label class="check"><input id="includeImages" type="checkbox" name="include_images"> Include images</label>
            <select id="pdfProfile" name="pdf_profile" aria-label="PDF image quality">
              <option value="fast">Fast</option>
              <option value="balanced" selected>Balanced</option>
              <option value="archive">Archive quality</option>
            </select>
          </div>
          <button>Render PDF</button>
        </form>
      </div>
      <iframe id="conversationFrame" src="{{ url_for('conversation_view', filename=viewer_filename) }}" title="Conversation" sandbox="allow-same-origin" referrerpolicy="no-referrer"></iframe>
    {% else %}
      <p class="muted">Select an exported conversation.</p>
    {% endif %}
  </main>
</div>
<script>
  const csrfToken = {{ csrf_token|tojson }};
  document.querySelectorAll('form[method="post"]').forEach(form => {
    if (form.querySelector('input[name="csrf_token"]')) return;
    const field = document.createElement('input');
    field.type = 'hidden';
    field.name = 'csrf_token';
    field.value = csrfToken;
    form.appendChild(field);
  });
  const toggle = document.getElementById('themeToggle');
  toggle.checked = document.documentElement.dataset.theme === 'dark';
  toggle.addEventListener('change', () => {
    const theme = toggle.checked ? 'dark' : 'light';
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('imessage-theme', theme);
  });
  const includeImages = document.getElementById('includeImages');
  const pdfProfile = document.getElementById('pdfProfile');
  if (includeImages && pdfProfile) {
    const syncImageOptions = () => { pdfProfile.disabled = !includeImages.checked; };
    includeImages.addEventListener('change', syncImageOptions);
    syncImageOptions();
  }

  const formatDuration = seconds => {
    seconds = Math.max(0, Math.round(seconds || 0));
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
  };

  let jobWasBusy = {{ 'true' if job.busy else 'false' }};
  const renderJob = job => {
    document.getElementById('jobStatus').textContent = `${job.phase}${job.busy ? ' · running' : ''}`;
    document.getElementById('jobPhase').textContent = job.phase;
    document.getElementById('jobDetail').textContent = job.progress_detail || '';
    const timing = [`Elapsed ${formatDuration(job.elapsed_seconds)}`];
    if (job.busy && job.eta_seconds !== null) timing.push(`ETA about ${formatDuration(job.eta_seconds)}`);
    document.getElementById('jobTiming').textContent = timing.join(' · ');

    const progress = document.getElementById('jobProgressBar');
    if (job.progress_total) {
      progress.max = job.progress_total;
      progress.value = job.progress_current || 0;
    } else {
      progress.removeAttribute('value');
      progress.removeAttribute('max');
    }

    document.getElementById('jobLog').textContent = job.log || 'No job output';
    const summary = document.getElementById('logSummary');
    summary.hidden = !job.truncated;
    document.getElementById('hiddenLogCount').textContent = job.truncated || 0;
    document.getElementById('cancelForm').hidden = !job.busy;

    if (job.pdfs) {
      const list = document.getElementById('pdfList');
      list.replaceChildren();
      if (!job.pdfs.length) {
        const empty = document.createElement('p');
        empty.className = 'muted';
        empty.textContent = 'No PDFs generated';
        list.appendChild(empty);
      } else {
        job.pdfs.forEach(pdf => {
          const link = document.createElement('a');
          link.className = 'pdf';
          link.href = pdf.url;
          link.textContent = pdf.name;
          list.appendChild(link);
        });
      }
    }
    if (job.packages) {
      const list = document.getElementById('packageList');
      list.replaceChildren();
      if (!job.packages.length) {
        const empty = document.createElement('p');
        empty.className = 'muted';
        empty.textContent = 'No archives generated';
        list.appendChild(empty);
      } else {
        job.packages.forEach(item => {
          const link = document.createElement('a');
          link.className = 'pdf';
          link.href = item.url;
          link.textContent = item.name;
          list.appendChild(link);
        });
      }
    }
    if (job.texts) {
      const list = document.getElementById('textList');
      list.replaceChildren();
      if (!job.texts.length) {
        const empty = document.createElement('p');
        empty.className = 'muted';
        empty.textContent = 'No text exports generated';
        list.appendChild(empty);
      } else {
        job.texts.forEach(item => {
          const link = document.createElement('a');
          link.className = 'pdf';
          link.href = item.url;
          link.textContent = item.name;
          list.appendChild(link);
        });
      }
    }
    jobWasBusy = job.busy;
  };

  const pollJob = async () => {
    try {
      const response = await fetch('{{ url_for('job_status') }}', {cache: 'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const job = await response.json();
      renderJob(job);
      if (job.busy) setTimeout(pollJob, 2000);
    } catch (_) {
      if (jobWasBusy) setTimeout(pollJob, 4000);
    }
  };
  renderJob({{ job|tojson }});
  if (jobWasBusy) setTimeout(pollJob, 1000);
</script>
</body>
</html>
"""


@app.get("/")
def index():
    exported = exported_conversations()
    indexed = {item["path"]: item for item in catalog.conversations()}
    conversations = []
    for relative in exported:
        record = indexed.get(relative)
        conversations.append(
            record
            or {
                "path": relative,
                "title": Path(relative).stem,
                "message_count": None,
                "chunk_count": 0,
                "missing_attachments": None,
            }
        )
    conversations.sort(key=lambda item: item["title"].casefold())
    selected = request.args.get("conversation", "")
    if selected not in indexed:
        selected = ""
    selected_record = indexed.get(selected)
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    viewer_filename = selected
    if selected_record and selected_record["chunk_count"]:
        page = min(page, selected_record["chunk_count"])
        selected_chunk = catalog.chunk(selected, page)
        if selected_chunk:
            viewer_filename = selected_chunk["path"]
    search_query = request.args.get("q", "").strip()
    search_results = catalog.search(search_query) if search_query else []
    storage_items = storage_summaries()
    for item in storage_items:
        item["free_display"] = human_size(item["free"])
    schedule_state = read_schedule_state()
    settings = effective_settings()
    schedule_status = schedule_state.get(
        "last_status",
        "enabled; waiting for first run" if settings["schedule_enabled"] else "disabled",
    )
    backup_items = backup_summaries()
    backup_paths = [item["path"] for item in backup_items]
    for item in backup_items:
        saved = catalog.diagnostics(item["path"])
        item["diagnostics"] = diagnostic_summary(saved["output"]) if saved else []
    saved_diagnostics = catalog.diagnostics(backup_paths[0]) if backup_paths else None
    diagnostics_items = (
        diagnostic_summary(saved_diagnostics["output"]) if saved_diagnostics else []
    )
    usb_devices, network_devices = device_lists()
    device_targets = [
        {"value": f"usb:{device_id}", "label": f"{device_id} · USB"}
        for device_id in usb_devices
    ] + [
        {"value": f"network:{device_id}", "label": f"{device_id} · Wi-Fi"}
        for device_id in network_devices
    ]
    device_ids = sorted(set(usb_devices + network_devices))
    return render_template_string(
        PAGE,
        usb_devices=usb_devices,
        network_devices=network_devices,
        device_targets=device_targets,
        device_ids=device_ids,
        backups=backup_paths,
        backup_items=backup_items,
        backup_runs=catalog.backup_runs(),
        job_runs=catalog.job_runs(),
        schedule_status=schedule_status,
        settings={
            **settings,
            "notification_configured": bool(settings["notify_webhook_url"]),
        },
        can_write=getattr(g, "role", "admin") == "admin",
        diagnostics_items=diagnostics_items,
        storage_items=storage_items,
        conversations=conversations,
        selected=selected,
        selected_record=selected_record,
        viewer_filename=viewer_filename,
        page=page,
        search_query=search_query,
        search_results=search_results,
        library_stats=catalog.stats(),
        csrf_token=session_csrf_token(),
        auth_enabled=bool(os.environ.get("APP_PASSWORD", ""))
        and os.environ.get("APP_PASSWORD") != "change-me-before-lan-use",
        pdfs=generated_pdfs(),
        texts=generated_text_exports(),
        packages=generated_packages(),
        job=jobs.snapshot(),
        log_limit=LOG_LINES,
    )


schedule_state_lock = threading.RLock()


def read_schedule_state() -> dict:
    with schedule_state_lock:
        try:
            value = json.loads(SCHEDULE_STATE_FILE.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}


def write_schedule_state(state: dict) -> None:
    with schedule_state_lock:
        temporary = SCHEDULE_STATE_FILE.with_name(SCHEDULE_STATE_FILE.name + ".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(SCHEDULE_STATE_FILE)


def scheduled_backup_tick(now: float | None = None) -> str:
    settings = effective_settings()
    if not settings["schedule_enabled"]:
        return "disabled"
    current_time = time.time() if now is None else now
    state = read_schedule_state()
    next_attempt = float(state.get("next_attempt", 0))
    if current_time < next_attempt:
        return "not-due"
    devices = list_devices(settings["schedule_network"], refresh=True)
    preferred_device = settings["schedule_device_id"]
    if preferred_device:
        devices = [device for device in devices if device == preferred_device]
    if not devices:
        state.update(
            {
                "last_status": "waiting for paired iPhone",
                "last_checked": current_time,
                "next_attempt": current_time + 600,
            }
        )
        write_schedule_state(state)
        return "waiting"
    if jobs.snapshot()["busy"]:
        return "busy"
    state.update(
        {
            "last_status": "scheduled backup starting",
            "last_checked": current_time,
            "next_attempt": current_time + 3600,
        }
    )
    write_schedule_state(state)

    def target() -> None:
        try:
            run_iphone_backup(
                full=settings["schedule_full"],
                network=settings["schedule_network"],
                device_id=devices[0],
            )
        except Exception:
            failed_state = read_schedule_state()
            failed_state.update(
                {
                    "last_status": "scheduled backup failed",
                    "next_attempt": time.time() + 3600,
                }
            )
            write_schedule_state(failed_state)
            raise
        completed_state = read_schedule_state()
        completed_state.update(
            {
                "last_status": "scheduled backup completed",
                "last_success": time.time(),
                "next_attempt": time.time()
                + settings["schedule_interval_hours"] * 3600,
            }
        )
        write_schedule_state(completed_state)

    jobs.start("Scheduled iPhone backup", target)
    return "started"


def scheduled_backup_loop() -> None:
    while True:
        scheduled_backup_tick()
        time.sleep(60)


threading.Thread(target=scheduled_backup_loop, daemon=True, name="backup-scheduler").start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
