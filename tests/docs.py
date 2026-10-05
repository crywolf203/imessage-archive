"""Keep the README configuration reference complete without importing the app."""

import ast
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlsplit


root = Path(__file__).resolve().parents[1]
readme = (root / "README.md").read_text(encoding="utf-8")
rows = {}
for match in re.finditer(r"^\| `([A-Z][A-Z0-9_]*)` \|(.+)\|$", readme, re.MULTILINE):
    name = match[1]
    assert name not in rows, f"Duplicate variable reference: {name}"
    cells = [cell.strip() for cell in match[2].split("|")]
    assert len(cells) == 3 and all(cells), f"Explain default, purpose, and usage: {name}"
    rows[name] = cells

runtime_defaults = {}
tree = ast.parse((root / "app/app.py").read_text(encoding="utf-8"))
for node in ast.walk(tree):
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        continue
    source = node.func.value
    if not (
        node.func.attr == "get"
        and isinstance(source, ast.Attribute)
        and source.attr == "environ"
        and isinstance(source.value, ast.Name)
        and source.value.id == "os"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        continue
    default = node.args[1] if len(node.args) > 1 else None
    runtime_defaults[node.args[0].value] = default.value if isinstance(default, ast.Constant) else None

supported = set(runtime_defaults)
for filename in ("docker-compose.yml", "docker-compose.image.yml", "entrypoint.sh"):
    source = (root / filename).read_text(encoding="utf-8")
    supported.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", source))
for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
    if line.strip() and not line.lstrip().startswith("#"):
        supported.add(line.partition("=")[0].strip())
container = ET.parse(root / "unraid/imessage-archive.xml").getroot()
supported.update(
    item.get("Target") for item in container.findall("Config") if item.get("Type") == "Variable"
)
supported.update(re.findall(
    r"^\s*(?:(?:ARG|ENV)\s+)?([A-Z][A-Z0-9_]*)=",
    (root / "Dockerfile").read_text(encoding="utf-8"), re.MULTILINE,
))
supported.add("BACKUP_PASSWORD")
assert supported <= rows.keys(), f"Undocumented variables: {sorted(supported - rows.keys())}"
assert rows.keys() <= supported, f"Remove stale variable documentation: {sorted(rows.keys() - supported)}"
for name, default in runtime_defaults.items():
    if default:
        assert f"`{default}`" in rows[name][0], f"Default changed without updating README: {name}"


def heading_anchors(document: str) -> set[str]:
    headings = re.findall(r"^#+ (.+)$", document, re.MULTILINE)
    return {re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-") for heading in headings}


for target in re.findall(r"!?\[[^\]]*\]\(([^\s)]+)\)", readme):
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        continue
    path = root / unquote(parsed.path) if parsed.path else root / "README.md"
    assert path.is_file(), f"Broken local README link: {target}"
    if parsed.fragment and path.suffix == ".md":
        assert unquote(parsed.fragment) in heading_anchors(path.read_text(encoding="utf-8")), target

assert readme.count("```") % 2 == 0, "Unclosed README code fence"
print(f"README documents all {len(supported)} supported variables; defaults and local links passed")
