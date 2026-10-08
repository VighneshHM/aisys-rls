"""Software bill of materials and licence inventory, read from the wheels that are actually shipped."""

from __future__ import annotations

import email.parser
import re
import zipfile
from pathlib import Path

# Licences that need a legal look before redistribution (copyleft or unusual terms).
REVIEW = re.compile(r"\b(GPL|AGPL|LGPL|MPL|EPL|CDDL|SSPL|CC-BY-SA|CC-BY-NC)\b", re.IGNORECASE)


def _licence_of(msg) -> str:
    expr = (msg.get("License-Expression") or "").strip()
    if expr:
        return expr
    classifiers = [c.split("::")[-1].strip() for c in msg.get_all("Classifier") or [] if c.startswith("License ::")]
    classifiers = [c for c in classifiers if c and c != "OSI Approved"]
    if classifiers:
        return " / ".join(classifiers)
    text = (msg.get("License") or "").strip().splitlines()
    return text[0][:80] if text else "UNKNOWN"


def read_wheel(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        meta = next((n for n in zf.namelist() if n.endswith(".dist-info/METADATA")), None)
        if meta is None:
            raise ValueError(f"{path.name} has no METADATA")
        msg = email.parser.Parser().parsestr(zf.read(meta).decode("utf-8", "replace"), headersonly=True)
        has_licence_file = any(
            "/licenses/" in n.lower() or Path(n).name.upper().startswith(("LICENSE", "COPYING")) for n in zf.namelist()
        )
    return {
        "name": msg.get("Name"),
        "version": msg.get("Version"),
        "licence": _licence_of(msg),
        "home": (msg.get("Home-page") or "").strip(),
        "licence_file_shipped": has_licence_file,
        "wheel": path.name,
    }


def inventory(wheels: Path) -> list[dict]:
    items = [read_wheel(p) for p in sorted(Path(wheels).glob("*.whl"))]
    for item in items:
        item["needs_review"] = bool(REVIEW.search(item["licence"])) or item["licence"] == "UNKNOWN"
    return sorted(items, key=lambda i: i["name"].lower())


def cyclonedx(items: list[dict], app_name: str, app_version: str) -> dict:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": app_name, "version": app_version}},
        "components": [
            {
                "type": "library",
                "name": i["name"],
                "version": i["version"],
                "purl": f"pkg:pypi/{i['name'].lower().replace('_', '-')}@{i['version']}",
                "licenses": [{"license": {"name": i["licence"]}}],
            }
            for i in items
        ],
    }


def licences_markdown(items: list[dict], app_name: str, app_version: str) -> str:
    lines = [
        f"# Third-party licences for {app_name} {app_version}",
        "",
        "Generated from the wheels shipped in this package. Each wheel carries its own licence files in its "
        "`.dist-info` folder.",
        "",
        "| Package | Version | Licence | Review |",
        "|---|---|---|---|",
    ]
    for i in items:
        lines.append(
            f"| {i['name']} | {i['version']} | {i['licence']} | {'**needs review**' if i['needs_review'] else 'ok'} |"
        )
    flagged = [i["name"] for i in items if i["needs_review"]]
    lines += [
        "",
        f"{len(items)} packages. "
        + (
            f"Needs legal review before redistribution: {', '.join(flagged)}."
            if flagged
            else "None use a copyleft or unknown licence."
        ),
    ]
    return "\n".join(lines) + "\n"
