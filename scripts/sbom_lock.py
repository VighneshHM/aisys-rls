"""Write the software bill of materials and the licence inventory for everything pinned in the lock files.

    python scripts/sbom_lock.py            # rewrite docs/sbom.cdx.json and docs/third-party-licences.md
    python scripts/sbom_lock.py --check    # exit 1 if either file is out of date with the lock files

Inputs: `requirements.lock` (what the application needs to run, and what ships to a site) and
`requirements-dev.lock` (the test, lint, type-check and signing tools, and the build backend). Licences are read from
the installed packages' metadata, so run it in the project's environment after installing both lock files
(`pip install --require-hashes -r requirements.lock -r requirements-dev.lock`).
The release script separately writes an SBOM and LICENSES.md of the wheels each package actually ships.

Scope, in both outputs: **runtime** packages are shipped inside every offline package and redistributed to the site;
**development** and **build** packages are used on the developer's and the build machine only and are never shipped.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from aisys_lifecycle.sbom import REVIEW, licence_of  # noqa: E402 - needs the path above

LOCKS = {"requirements.lock": "runtime", "requirements-dev.lock": "development"}
BUILD = {"setuptools"}  # the build backend named in pyproject.toml [build-system]
SBOM = REPO / "docs" / "sbom.cdx.json"
INVENTORY = REPO / "docs" / "third-party-licences.md"
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)")
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock(path: Path, scope: str) -> dict[str, dict]:
    """name -> {name, version, hashes, scope} from one lock file (a pin, then its hash lines)."""
    pins: dict[str, dict] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = PIN.match(line)
        if m:
            key = norm(m.group(1))
            current = pins.setdefault(
                key,
                {"name": m.group(1), "version": m.group(2), "hashes": [], "scope": "build" if key in BUILD else scope},
            )
        if current is not None:
            current["hashes"] += HASH.findall(line)
    return pins


def read_locks() -> dict[str, dict]:
    pins: dict[str, dict] = {}
    for file, scope in LOCKS.items():
        for key, pin in read_lock(REPO / file, scope).items():
            if key in pins:
                raise SystemExit(f"{pin['name']} is pinned in more than one lock file")
            pins[key] = pin
    return pins


def runtime_names() -> set[str]:
    """The packages the application needs to run: its own requirements and theirs, found from the pyproject file."""
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


def licence(name: str, version: str) -> str:
    try:
        meta = metadata.metadata(name)
    except metadata.PackageNotFoundError:
        raise SystemExit(f"{name} is not installed: install both lock files first") from None
    if meta["Version"] != version:
        raise SystemExit(f"{name} {meta['Version']} is installed but {version} is pinned: reinstall the lock files")
    return licence_of(meta)


def app_version() -> str:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def collect() -> list[dict]:
    pins = read_locks()
    for key, pin in pins.items():
        pin["licence"] = licence(pin["name"], pin["version"])
        pin["copyleft"] = bool(REVIEW.search(pin["licence"])) or pin["licence"] == "UNKNOWN"
        pin["needs_review"] = pin["copyleft"] and pin["scope"] == "runtime"  # only shipped packages are redistributed
        pin["key"] = key
    return [pins[k] for k in sorted(pins)]


def sbom(items: list[dict]) -> dict:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "aisys", "version": app_version()}},
        "components": [
            {
                "type": "library",
                "name": p["name"],
                "version": p["version"],
                "scope": "required" if p["scope"] == "runtime" else "optional",
                "purl": f"pkg:pypi/{p['key']}@{p['version']}",
                "licenses": [{"license": {"name": p["licence"]}}],
                "properties": [{"name": "aisys:scope", "value": p["scope"]}],
                "hashes": [{"alg": "SHA-256", "content": h} for h in sorted(set(p["hashes"]))],
            }
            for p in items
        ],
    }


def inventory(items: list[dict]) -> str:
    counts = {s: sum(1 for p in items if p["scope"] == s) for s in ("runtime", "development", "build")}
    flagged = [p["name"] for p in items if p["needs_review"]]
    tools = [p["name"] for p in items if p["copyleft"] and not p["needs_review"]]
    lines = [
        f"# Third-party licences (NFR04, D4): aisys {app_version()}",
        "",
        "_Generated by `python scripts/sbom_lock.py` from `requirements.lock` and `requirements-dev.lock`;"
        " `tests/test_docs.py` fails if it is out of date. The same list, with hashes, is `docs/sbom.cdx.json`"
        " (CycloneDX)._",
        "",
        f"{len(items)} packages: {counts['runtime']} runtime, {counts['development']} development,"
        f" {counts['build']} build. "
        + (
            f"**Needs legal review before redistribution: {', '.join(flagged)}.**"
            if flagged
            else "No shipped (runtime) package uses a copyleft or unknown licence."
        )
        + (
            f" Not shipped, so no redistribution duty: {', '.join(tools)}"
            " (weak copyleft, used only on the developer's machine)."
            if tools
            else ""
        ),
        "",
        "* **runtime**: needed to run the application. These wheels are **redistributed**: every offline package"
        " carries them, with their own licence files in each wheel's `.dist-info` folder, plus a generated"
        " `LICENSES.md` and `SBOM.json`.",
        "* **development**: tests, linting, type checking, dependency auditing and package signing. Used on the"
        " developer's and the build machine only; **never shipped** to a site.",
        "* **build**: the build backend that turns the source into a wheel on the build machine; **never shipped**.",
        "",
        "Redistribution and offline-use obligations: `docs/deployment.md`, section *Redistribution and offline use*.",
        "",
        "| Package | Version | Scope | Licence | Review |",
        "|---|---|---|---|---|",
    ]
    order = {"runtime": 0, "development": 1, "build": 2}
    for p in sorted(items, key=lambda p: (order[p["scope"]], p["key"])):
        lines.append(
            f"| {p['name']} | {p['version']} | {p['scope']} | {p['licence']} | "
            + ("**needs review**" if p["needs_review"] else "ok, not shipped" if p["copyleft"] else "ok")
            + " |"
        )
    return "\n".join(lines) + "\n"


def check_scopes(items: list[dict]) -> list[str]:
    """Every package the application needs at run time must be in requirements.lock, and only those."""
    runtime = runtime_names()
    wrong = [p["name"] for p in items if (p["key"] in runtime) != (p["scope"] == "runtime")]
    return [f"{n} is in the wrong lock file (runtime packages go in requirements.lock, others in -dev)" for n in wrong]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    items = collect()
    problems = check_scopes(items)
    sbom_text = json.dumps(sbom(items), indent=2) + "\n"
    inv_text = inventory(items)
    if args.check:
        for path, text in ((SBOM, sbom_text), (INVENTORY, inv_text)):
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                problems.append(f"{path.relative_to(REPO)} is out of date: run python scripts/sbom_lock.py")
        print("\n".join(problems))
        return 1 if problems else 0
    if problems:
        print("\n".join(problems))
        return 1
    SBOM.write_text(sbom_text, encoding="utf-8", newline="\n")
    INVENTORY.write_text(inv_text, encoding="utf-8", newline="\n")
    print(f"wrote {SBOM.relative_to(REPO)} and {INVENTORY.relative_to(REPO)} ({len(items)} packages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
