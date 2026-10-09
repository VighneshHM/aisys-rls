"""D1/D8: the generated documents match the code, and every requirement points at evidence."""

from __future__ import annotations

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


DELIVERABLES = {
    "D1 requirements package": [
        "assumptions.md",
        "use-cases.md",
        "data-dictionary.md",
        "requirements.csv",
        "traceability.md",
    ],
    "D2 architecture package": ["architecture.md"],
    "D4 repository": ["engineering.md", "third-party-licences.md"],
    "D6 test package": ["test-plan.md", "uat.md", "performance-results.md", "security-checks.md", "defect-log.md"],
    "D7 deployment package": ["deployment.md", "backup-restore.md"],
    "D8 operational documentation": [
        "administration.md",
        "user-guide.md",
        "api-reference.md",
        "troubleshooting.md",
        "training-plan.md",
    ],
    "D9 demonstration": ["demo-script.md", "remaining-work.md"],
}


@pytest.mark.req("NFR08", "NFR09")
def test_every_mandatory_deliverable_has_its_document():
    missing = []
    for deliverable, files in DELIVERABLES.items():
        for name in files:
            path = ROOT / "docs" / name
            if not path.is_file() or len(path.read_text(encoding="utf-8").split()) < 150:
                missing.append(f"{deliverable}: docs/{name}")
    assert not missing, "missing or empty: " + ", ".join(missing)
    index = (ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    for files in DELIVERABLES.values():
        for name in files:
            assert name in index, f"docs/index.md does not list {name}"
