import os
import sys
import tempfile
from pathlib import Path

from bs4 import BeautifulSoup


REPOSITORY = Path(__file__).resolve().parents[1]
TEMPORARY = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
ROOT = Path(TEMPORARY.name)
os.environ.update(
    {
        "BACKUPS_DIR": str(ROOT / "backups"),
        "EXPORTS_DIR": str(ROOT / "exports"),
        "PDFS_DIR": str(ROOT / "pdfs"),
        "CONFIG_DIR": str(ROOT / "config"),
        "APP_USER": "admin",
        "APP_PASSWORD": "admin-test-password",
        "VIEWER_USER": "viewer",
        "VIEWER_PASSWORD": "viewer-test-password",
        "FLASK_SECRET_KEY": "test-secret-key-not-for-production",
    }
)
sys.path.insert(0, str(REPOSITORY / "app"))

import app as module


module.list_devices = lambda network=False, refresh=False: []
admin = module.app.test_client()
assert admin.get("/").status_code == 302
login_page = admin.get("/login")
token = BeautifulSoup(login_page.data, "html.parser").find(
    "input", {"name": "csrf_token"}
)["value"]
assert admin.post(
    "/login",
    data={
        "username": "admin",
        "password": "admin-test-password",
        "csrf_token": token,
        "next": "/",
    },
).status_code == 302
home = admin.get("/")
assert home.status_code == 200
assert home.headers["X-Content-Type-Options"] == "nosniff"
assert home.headers["X-Frame-Options"] == "SAMEORIGIN"
assert admin.post("/logout").status_code == 403

viewer = module.app.test_client()
login_page = viewer.get("/login")
token = BeautifulSoup(login_page.data, "html.parser").find(
    "input", {"name": "csrf_token"}
)["value"]
assert viewer.post(
    "/login",
    data={
        "username": "viewer",
        "password": "viewer-test-password",
        "csrf_token": token,
        "next": "/",
    },
).status_code == 302
with viewer.session_transaction() as state:
    viewer_csrf = state["csrf_token"]
assert viewer.post(
    "/library/index", data={"csrf_token": viewer_csrf}
).status_code == 403

print("authentication and authorization smoke tests passed")
