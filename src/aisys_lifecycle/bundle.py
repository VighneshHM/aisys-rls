"""The release bundle: a folder or zip holding a signed manifest, the wheels, the SBOM and the lifecycle tools.

    aisys-<version>/
      manifest.json      version, kind, schema revision, python needed, SHA-256 of every other file
      manifest.sig       base64 Ed25519 signature over the exact bytes of manifest.json
      wheels/*.whl       the application and every dependency, so nothing is downloaded
      tools/aisys_lifecycle/   this package, so a first install needs nothing but Python
      SBOM.json, LICENSES.md, config.sample.toml, licence.sample.json

Verification is all-or-nothing and happens before anything is installed: signature, then every file's hash, then that no
unlisted file is present. Extraction refuses paths that escape the target folder.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from aisys_lifecycle import ed25519

MANIFEST = "manifest.json"
SIGNATURE = "manifest.sig"
KINDS = ("release", "patch", "security")
MAX_MANIFEST_BYTES = 1_000_000
_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")


class LifecycleError(Exception):
    """A refusal with a message an operator can act on."""


def parse_version(text: str) -> tuple[int, ...]:
    if not isinstance(text, str) or not _VERSION_RE.match(text):
        raise LifecycleError(f"'{text}' is not a version number like 1.2.3")
    return tuple(int(part) for part in text.split("."))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_public_key(text: str) -> bytes:
    """A public key file holds the 32 key bytes as base64 text."""
    try:
        key = base64.b64decode(text.strip(), validate=True)
    except ValueError as exc:
        raise LifecycleError("The trusted key file is not valid base64.") from exc
    if len(key) != 32:
        raise LifecycleError("The trusted key must be a 32-byte Ed25519 public key.")
    return key


def key_id(public_key: bytes) -> str:
    """Short fingerprint to compare by eye when a key is first trusted."""
    return hashlib.sha256(public_key).hexdigest()[:16]


def canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def check_signature(public_key: bytes, payload: bytes, signature_b64: str) -> bool:
    try:
        signature = base64.b64decode(signature_b64.strip(), validate=True)
    except ValueError:
        return False
    return ed25519.verify(public_key, payload, signature)


@dataclass
class Bundle:
    """A bundle whose signature and file hashes have been checked."""

    root: Path
    manifest: dict
    signer: str  # key id that signed it
    _tmp: Path | None = None

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def wheels(self) -> Path:
        return self.root / "wheels"

    def close(self) -> None:
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None


def _safe_extract(archive: Path, target: Path) -> None:
    base = target.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            dest = (target / info.filename).resolve()
            if base != dest and base not in dest.parents:
                raise LifecycleError(f"The package contains an unsafe path ('{info.filename}') and was not opened.")
        zf.extractall(target)  # noqa: S202 - every member path was checked above


def _find_root(folder: Path) -> Path:
    if (folder / MANIFEST).is_file():
        return folder
    inner = [p for p in folder.iterdir() if p.is_dir() and (p / MANIFEST).is_file()]
    if len(inner) == 1:
        return inner[0]
    raise LifecycleError("This is not an AISYS update package (no manifest.json found).")


def _validate_manifest(manifest: object) -> dict:
    if not isinstance(manifest, dict):
        raise LifecycleError("The package manifest is not readable.")
    problems = []
    for key, kind in (("name", str), ("version", str), ("kind", str), ("schema_revision", str), ("files", dict)):
        if not isinstance(manifest.get(key), kind):
            problems.append(key)
    if problems:
        raise LifecycleError("The package manifest is missing or has wrong values for: " + ", ".join(problems) + ".")
    if manifest["name"] != "aisys":
        raise LifecycleError(f"This package is for '{manifest['name']}', not for AISYS.")
    parse_version(manifest["version"])
    if manifest["kind"] not in KINDS:
        raise LifecycleError(f"Unknown package kind '{manifest['kind']}'.")
    for rel, digest in manifest["files"].items():
        if (
            not isinstance(rel, str)
            or not isinstance(digest, str)
            or rel.startswith(("/", "\\"))
            or ".." in Path(rel).parts
        ):
            raise LifecycleError("The package manifest lists an unsafe file name.")
    return manifest


def open_bundle(source: Path, trusted_keys: list[bytes]) -> Bundle:
    """Open a bundle folder or zip and prove it is intact and signed by a trusted key. Raises LifecycleError if not."""
    source = Path(source)
    tmp: Path | None = None
    try:
        if source.is_file():
            if source.suffix.lower() != ".zip":
                raise LifecycleError("An update package is a folder or a .zip file.")
            tmp = Path(tempfile.mkdtemp(prefix="aisys-bundle-"))
            try:
                _safe_extract(source, tmp)
            except zipfile.BadZipFile as exc:
                raise LifecycleError("The package is not a readable zip file (damaged or incomplete copy?).") from exc
            root = _find_root(tmp)
        elif source.is_dir():
            root = _find_root(source)
        else:
            raise LifecycleError(f"No such package: {source}")

        manifest_path, sig_path = root / MANIFEST, root / SIGNATURE
        if not sig_path.is_file():
            raise LifecycleError("The package is not signed (manifest.sig is missing), so it was not accepted.")
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise LifecycleError("The package manifest is unreasonably large.")
        payload = manifest_path.read_bytes()
        signer = next(
            (key_id(k) for k in trusted_keys if check_signature(k, payload, sig_path.read_text("ascii"))), None
        )
        if signer is None:
            raise LifecycleError(
                "The signature does not match a trusted key. The package was altered or comes from someone else."
            )
        try:
            manifest = _validate_manifest(json.loads(payload))
        except ValueError as exc:
            raise LifecycleError("The package manifest is not readable.") from exc

        listed = set(manifest["files"])
        for rel, digest in manifest["files"].items():
            path = root / rel
            if not path.is_file():
                raise LifecycleError(f"The package is incomplete: {rel} is missing.")
            if sha256_file(path) != digest:
                raise LifecycleError(f"The package is damaged or altered: {rel} does not match its checksum.")
        present = {
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and p.name not in (MANIFEST, SIGNATURE)
        }
        extra = sorted(present - listed)
        if extra:
            raise LifecycleError(
                f"The package contains files its manifest does not list ({extra[0]}), so it was refused."
            )
        return Bundle(root=root, manifest=manifest, signer=signer, _tmp=tmp)
    except Exception:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        raise
