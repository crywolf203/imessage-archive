"""Check update discovery and stable-promotion guards without network access."""

import copy
import importlib.util
import json
import re
from pathlib import Path


root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("candidate", root / ".github/scripts/candidate.py")
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)

config = json.loads((root / "renovate.json").read_text(encoding="utf-8"))
assert config["automerge"] is False
manager, = config["customManagers"]
assert manager["datasourceTemplate"] == "crate"
assert manager["depNameTemplate"] == "imessage-exporter"
pattern = manager["matchStrings"][0].replace("(?<currentValue>", "(?P<currentValue>")
matches = re.findall(pattern, (root / "Dockerfile").read_text(encoding="utf-8"))
assert matches == [candidate.exporter_version()]
assert "IMESSAGE_EXPORTER_VERSION" not in (root / "docker-compose.yml").read_text()

run = {
    "repository": {"full_name": "crywolf203/imessage-archive"},
    "head_repository": {"full_name": "crywolf203/imessage-archive"},
    "path": ".github/workflows/container.yml", "event": "push",
    "head_branch": "main", "head_sha": "a" * 40,
    "status": "completed", "conclusion": "success", "id": 123, "run_attempt": 2,
}
metadata = {
    "schema": 1, "checks": "passed", "repository": "crywolf203/imessage-archive",
    "source_sha": "a" * 40, "event": "push", "branch": "main",
    "run_id": 123, "run_attempt": 2,
    "image": "ghcr.io/crywolf203/imessage-archive:candidate-aaaaaaaaaaaa-123-2",
    "digest": "sha256:" + "b" * 64,
    "image_reference": "ghcr.io/crywolf203/imessage-archive@sha256:" + "b" * 64,
}
outputs = candidate.validate_candidate(run, metadata, "crywolf203/imessage-archive", 123)
assert outputs["PROMOTION_IMAGE"].endswith("@sha256:" + "b" * 64)


def rejected(changed_run, changed_metadata, requested_id=123):
    try:
        candidate.validate_candidate(changed_run, changed_metadata, "crywolf203/imessage-archive", requested_id)
    except ValueError:
        return
    raise AssertionError("Unsafe candidate was accepted")


for key, value in (
    ("event", "pull_request"), ("head_branch", "renovate/update"),
    ("conclusion", "failure"), ("status", "in_progress"), ("run_attempt", 3),
    ("head_sha", "c" * 40), ("path", ".github/workflows/other.yml"),
    ("head_repository", {"full_name": "other/fork"}),
    ("repository", {"full_name": "other/project"}),
):
    changed = copy.deepcopy(run)
    changed[key] = value
    rejected(changed, metadata)
for key, value in (
    ("digest", "sha256:invalid\nINJECTED=yes"), ("image", "other/image:latest"),
    ("checks", "failed"), ("run_id", 456), ("schema", 0), ("branch", "other"),
    ("image_reference", "other/image@sha256:" + "b" * 64),
):
    changed = copy.deepcopy(metadata)
    changed[key] = value
    rejected(run, changed)
rejected(run, metadata, 456)

build = (root / ".github/workflows/container.yml").read_text()
assert ":latest" not in build and "pull_request_target" not in build
assert build.index("verify-candidate") < build.index("docker/login-action") < build.index("docker push")
assert "head.repo.full_name == github.repository" in build
assert "no-cache:" in build and "schedule:" in build
promotion = (root / ".github/workflows/promote.yml").read_text()
assert "inputs.confirm == true" in promotion
assert "--prefer-index=false" in promotion and '"$PROMOTION_IMAGE"' in promotion
assert "merge-base --is-ancestor" in promotion
assert "workflow_run" not in (root / ".github/workflows/verify-image.yml").read_text()
print("Renovate extraction and 17 invalid promotion scenarios passed")
