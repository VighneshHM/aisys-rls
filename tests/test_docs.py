"""D1/D8: the generated documents match the code, and every requirement points at evidence."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_the_data_dictionary_and_api_reference_are_current():
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "gen_docs.py"), "--check"], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_traceability_matrix_is_current():
    before = (ROOT / "docs" / "traceability.md").read_text(encoding="utf-8")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "trace.py")], check=True, capture_output=True)  # noqa: S603
    after = (ROOT / "docs" / "traceability.md").read_text(encoding="utf-8")
    assert before == after, "docs/traceability.md was out of date: run python scripts/trace.py and commit it"


def test_every_requirement_marked_done_has_test_evidence():
    import csv

    rows = list(csv.DictReader((ROOT / "docs" / "requirements.csv").open(encoding="utf-8", newline="")))
    text = (ROOT / "docs" / "traceability.md").read_text(encoding="utf-8")
    missing = [
        r["id"]
        for r in rows
        if r["status"] == "done" and f"| {r['id']} |" in text and f"| {r['id']} | {r['area']} | done | - |" in text
    ]
    assert not missing, f"marked done with no test evidence: {missing}"


DOC_AREAS = {
    "requirements": [
        "assumptions.md",
        "use-cases.md",
        "data-dictionary.md",
        "requirements.csv",
        "traceability.md",
    ],
    "architecture": ["architecture.md"],
    "engineering": ["engineering.md", "third-party-licences.md", "project-management.md", "decisions.md"],
    "testing": [
        "test-plan.md",
        "uat.md",
        "uat-signoff.md",
        "uat-signoff-development.md",
        "performance-results.md",
        "security-checks.md",
        "defect-log.md",
    ],
    "deployment": ["deployment.md", "backup-restore.md"],
    "operations": [
        "administration.md",
        "user-guide.md",
        "api-reference.md",
        "troubleshooting.md",
        "training-plan.md",
    ],
    "demo and roadmap": ["demo-script.md", "remaining-work.md"],
}


@pytest.mark.req("NFR08", "NFR09")
def test_every_documentation_area_has_its_documents():
    missing = []
    for area, files in DOC_AREAS.items():
        for name in files:
            path = ROOT / "docs" / name
            if not path.is_file() or len(path.read_text(encoding="utf-8").split()) < 150:
                missing.append(f"{area}: docs/{name}")
    assert not missing, "missing or empty: " + ", ".join(missing)
    index = (ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    for files in DOC_AREAS.values():
        for name in files:
            assert name in index, f"docs/index.md does not list {name}"


@pytest.mark.req("NFR04")
def test_the_sbom_and_the_licence_inventory_list_every_pinned_package():
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "sbom_lock.py"), "--check"], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.req("NFR04")
def test_the_build_backend_is_pinned():
    dev = (ROOT / "requirements-dev.lock").read_text(encoding="utf-8")
    assert "\nsetuptools==" in dev, "pyproject.toml [build-system] needs setuptools: pin it in requirements-dev.lock"


@pytest.mark.req("NFR04")
@pytest.mark.parametrize("lock", ["requirements.lock", "requirements-dev.lock"])
def test_every_package_in_the_lock_files_carries_hashes(lock):
    text = (ROOT / lock).read_text(encoding="utf-8")
    blocks = [b for b in text.split("\n") if b and not b.startswith((" ", "#"))]
    assert blocks and all(" \\" in b for b in blocks), (
        "a pin has no --hash lines: regenerate with pip-compile --generate-hashes"
    )
    assert text.count("--hash=sha256:") >= len(blocks)


def test_the_sample_reports_are_current():
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "sample_reports.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.req("NFR09")
def test_the_stage_timeline_covers_d0_to_d10():
    text = (ROOT / "docs" / "project-management.md").read_text(encoding="utf-8")
    days = re.findall(r"^\| (D\d+(?: to D\d+)?) \|", text, flags=re.M)
    assert days[0] == "D0" and days[-1] == "D10", days


@pytest.mark.req("NFR04")
def test_redistribution_and_offline_use_notes_exist():
    text = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    assert "## Redistribution and offline use" in text
    section = text.split("## Redistribution and offline use", 1)[1].split("\n## ", 1)[0]
    for must in ("redistributed", "Offline use", "Apache-2.0", "not redistributed"):
        assert must in section, must
