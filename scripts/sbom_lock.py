"""Write docs/sbom.cdx.json: the software bill of materials for everything pinned in requirements.lock.

    python scripts/sbom_lock.py            # rewrite the file
    python scripts/sbom_lock.py --check    # exit 1 if a pinned package is missing from it or has another version

The release script already writes an SBOM of the wheels that ship to a site (runtime packages only). This one covers
the build and test tools too (pytest, ruff, mypy, cryptography for the signing tests), with the sha256 hashes from the
lock file, so a reviewer can see every package the project depends on without building anything.
`scope` is "required" for packages the application needs to run and "optional" for the development tools.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOCK = REPO / "requirements.lock"
OUT = REPO / "docs" / "sbom.cdx.json"
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)")
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock() -> dict[str, dict]:
    """name -> {name, version, hashes} from the lock file (one pin, then its hash lines)."""
    pins: dict[str, dict] = {}
    current = None
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        m = PIN.match(line)
        if m:
            current = pins.setdefault(norm(m.group(1)), {"name": m.group(1), "version": m.group(2), "hashes": []})
        if current is not None:
            current["hashes"] += HASH.findall(line)
    return pins


def runtime_names() -> set[str]:
    """The packages the application needs to run: its own requirements and theirs, found from the pyproject file."""
    import tomllib

    declared = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    seen: set[str] = set()
    todo = [norm(re.split(r"[<>=!~;\[ ]", d, maxsplit=1)[0]) for d in declared]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            requires = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            continue
        for r in requires:
            if "extra ==" in r:
                continue
            todo.append(norm(re.split(r"[<>=!~;\[ (]", r, maxsplit=1)[0]))
    return seen


def build() -> dict:
    pins, runtime = read_lock(), runtime_names()
    version = tomllib_version()
    components = []
    for key in sorted(pins):
        p = pins[key]
        components.append(
            {
                "type": "library",
                "name": p["name"],
                "version": p["version"],
                "scope": "required" if key in runtime else "optional",
                "purl": f"pkg:pypi/{key}@{p['version']}",
                "hashes": [{"alg": "SHA-256", "content": h} for h in sorted(set(p["hashes"]))],
            }
        )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "aisys", "version": version}},
        "components": components,
    }


def tomllib_version() -> str:
    import tomllib

    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    wanted = build()
    if args.check:
        have = (
            {c["name"].lower(): c["version"] for c in json.loads(OUT.read_text(encoding="utf-8"))["components"]}
            if OUT.exists()
            else {}
        )
        stale = [c["name"] for c in wanted["components"] if have.get(c["name"].lower()) != c["version"]]
        if stale:
            print("sbom.cdx.json is out of date for:", ", ".join(stale), "- run python scripts/sbom_lock.py")
            return 1
        return 0
    OUT.write_text(json.dumps(wanted, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(REPO)} ({len(wanted['components'])} packages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
