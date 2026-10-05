"""Validate the Unraid template against the production Compose contract."""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


root = Path(__file__).resolve().parents[1]
template = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "unraid/imessage-archive.xml"
container = ET.parse(template).getroot()
assert container.tag == "Container" and container.get("version") == "2"
for field in ("Name", "Repository", "Overview", "Category", "WebUI", "Support", "Project", "TemplateURL", "Icon", "License", "Requires"):
    assert (container.findtext(field) or "").strip(), field
assert container.findtext("Name") == "imessage-archive"
assert container.findtext("Repository") == "ghcr.io/crywolf203/imessage-archive:latest"
assert container.findtext("Privileged") == "true"
assert container.findtext("TemplateURL").endswith("/unraid-templates/main/templates/imessage-archive.xml")
for field in ("Support", "Project", "TemplateURL", "Icon", "License", "ReadMe"):
    assert container.findtext(field).startswith("https://"), field

configs = {entry.get("Target"): entry for entry in container.findall("Config")}
assert len(configs) == len(container.findall("Config")), "Duplicate configuration targets"
for entry in configs.values():
    assert (entry.text or "") == entry.get("Default"), entry.attrib
for key in ("APP_PASSWORD", "FLASK_SECRET_KEY"):
    assert configs[key].get("Required") == "true"
    assert configs[key].get("Mask") == "true"
    assert configs[key].get("Default") == ""
assert configs["/dev/bus/usb"].get("Type") == "Path"

compose_services = []
for filename in ("docker-compose.yml", "docker-compose.image.yml"):
    result = subprocess.run(
        ["docker", "compose", "--env-file", ".env.example", "-f", filename, "config", "--format", "json"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    service = json.loads(result.stdout)["services"]["imessage-archive"]
    compose_services.append(service)
    assert service["privileged"]
    assert service["healthcheck"]["test"]
    assert service["labels"]["net.unraid.docker.icon"] == container.findtext("Icon")
    assert any(port["target"] == 8080 for port in service["ports"])
    for volume in service["volumes"]:
        entry = configs[volume["target"]]
        assert entry.get("Default") == volume["source"], volume
        assert not volume.get("read_only", False)
    for entry in configs.values():
        if entry.get("Type") == "Variable" and entry.get("Default"):
            if entry.get("Target") == "APP_USER":
                continue
            assert entry.get("Default") == service["environment"][entry.get("Target")], entry.attrib

for field in ("environment", "volumes", "ports", "labels", "healthcheck"):
    assert compose_services[0][field] == compose_services[1][field], field

result = subprocess.run(
    ["docker", "compose", "--env-file", ".env.example", "-f", "docker-compose.image.yml", "-f", "tests/compose.ci.yml", "config", "--format", "json"],
    cwd=root, capture_output=True, text=True, check=True,
)
ci = json.loads(result.stdout)["services"]["imessage-archive"]
assert not ci.get("privileged", False)
assert not ci.get("container_name")
assert ci["environment"]["START_USBMUXD"] == "0"
assert ci["environment"]["SCHEDULE_ENABLED"] == "0"
assert all(port["host_ip"] == "127.0.0.1" for port in ci["ports"])
for volume in ci["volumes"]:
    assert volume["type"] == "volume" or (
        volume["target"] == "/verification" and volume.get("read_only")
    ), volume
result = subprocess.run(
    ["docker", "compose", "--env-file", ".env.staging.example", "-f", "docker-compose.staging.yml", "config", "--format", "json"],
    cwd=root, capture_output=True, text=True, check=True,
)
staging = json.loads(result.stdout)["services"]["imessage-archive-staging"]
assert not staging.get("privileged", False)
assert staging["container_name"] != compose_services[0]["container_name"]
assert staging["environment"]["START_USBMUXD"] == "0"
assert staging["environment"]["SCHEDULE_ENABLED"] == "0"
assert all(volume["type"] == "volume" for volume in staging["volumes"])
assert all(port["published"] != compose_services[0]["ports"][0]["published"] for port in staging["ports"])
print("Unraid, production Compose, CI isolation and staging isolation contracts passed")
