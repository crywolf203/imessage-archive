"""Exercise a running, disposable image without device data or test doubles."""

import base64
import csv
import io
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from pypdf import PdfReader


BASE_URL = "http://127.0.0.1:8080"
EXPORT_ROOT = Path("/data/exports/current")
PASSWORD = os.environ["APP_PASSWORD"]
USERNAME = os.environ.get("APP_USER", "runtime-test")
AUTHORIZATION = "Basic " + base64.b64encode(
    f"{USERNAME}:{PASSWORD}".encode()
).decode()
TIMINGS = {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def request(path, data=None, authenticated=True):
    headers = {"Authorization": AUTHORIZATION} if authenticated else {}
    payload = urllib.parse.urlencode(data).encode() if data is not None else None
    query = urllib.request.Request(BASE_URL + path, data=payload, headers=headers)
    try:
        with OPENER.open(query, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def job_result(timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, payload = request("/api/job")
        assert status == 200, (status, payload)
        result = json.loads(payload)
        if not result["busy"]:
            assert result["status"] == "success", result
            return result
        time.sleep(0.2)
    raise AssertionError("The sample job exceeded its runtime deadline")


def start_job(path, data=None):
    status, payload = request(path, data=data or {})
    assert status == 302, (path, status, payload)
    return job_result()


def command(*arguments):
    result = subprocess.run(
        arguments, check=True, capture_output=True, text=True, timeout=60
    )
    return result.stdout


def fixture(name, count, images=False):
    with (EXPORT_ROOT / name).open("w", encoding="utf-8") as output:
        output.write(
            '<html><head><style>body{font:14px/1.5 system-ui,sans-serif;padding:16px}'
            '.message{display:flex;margin:12px 0;break-inside:avoid}'
            '.sent{margin-left:auto;max-width:80%;background:#dcefe9;padding:12px 16px;border-radius:8px}'
            '.timestamp{display:block;font-size:11px;color:#45564f}'
            '.sender{display:block;font-weight:600}.bubble{display:block}'
            'img{width:40px;height:40px;margin-top:8px}</style></head><body>'
        )
        for index in range(count):
            image = (
                '<img src="attachments/fixture.png">'
                if images and index % 150 == 0 else ""
            )
            sentinel = " finalsentinel" if index == count - 1 else ""
            output.write(
                f'<div class="message"><div class="sent">'
                '<span class="timestamp">Jan 1, 2025 12:00 PM</span>'
                '<span class="sender">Runtime sender</span>'
                '<div class="message_part"><span class="bubble">'
                f'Runtime sample {index}{sentinel}</span>{image}</div></div></div>'
            )
        output.write("</body></html>")


for _ in range(60):
    try:
        if request("/healthz", authenticated=False)[0] == 200:
            break
    except urllib.error.URLError:
        pass
    time.sleep(0.5)
else:
    raise AssertionError("The container did not become healthy")

assert request("/api/job", authenticated=False)[0] == 401
assert request("/", authenticated=False)[0] == 302
assert "4.3.0" in command("imessage-exporter", "--version")
command("idevicebackup2", "--version")
command("usbmuxd", "--version")
command("ffmpeg", "-version")
command("chromium", "--version")

EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
assert not list(EXPORT_ROOT.glob("*.html")), "Use a fresh disposable container"
attachments = EXPORT_ROOT / "attachments"
attachments.mkdir(exist_ok=True)
command("convert", "-size", "40x40", "xc:seagreen", str(attachments / "fixture.png"))
command("convert", str(attachments / "fixture.png"), "/tmp/runtime-fixture.heic")
command("magick", "/tmp/runtime-fixture.heic", "/tmp/runtime-fixture.jpg")
assert Path("/tmp/runtime-fixture.jpg").stat().st_size > 0
fixture("runtime-large.html", 10000)
fixture("runtime-pdf.html", 450, images=True)

started = time.monotonic()
start_job("/library/index")
TIMINGS["index_10450_messages_seconds"] = round(time.monotonic() - started, 3)
started = time.monotonic()
status, page = request("/?conversation=runtime-large.html&q=finalsentinel")
assert status == 200 and b"Part 1 of 34" in page and b"finalsentinel" in page
assert len(page) < 2 * 1024 * 1024
TIMINGS["indexed_search_page_seconds"] = round(time.monotonic() - started, 3)
status, csv_output = request("/csv/runtime-large.html")
assert status == 200
assert sum(1 for _ in csv.reader(io.StringIO(csv_output.decode("utf-8-sig")))) == 10001

for include_images in (False, True):
    started = time.monotonic()
    result = start_job(
        "/pdf",
        {
            "conversation": "runtime-pdf.html",
            "chunked": "on",
            "pdf_profile": "fast",
            **({"include_images": "on"} if include_images else {}),
        },
    )
    TIMINGS[f"pdf_{'images' if include_images else 'text'}_seconds"] = round(
        time.monotonic() - started, 3
    )
    pdf_url = result["pdfs"][0]["url"]
    status, pdf_content = request(pdf_url)
    assert status == 200 and pdf_content.startswith(b"%PDF-")
    reader = PdfReader(io.BytesIO(pdf_content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "Runtime sample 0" in text and "Runtime sample 449" in text
    if include_images:
        assert any(len(page.images) for page in reader.pages), "PDF images were omitted"

result = start_job("/package", {"conversation": "runtime-pdf.html"})
status, package = request(result["packages"][0]["url"])
assert status == 200
with zipfile.ZipFile(io.BytesIO(package)) as archive:
    assert "runtime-pdf.html" in archive.namelist()
    assert "attachments/fixture.png" in archive.namelist()
    assert any(name.startswith("pdf/") for name in archive.namelist())

Path("/tmp/runtime-check-results.json").write_text(
    json.dumps({"status": "passed", "timings": TIMINGS}, indent=2) + "\n",
    encoding="utf-8",
)
print(json.dumps({"status": "passed", "timings": TIMINGS}, indent=2))
