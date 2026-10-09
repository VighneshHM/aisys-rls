"""Build machine tool (needs internet once, and the dev extras): make signed update packages and licences.

    python scripts/release.py keygen  --out release-keys
    python scripts/release.py build   --key release-keys/private.key [--version 0.2.0] [--kind security] [--notes "..."]
    python scripts/release.py licence --key release-keys/private.key --customer "Demo Library" --expires 2027-12-31

`build` produces dist/aisys-<version>/ and dist/aisys-<version>.zip. Everything the target needs is inside: the
application and ALL dependency wheels, the lifecycle tools, SBOM.json, LICENSES.md, the sample config, the public key.
The private key never goes into a package; keep release-keys/ off the target machines (it is git-ignored).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess  # noqa: S404
import sys
import tempfile
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from aisys_lifecycle import bundle, sbom  # noqa: E402

INSTALL_PY = '''"""Install AISYS: python install.py --root <dir> --licence <file> --trusted-key <file>"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))

from aisys_lifecycle.__main__ import main  # noqa: E402

argv = sys.argv[1:]
if "--root" not in argv:
    sys.exit("usage: python install.py --root <dir> --licence <file> (--trusted-key <file> | --trust-bundle-key)")
root = argv[argv.index("--root") + 1]
rest = [a for i, a in enumerate(argv) if a != "--root" and (i == 0 or argv[i - 1] != "--root")]
sys.exit(main(["--root", root, "install", "--bundle", HERE, *rest]))
'''


def _private(path: Path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(path.read_text().strip()))


def _public_b64(priv) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def keygen(out: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    out.mkdir(parents=True, exist_ok=True)
    if (out / "private.key").exists():
        sys.exit(f"{out / 'private.key'} already exists; refusing to overwrite a signing key")
    priv = Ed25519PrivateKey.generate()
    raw = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    (out / "private.key").write_text(base64.b64encode(raw).decode())
    (out / "public.key").write_text(_public_b64(priv))
    print(f"keys written to {out}  (public key fingerprint {bundle.key_id(base64.b64decode(_public_b64(priv)))})")


def make_licence(key: Path, customer: str, expires: str | None, out: Path) -> None:
    priv = _private(key)
    licence = {"product": "aisys", "customer": customer, "issued": date.today().isoformat(), "expires": expires}
    if expires:
        date.fromisoformat(expires)
    sig = base64.b64encode(priv.sign(bundle.canonical(licence))).decode()
    out.write_text(json.dumps({"licence": licence, "signature": sig}, indent=2))
    print(f"licence for {customer!r} written to {out}")


def _prepare_source(source: Path, version: str | None, tmp: Path) -> Path:
    """A clean copy of the source tree (optionally with another version number) so the repo is never modified."""
    work = tmp / "src-tree"
    shutil.copytree(
        source,
        work,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "data",
            "dist",
            "build",
            "*.egg-info",
            "__pycache__",
            "release-keys",
            ".pytest_cache",
            ".ruff_cache",
            "*.db",
        ),
    )
    if version:
        py = work / "pyproject.toml"
        py.write_text(re.sub(r'(?m)^version = ".*"', f'version = "{version}"', py.read_text("utf-8"), count=1), "utf-8")
        init = work / "src" / "aisys" / "__init__.py"
        init.write_text(re.sub(r'__version__ = ".*"', f'__version__ = "{version}"', init.read_text("utf-8")), "utf-8")
    return work


def build(
    key: Path,
    source: Path,
    version: str | None,
    kind: str,
    notes: str,
    out: Path,
    min_from: str | None,
    wheels_from: Path | None,
) -> Path:
    priv = _private(key)
    with tempfile.TemporaryDirectory(prefix="aisys-build-") as t:
        tmp = Path(t)
        work = _prepare_source(source, version, tmp)
        wheels = tmp / "wheels"
        wheels.mkdir()
        # pip does not accept hashes in a constraints file, so pass the same pins without them
        pins = [ln.rstrip(" \\") for ln in (work / "requirements.lock").read_text(encoding="utf-8").splitlines()]
        constraint = tmp / "constraints.txt"
        pinned = [ln for ln in pins if ln and not ln.startswith((" ", "#"))]
        constraint.write_text("\n".join(pinned) + "\n", encoding="utf-8")
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(work),
            "-w",
            str(wheels),
            "-c",
            str(constraint),
            "--disable-pip-version-check",
        ]
        if wheels_from:
            cmd += ["--no-index", "--no-build-isolation", "--find-links", str(wheels_from)]
        print("building wheels (needs the internet unless --wheels-from is given)...")
        subprocess.run(cmd, check=True)  # noqa: S603
        app = next(p for p in wheels.glob("aisys-*.whl"))
        ver = app.name.split("-")[1]
        items = sbom.inventory(wheels)
        pkg = out / f"aisys-{ver}"
        shutil.rmtree(pkg, ignore_errors=True)
        (pkg / "wheels").mkdir(parents=True)
        for w in wheels.glob("*.whl"):
            shutil.copy2(w, pkg / "wheels" / w.name)
        shutil.copytree(
            work / "src" / "aisys_lifecycle",
            pkg / "tools" / "aisys_lifecycle",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (pkg / "SBOM.json").write_text(json.dumps(sbom.cyclonedx(items, "aisys", ver), indent=2))
        (pkg / "LICENSES.md").write_text(sbom.licences_markdown(items, "aisys", ver), encoding="utf-8")
        shutil.copyfile(work / "config" / "config.sample.toml", pkg / "config.sample.toml")
        (pkg / "vendor.pub").write_text(_public_b64(priv))
        (pkg / "install.py").write_text(INSTALL_PY, encoding="utf-8")

        sys.path.insert(0, str(work / "src"))
        schema = _head_revision(work)
        files = {p.relative_to(pkg).as_posix(): bundle.sha256_file(p) for p in sorted(pkg.rglob("*")) if p.is_file()}
        manifest = {
            "name": "aisys", "version": ver, "kind": kind, "notes": notes, "schema_revision": schema,
            "python_min": "3.12", "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "files": files,
        }  # fmt: skip
        if min_from:
            manifest["min_from_version"] = min_from
        payload = json.dumps(manifest, indent=2, sort_keys=True).encode()
        (pkg / "manifest.json").write_bytes(payload)
        (pkg / "manifest.sig").write_text(base64.b64encode(priv.sign(payload)).decode())

        archive = out / f"aisys-{ver}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(pkg.rglob("*")):
                if p.is_file():
                    zf.write(p, f"aisys-{ver}/{p.relative_to(pkg).as_posix()}")
        flagged = [i["name"] for i in items if i["needs_review"]]
        print(f"built {archive}  ({archive.stat().st_size / 1e6:.1f} MB, {len(items)} packages, schema {schema})")
        if flagged:
            print("licence review needed for:", ", ".join(flagged))
        return pkg


def _head_revision(work: Path) -> str:
    versions = work / "src" / "aisys" / "migrations" / "versions"
    revs, downs = {}, set()
    for f in versions.glob("*.py"):
        text = f.read_text("utf-8")
        rev = re.search(r'(?m)^revision(?:: str)? = "([^"]+)"', text)
        down = re.search(r'(?m)^down_revision(?:: [^=]+)? = "?([^"\n]+)"?', text)
        if rev:
            revs[rev.group(1)] = f
            if down and down.group(1) != "None":
                downs.add(down.group(1))
    heads = sorted(set(revs) - downs)
    if len(heads) != 1:
        sys.exit(f"expected exactly one migration head, found {heads}")
    return heads[0]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--out", default="release-keys", type=Path)
    b = sub.add_parser("build")
    b.add_argument("--key", required=True, type=Path)
    b.add_argument("--source", default=REPO, type=Path)
    b.add_argument("--version", help="override the version number (built from a temporary copy)")
    b.add_argument("--kind", default="release", choices=bundle.KINDS)
    b.add_argument("--notes", default="")
    b.add_argument("--out", default=REPO / "dist", type=Path)
    b.add_argument("--min-from", help="oldest installed version that may apply this package")
    b.add_argument("--wheels-from", type=Path, help="build offline from a folder of wheels")
    lc = sub.add_parser("licence")
    lc.add_argument("--key", required=True, type=Path)
    lc.add_argument("--customer", required=True)
    lc.add_argument("--expires")
    lc.add_argument("--out", default=Path("licence.json"), type=Path)
    a = p.parse_args()
    if a.cmd == "keygen":
        keygen(a.out)
    elif a.cmd == "licence":
        make_licence(a.key, a.customer, a.expires, a.out)
    else:
        a.out.mkdir(parents=True, exist_ok=True)
        build(a.key, a.source, a.version, a.kind, a.notes, a.out, a.min_from, a.wheels_from)


if __name__ == "__main__":
    main()
