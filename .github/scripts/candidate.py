"""Record tested-image provenance and reject unsafe stable-promotion inputs."""

import json
import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def exporter_version(root=ROOT):
    matches = re.findall(
        r"^ARG IMESSAGE_EXPORTER_VERSION=([0-9]+\.[0-9]+\.[0-9]+)$",
        (root / "Dockerfile").read_text(encoding="utf-8"), re.MULTILINE,
    )
    if len(matches) != 1:
        raise ValueError("Keep exactly one exporter version default in Dockerfile")
    return matches[0]


def validate_candidate(run, candidate, repository, requested_id):
    expected_base = f"ghcr.io/{repository.split('/')[0].lower()}/imessage-archive"
    if run.get("repository", {}).get("full_name") != repository:
        raise ValueError("Candidate belongs to a different repository")
    if run.get("head_repository", {}).get("full_name") != repository:
        raise ValueError("Fork candidates cannot be promoted")
    if run.get("path") != ".github/workflows/container.yml":
        raise ValueError("Candidate must come from the Build candidate workflow")
    if run.get("event") not in {"push", "schedule", "workflow_dispatch"}:
        raise ValueError("Pull-request candidates cannot be promoted; merge and test main first")
    if run.get("head_branch") != "main" or run.get("conclusion") != "success":
        raise ValueError("Only successful main-branch candidates can be promoted")
    if run.get("status") != "completed" or run.get("id") != requested_id:
        raise ValueError("Candidate run is incomplete or does not match the request")
    if candidate.get("schema") != 1 or candidate.get("checks") != "passed":
        raise ValueError("Missing successful candidate regression-test metadata")
    for key, expected in (
        ("repository", repository), ("run_id", run["id"]),
        ("run_attempt", run["run_attempt"]), ("source_sha", run["head_sha"]),
        ("event", run["event"]), ("branch", "main"),
    ):
        if candidate.get(key) != expected:
            raise ValueError(f"Candidate provenance mismatch: {key}")
    sha = candidate["source_sha"]
    digest = candidate.get("digest", "")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid source revision")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Invalid candidate image digest")
    expected_tag = f"{expected_base}:candidate-{sha[:12]}-{run['id']}-{run['run_attempt']}"
    if candidate.get("image") != expected_tag:
        raise ValueError("Unexpected candidate registry or tag")
    if candidate.get("image_reference") != f"{expected_base}@{digest}":
        raise ValueError("Immutable image reference does not match the recorded digest")
    return {
        "PROMOTION_IMAGE": f"{expected_base}@{digest}",
        "PROMOTION_SHA": sha,
        "PROMOTION_DIGEST": digest,
    }


def record_candidate(manifest):
    result = json.loads((ROOT / "runtime-check-results.json").read_text(encoding="utf-8"))
    if result.get("status") != "passed":
        raise ValueError("Runtime regression tests did not pass")
    digest = manifest.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Registry did not return an image digest")
    record = {
        "schema": 1,
        "repository": os.environ["GITHUB_REPOSITORY"],
        "source_sha": os.environ["SOURCE_SHA"],
        "event": os.environ["GITHUB_EVENT_NAME"],
        "branch": os.environ["GITHUB_REF_NAME"],
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "image": os.environ["CANDIDATE_IMAGE"],
        "image_reference": os.environ["CANDIDATE_IMAGE"].split(":", 1)[0] + "@" + digest,
        "digest": digest,
        "exporter_version": exporter_version(),
        "checks": "passed",
        "runtime": result,
    }
    (ROOT / "candidate.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def main():
    if sys.argv[1] == "version":
        print(exporter_version())
    elif sys.argv[1] == "record":
        record_candidate(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))
    elif sys.argv[1] == "validate":
        outputs = validate_candidate(
            json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")),
            json.loads(Path(sys.argv[3]).read_text(encoding="utf-8")),
            os.environ["GITHUB_REPOSITORY"], int(os.environ["CANDIDATE_RUN_ID"]),
        )
        with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
            for name, value in outputs.items():
                output.write(f"{name}={value}\n")
        print(json.dumps(outputs, indent=2))
    else:
        raise ValueError("Unknown candidate command")


if __name__ == "__main__":
    main()
