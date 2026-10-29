"""AC09 / FR12 / NFR04: offline install, signed update packages, update, rollback, recovery, SBOM."""

from __future__ import annotations

import base64
import json
import shutil
import sqlite3
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from shelfsense_lifecycle import bundle, ed25519, licence, ops, sbom
from shelfsense_lifecycle.bundle import LifecycleError

pytestmark = pytest.mark.req("AC09", "FR12")

SCHEMA_OLD, SCHEMA_NEW = "0008", "0009"


# ---------- helpers: keys, signed fake packages, a fake installation ----------
def pub_bytes(priv) -> bytes:
    return priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@pytest.fixture(scope="module")
def vendor():
    return Ed25519PrivateKey.generate()


def make_package(
    where: Path, priv, version="0.2.0", schema=SCHEMA_NEW, kind="release", extra: dict | None = None, **manifest_extra
) -> Path:
    """A signed package folder. The wheels are placeholders: tests that install patch out the pip step."""
    pkg = where / f"shelfsense-{version}"
    shutil.rmtree(pkg, ignore_errors=True)
    (pkg / "wheels").mkdir(parents=True)
    (pkg / "wheels" / f"shelfsense-{version}-py3-none-any.whl").write_bytes(b"not a real wheel " + version.encode())
    (pkg / "config.sample.toml").write_text("[app]\nname = 'x'\n")
    (pkg / "vendor.pub").write_text(base64.b64encode(pub_bytes(priv)).decode())
    for rel, content in (extra or {}).items():
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        (pkg / rel).write_bytes(content)
    files = {p.relative_to(pkg).as_posix(): bundle.sha256_file(p) for p in sorted(pkg.rglob("*")) if p.is_file()}
    manifest = {
        "name": "shelfsense", "version": version, "kind": kind, "notes": "test", "schema_revision": schema,
        "python_min": "3.12", "files": files, **manifest_extra,
    }  # fmt: skip
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode()
    (pkg / "manifest.json").write_bytes(payload)
    (pkg / "manifest.sig").write_text(base64.b64encode(priv.sign(payload)).decode())
    return pkg


def make_licence(path: Path, priv, expires: str | None = "2999-12-31", product="shelfsense") -> Path:
    lic = {"product": product, "customer": "Test Library", "issued": "2026-01-01", "expires": expires}
    sig = base64.b64encode(priv.sign(bundle.canonical(lic))).decode()
    path.write_text(json.dumps({"licence": lic, "signature": sig}))
    return path


@pytest.fixture
def fake_release(monkeypatch):
    """Skip the venv and pip step (slow, needs wheels): every 'release' runs the test environment's own shelfsense."""

    def create(root, b, log):
        d = root.release_dir(b.version)
        d.mkdir(parents=True, exist_ok=True)
        (d / "release.json").write_text(json.dumps(b.manifest))

    monkeypatch.setattr(ops, "_create_release", create)
    monkeypatch.setattr(ops, "_release_python", lambda root, version: Path(sys.executable))


@pytest.fixture
def no_smoke(monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "smoke", lambda root, version, url, log=print: calls.append((version, url)))
    return calls


@pytest.fixture
def installed(tmp_path, vendor, fake_release, no_smoke):
    """A root with 0.1.0 installed (schema 0008) holding some data."""
    root = ops.Root(tmp_path / "root")
    pkg = make_package(tmp_path / "in", vendor, "0.1.0", SCHEMA_OLD)
    lic = make_licence(tmp_path / "licence.json", vendor)
    ops.install(pkg, root, lic, trusted_key_file=_keyfile(tmp_path, vendor), log=lambda m: None)
    return root


def _keyfile(tmp_path: Path, priv) -> Path:
    p = tmp_path / "trusted.pub"
    p.write_text(base64.b64encode(pub_bytes(priv)).decode())
    return p


def users(root) -> int:
    con = sqlite3.connect(root.db_file)
    try:
        return con.execute("select count(*) from users").fetchone()[0]
    finally:
        con.close()


def seed(root) -> None:
    proc = ops.run_release(root, "0.1.0", ["seed-demo"])
    assert proc.returncode == 0, proc.stderr


# ---------- the signature code ----------
def test_ed25519_matches_the_rfc_8032_vector():
    pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    sig = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
        "d25bf5f0595bbe24655141438e7a100b"
    )
    assert ed25519.verify(pub, b"", sig)
    assert not ed25519.verify(pub, b"x", sig)


def test_verifier_agrees_with_the_real_library_and_never_raises(vendor):
    msg = b"update package manifest"
    sig = vendor.sign(msg)
    assert ed25519.verify(pub_bytes(vendor), msg, sig)
    assert not ed25519.verify(pub_bytes(vendor), msg + b".", sig)
    assert not ed25519.verify(pub_bytes(Ed25519PrivateKey.generate()), msg, sig)
    for junk in (b"", b"\x00" * 64, b"\xff" * 64, sig[:63], sig + b"x"):
        assert ed25519.verify(pub_bytes(vendor), msg, junk) is False
    assert ed25519.verify(b"short", msg, sig) is False
    assert ed25519.verify(b"\xff" * 32, msg, sig) is False


# ---------- the package ----------
def test_a_good_package_opens(tmp_path, vendor):
    pkg = make_package(tmp_path, vendor)
    b = bundle.open_bundle(pkg, [pub_bytes(vendor)])
    assert b.version == "0.2.0" and b.signer == bundle.key_id(pub_bytes(vendor))


def test_a_zip_package_opens_and_is_cleaned_up(tmp_path, vendor):
    pkg = make_package(tmp_path, vendor)
    archive = tmp_path / "p.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for f in pkg.rglob("*"):
            if f.is_file():
                zf.write(f, f"shelfsense-0.2.0/{f.relative_to(pkg).as_posix()}")
    b = bundle.open_bundle(archive, [pub_bytes(vendor)])
    tmp = b._tmp
    assert tmp and tmp.exists()
    b.close()
    assert not tmp.exists()


@pytest.mark.parametrize(
    "tamper, message",
    [
        (lambda p: (p / "config.sample.toml").write_text("changed"), "checksum"),
        (lambda p: (p / "wheels" / "shelfsense-0.2.0-py3-none-any.whl").write_bytes(b"swapped"), "checksum"),
        (lambda p: (p / "manifest.sig").unlink(), "not signed"),
        (lambda p: (p / "manifest.sig").write_text("!!notbase64!!"), "signature"),
        (lambda p: (p / "evil.py").write_text("print(1)"), "does not list"),
        (lambda p: (p / "config.sample.toml").unlink(), "incomplete"),
        (lambda p: (p / "manifest.json").write_text("{}"), "signature"),
    ],
)
def test_a_tampered_package_is_refused(tmp_path, vendor, tamper, message):
    pkg = make_package(tmp_path, vendor)
    tamper(pkg)
    with pytest.raises(LifecycleError, match=message):
        bundle.open_bundle(pkg, [pub_bytes(vendor)])


def test_a_package_signed_by_someone_else_is_refused(tmp_path, vendor):
    pkg = make_package(tmp_path, Ed25519PrivateKey.generate())
    with pytest.raises(LifecycleError, match="trusted key"):
        bundle.open_bundle(pkg, [pub_bytes(vendor)])


def test_a_manifest_with_wrong_shapes_is_refused_not_crashed(tmp_path, vendor):
    for bad in ({"name": "shelfsense", "version": 3}, {"name": "other"}, []):
        pkg = tmp_path / "bad"
        shutil.rmtree(pkg, ignore_errors=True)
        pkg.mkdir()
        payload = json.dumps(bad).encode()
        (pkg / "manifest.json").write_bytes(payload)
        (pkg / "manifest.sig").write_text(base64.b64encode(vendor.sign(payload)).decode())
        with pytest.raises(LifecycleError):
            bundle.open_bundle(pkg, [pub_bytes(vendor)])


def test_unsafe_file_names_and_zip_paths_are_refused(tmp_path, vendor):
    pkg = make_package(tmp_path, vendor)
    m = json.loads((pkg / "manifest.json").read_text())
    m["files"]["../outside.txt"] = "0" * 64
    payload = json.dumps(m).encode()
    (pkg / "manifest.json").write_bytes(payload)
    (pkg / "manifest.sig").write_text(base64.b64encode(vendor.sign(payload)).decode())
    with pytest.raises(LifecycleError, match="unsafe"):
        bundle.open_bundle(pkg, [pub_bytes(vendor)])
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escaped.txt", "x")
    with pytest.raises(LifecycleError, match="unsafe path"):
        bundle.open_bundle(evil, [pub_bytes(vendor)])
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_not_a_package_gives_plain_messages(tmp_path, vendor):
    keys = [pub_bytes(vendor)]
    with pytest.raises(LifecycleError, match="No such package"):
        bundle.open_bundle(tmp_path / "nope.zip", keys)
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"this is not a zip")
    with pytest.raises(LifecycleError, match="not a readable zip"):
        bundle.open_bundle(junk, keys)
    (tmp_path / "empty").mkdir()
    with pytest.raises(LifecycleError, match="not an ShelfSense"):
        bundle.open_bundle(tmp_path / "empty", keys)


def test_version_parsing():
    assert bundle.parse_version("1.10.0") > bundle.parse_version("1.9.9")
    for bad in ("", "v1", "1.x", "1..2", None):
        with pytest.raises(LifecycleError):
            bundle.parse_version(bad)


# ---------- activation ----------
def test_licence_states(tmp_path, vendor):
    keys = [pub_bytes(vendor)]
    ok = make_licence(tmp_path / "ok.json", vendor)
    assert licence.check_licence(ok, keys)["status"] == "valid"
    old = make_licence(tmp_path / "old.json", vendor, expires="2020-01-01")
    assert licence.check_licence(old, keys)["status"] == "expired"
    assert licence.check_licence(old, keys, today=date(2019, 1, 1))["status"] == "valid"
    other = make_licence(tmp_path / "other.json", Ed25519PrivateKey.generate())
    assert licence.check_licence(other, keys)["status"] == "invalid"
    wrong = make_licence(tmp_path / "wrong.json", vendor, product="something-else")
    assert licence.check_licence(wrong, keys)["status"] == "invalid"
    (tmp_path / "junk.json").write_text("nope")
    assert licence.check_licence(tmp_path / "junk.json", keys)["status"] == "invalid"
    assert licence.check_licence(tmp_path / "missing.json", keys)["status"] == "invalid"
    doc = json.loads(ok.read_text())
    doc["licence"]["expires"] = "2999-01-01"
    ok.write_text(json.dumps(doc))
    assert licence.check_licence(ok, keys)["status"] == "invalid", "an edited licence must not verify"
    with pytest.raises(LifecycleError, match="Activation failed"):
        licence.require_valid(old, keys)


# ---------- SBOM ----------
def fake_wheel(path: Path, name: str, version: str, **meta) -> None:
    lines = [f"Name: {name}", f"Version: {version}"] + [f"{k}: {v}" for k, v in meta.items()]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{name}-{version}.dist-info/METADATA", "\n".join(lines) + "\n")


@pytest.mark.req("NFR04")
def test_sbom_lists_licences_and_flags_copyleft(tmp_path):
    fake_wheel(tmp_path / "a-1-py3-none-any.whl", "alpha", "1.0", **{"License-Expression": "MIT"})
    fake_wheel(tmp_path / "b-2-py3-none-any.whl", "beta", "2.0", License="GPL-3.0-or-later")
    fake_wheel(tmp_path / "c-3-py3-none-any.whl", "gamma", "3.0")
    items = sbom.inventory(tmp_path)
    by = {i["name"]: i for i in items}
    assert by["alpha"]["licence"] == "MIT" and not by["alpha"]["needs_review"]
    assert by["beta"]["needs_review"], "copyleft needs a legal look"
    assert by["gamma"]["licence"] == "UNKNOWN" and by["gamma"]["needs_review"]
    bom = sbom.cyclonedx(items, "shelfsense", "1.0")
    assert bom["bomFormat"] == "CycloneDX" and len(bom["components"]) == 3
    assert bom["components"][0]["purl"] == "pkg:pypi/alpha@1.0"
    text = sbom.licences_markdown(items, "shelfsense", "1.0")
    assert "beta" in text and "needs review" in text


# ---------- install ----------
def test_install_needs_a_trusted_key_and_a_valid_licence(tmp_path, vendor, fake_release, no_smoke):
    pkg = make_package(tmp_path / "in", vendor, "0.1.0", SCHEMA_OLD)
    lic = make_licence(tmp_path / "licence.json", vendor)
    root = ops.Root(tmp_path / "r1")
    with pytest.raises(LifecycleError, match="Say which key"):
        ops.install(pkg, root, lic)
    stranger = _keyfile(tmp_path, Ed25519PrivateKey.generate())
    with pytest.raises(LifecycleError, match="trusted key"):
        ops.install(pkg, root, lic, trusted_key_file=stranger)
    expired = make_licence(tmp_path / "old.json", vendor, expires="2020-01-01")
    with pytest.raises(LifecycleError, match="expired"):
        ops.install(pkg, root, expired, trusted_key_file=_keyfile(tmp_path, vendor))
    assert ops.read_state(root)["current"] is None, "a refused install leaves nothing behind that counts as installed"


def test_install_sets_everything_up_and_runs_the_checks(installed, no_smoke):
    root = installed
    assert ops.read_state(root) == {"current": "0.1.0", "previous": None}
    assert root.db_file.is_file() and root.secret_file.read_text().strip()
    assert root.trusted_file.is_file() and root.licence_file.is_file() and (root.path / "manage.py").is_file()
    assert [v for v, _ in no_smoke] == ["0.1.0"], "it started the app once to prove it works"
    s = ops.status(root)
    assert s["installed"] and s["licence"]["status"] == "valid" and s["history"][0]["action"] == "install"
    sec = root.secret_file.read_text()
    assert len(sec) >= 32, "the production secret must be long enough"


def test_install_twice_is_refused(installed, tmp_path, vendor):
    pkg = make_package(tmp_path / "again", vendor, "0.1.0", SCHEMA_OLD)
    with pytest.raises(LifecycleError, match="already installed"):
        ops.install(
            pkg, installed, make_licence(tmp_path / "l2.json", vendor), trusted_key_file=_keyfile(tmp_path, vendor)
        )


def test_a_failed_install_removes_its_half_built_release(tmp_path, vendor, fake_release, monkeypatch):
    monkeypatch.setattr(ops, "smoke", lambda *a, **k: (_ for _ in ()).throw(LifecycleError("did not start")))
    root = ops.Root(tmp_path / "r")
    pkg = make_package(tmp_path / "in", vendor, "0.1.0", SCHEMA_OLD)
    with pytest.raises(LifecycleError, match="did not start"):
        ops.install(pkg, root, make_licence(tmp_path / "l.json", vendor), trusted_key_file=_keyfile(tmp_path, vendor))
    assert not root.release_dir("0.1.0").exists() and ops.read_state(root)["current"] is None


@pytest.mark.skipif(sys.platform != "win32", reason="the 260-character path limit is a Windows rule")
def test_a_too_deep_install_folder_is_refused_up_front(tmp_path, vendor, no_smoke):
    deep = tmp_path / ("d" * 70)  # long enough to pass 150 characters, short enough to create
    pkg = make_package(tmp_path / "in", vendor, "0.1.0", SCHEMA_OLD)
    root = ops.Root(deep)
    with pytest.raises(LifecycleError, match="too deep"):
        ops.install(pkg, root, make_licence(tmp_path / "l.json", vendor), trusted_key_file=_keyfile(tmp_path, vendor))
    assert ops.read_state(root)["current"] is None


# ---------- update ----------
def new_pkg(tmp_path, vendor, version="0.2.0", schema=SCHEMA_NEW, **kw):
    return make_package(tmp_path / "up", vendor, version, schema, **kw)


def test_dry_run_changes_nothing(installed, tmp_path, vendor):
    before = ops.read_history(installed)
    b, plan = ops.plan_update(installed, new_pkg(tmp_path, vendor))
    b.close()
    assert plan["from"] == "0.1.0" and plan["to"] == "0.2.0" and plan["schema_to"] == SCHEMA_NEW
    assert ops.read_state(installed)["current"] == "0.1.0" and ops.read_history(installed) == before
    assert not installed.release_dir("0.2.0").exists()


def test_update_applies_keeps_data_and_the_old_release(installed, tmp_path, vendor, no_smoke):
    seed(installed)
    n = users(installed)
    result = ops.update(installed, new_pkg(tmp_path, vendor), log=lambda m: None)
    assert ops.read_state(installed) == {"current": "0.2.0", "previous": "0.1.0"}
    assert installed.release_dir("0.1.0").exists() and installed.release_dir("0.2.0").exists()
    assert users(installed) == n, "the data came through the update"
    assert Path(result["backup"]).is_file() and "pre-update-0.2.0" in Path(result["backup"]).name.replace("_", ".")
    assert not installed.journal_file.exists()
    h = ops.read_history(installed)[0]
    assert h["action"] == "update" and h["ok"] and h["from"] == "0.1.0" and h["to"] == "0.2.0" and h["backup"]
    versions = [v for v, _ in no_smoke]
    assert versions[-2:] == ["0.2.0", "0.2.0"], "rehearsal on a copy and a final start on the real data"
    rehearsal_url = no_smoke[-2][1]
    assert rehearsal_url != installed.db_url, "the rehearsal ran on a copy, never the real database"


@pytest.mark.parametrize("version, message", [("0.1.0", "must be newer"), ("0.0.9", "must be newer")])
def test_an_update_must_be_newer(installed, tmp_path, vendor, version, message):
    with pytest.raises(LifecycleError, match=message):
        ops.update(installed, new_pkg(tmp_path, vendor, version), log=lambda m: None)


def test_update_refuses_tampered_unsigned_or_foreign_packages_before_touching_anything(installed, tmp_path, vendor):
    pkg = new_pkg(tmp_path, vendor)
    (pkg / "config.sample.toml").write_text("evil")
    with pytest.raises(LifecycleError, match="altered"):
        ops.update(installed, pkg, log=lambda m: None)
    foreign = make_package(tmp_path / "f", Ed25519PrivateKey.generate(), "0.3.0")
    with pytest.raises(LifecycleError, match="trusted key"):
        ops.update(installed, foreign, log=lambda m: None)
    assert ops.read_state(installed)["current"] == "0.1.0"
    assert not list(installed.backup_dir.glob("pre-update-*")), "refused before any backup was needed"


def test_min_from_version_and_python_requirements(installed, tmp_path, vendor):
    with pytest.raises(LifecycleError, match="at least version 0.1.5"):
        ops.update(installed, new_pkg(tmp_path, vendor, min_from_version="0.1.5"), log=lambda m: None)
    with pytest.raises(LifecycleError, match="needs Python 99.0"):
        ops.update(installed, new_pkg(tmp_path, vendor, "0.3.0", python_min="99.0"), log=lambda m: None)


def test_expired_licence_blocks_features_but_not_security_updates(installed, tmp_path, vendor, monkeypatch):
    make_licence(installed.licence_file, vendor, expires="2020-01-01")
    with pytest.raises(LifecycleError, match="Activation problem"):
        ops.update(installed, new_pkg(tmp_path, vendor), log=lambda m: None)
    sec = new_pkg(tmp_path, vendor, "0.1.1", SCHEMA_OLD, kind="security")
    ops.update(installed, sec, log=lambda m: None)
    assert ops.read_state(installed)["current"] == "0.1.1"


def test_update_refuses_while_the_app_runs(installed, tmp_path, vendor):
    lock = ops.RunLock(installed.lock_file)
    assert lock.acquire()
    try:
        assert ops.is_running(installed)
        with pytest.raises(LifecycleError, match="running"):
            ops.update(installed, new_pkg(tmp_path, vendor), log=lambda m: None)
        assert not list(installed.backup_dir.glob("pre-update-*"))
        assert [h["action"] for h in ops.read_history(installed)] == ["install"], "a refusal is not a failed update"
    finally:
        lock.release()
    assert not ops.is_running(installed)


def damaging_runner(real, bad_version, wipe: bool = True):
    """Make the REAL migrate of the real database 'fail' after it already changed the data."""

    def run(root, version, args, env=None, timeout=ops.STEP_TIMEOUT_S):
        proc = real(root, version, args, env=env, timeout=timeout)
        live = env is None or env.get("SHELFSENSE_DATABASE_URL") == root.db_url
        if version == bad_version and args == ["migrate"] and live:
            if wipe:
                con = sqlite3.connect(root.db_file)
                con.execute("delete from cards")
                con.commit()
                con.close()
            proc.returncode, proc.stderr = 1, "simulated migration failure"
        return proc

    return run


def test_a_failing_data_upgrade_puts_the_original_data_back(installed, tmp_path, vendor, monkeypatch):
    seed(installed)
    con = sqlite3.connect(installed.db_file)
    cards = con.execute("select count(*) from cards").fetchone()[0]
    con.close()
    assert cards > 0
    monkeypatch.setattr(ops, "run_release", damaging_runner(ops.run_release, "0.2.0"))
    with pytest.raises(LifecycleError, match="still current"):
        ops.update(installed, new_pkg(tmp_path, vendor), log=lambda m: None)
    con = sqlite3.connect(installed.db_file)
    assert con.execute("select count(*) from cards").fetchone()[0] == cards, "the damaged data was restored"
    con.close()
    assert ops.read_state(installed) == {"current": "0.1.0", "previous": None}
    assert not installed.release_dir("0.2.0").exists() and not installed.journal_file.exists()
    h = ops.read_history(installed)[0]
    assert h["action"] == "update" and h["ok"] is False and "simulated" in h["problem"] and h["backup"]


def test_a_rehearsal_failure_never_touches_the_real_data(installed, tmp_path, vendor, monkeypatch):
    seed(installed)
    real = ops.run_release

    def run(root, version, args, env=None, timeout=ops.STEP_TIMEOUT_S):
        if version == "0.2.0" and args == ["migrate"]:
            proc = real(root, version, ["version"], env=env)
            proc.returncode, proc.stderr = 1, "boom on the copy"
            return proc
        return real(root, version, args, env=env, timeout=timeout)

    monkeypatch.setattr(ops, "run_release", run)
    with pytest.raises(LifecycleError, match="on the copy"):
        ops.update(installed, new_pkg(tmp_path, vendor), log=lambda m: None)
    assert ops.read_state(installed)["current"] == "0.1.0"
    live = sqlite3.connect(installed.db_file)
    assert live.execute("select count(*) from cards").fetchone()[0] > 0
    live.close()


def test_a_new_version_that_does_not_start_is_never_switched_to(installed, tmp_path, vendor, monkeypatch):
    calls = {"n": 0}

    def smoke(root, version, url, log=print):
        calls["n"] += 1
        if version == "0.2.0" and url == root.db_url:
            raise LifecycleError("Release 0.2.0 stopped while starting")

    monkeypatch.setattr(ops, "smoke", smoke)
    with pytest.raises(LifecycleError, match="stopped while starting"):
        ops.update(installed, new_pkg(tmp_path, vendor), log=lambda m: None)
    assert ops.read_state(installed)["current"] == "0.1.0" and not installed.release_dir("0.2.0").exists()


# ---------- rollback ----------
def test_rollback_restores_the_data_as_it_was_before_the_update(installed, tmp_path, vendor):
    seed(installed)
    n = users(installed)
    ops.update(installed, new_pkg(tmp_path, vendor), log=lambda m: None)
    con = sqlite3.connect(installed.db_file)  # work done after the update
    con.execute("delete from cards")
    con.commit()
    con.close()
    plan = ops.rollback_plan(installed)
    assert plan["schema_changed"] and plan["to"] == "0.1.0"
    with pytest.raises(LifecycleError, match="--yes"):
        ops.rollback(installed, log=lambda m: None)
    assert ops.read_state(installed)["current"] == "0.2.0", "nothing changes without confirmation"
    ops.rollback(installed, yes=True, log=lambda m: None)
    assert ops.read_state(installed) == {"current": "0.1.0", "previous": None}
    con = sqlite3.connect(installed.db_file)
    assert con.execute("select count(*) from cards").fetchone()[0] > 0 and users(installed) == n
    con.close()
    assert list(installed.backup_dir.glob("pre-restore-*.db")), "the post-update data is kept in a safety copy"
    assert ops.read_history(installed)[0]["action"] == "rollback"


def test_rollback_without_a_schema_change_keeps_all_data(installed, tmp_path, vendor):
    ops.update(installed, new_pkg(tmp_path, vendor, "0.1.1", SCHEMA_OLD, kind="patch"), log=lambda m: None)
    plan = ops.rollback_plan(installed)
    assert not plan["schema_changed"] and plan["backup"] == ""
    con = sqlite3.connect(installed.db_file)
    con.execute("delete from cards")  # entered after the update: must survive a code-only rollback
    con.commit()
    con.close()
    ops.rollback(installed, log=lambda m: None)
    assert ops.read_state(installed)["current"] == "0.1.0"
    assert not list(installed.backup_dir.glob("pre-restore-*")), "no data was restored, nothing was replaced"


def test_rollback_needs_a_previous_version_and_a_healthy_backup(installed, tmp_path, vendor):
    with pytest.raises(LifecycleError, match="no previous"):
        ops.rollback(installed, yes=True)
    result = ops.update(installed, new_pkg(tmp_path, vendor), log=lambda m: None)
    Path(result["backup"]).unlink()
    with pytest.raises(LifecycleError, match="backup is missing"):
        ops.rollback(installed, yes=True, log=lambda m: None)
    assert ops.read_state(installed)["current"] == "0.2.0"


def test_rollback_refuses_while_running(installed, tmp_path, vendor):
    ops.update(installed, new_pkg(tmp_path, vendor), log=lambda m: None)
    lock = ops.RunLock(installed.lock_file)
    assert lock.acquire()
    try:
        with pytest.raises(LifecycleError, match="running"):
            ops.rollback(installed, yes=True, log=lambda m: None)
    finally:
        lock.release()


# ---------- recover, prune, status ----------
def test_recover_after_a_crash_mid_migration(installed, tmp_path, vendor):
    seed(installed)
    n_cards = sqlite3.connect(installed.db_file).execute("select count(*) from cards").fetchone()[0]
    proc = ops.run_release(installed, "0.1.0", ["backup", "create", "--label", "crash"])
    backup_path = ops._version_of_backup(proc.stdout)
    installed.release_dir("0.2.0").mkdir()  # the half-built release a crash leaves behind
    con = sqlite3.connect(installed.db_file)
    con.execute("delete from cards")
    con.commit()
    con.close()
    ops._write_json_atomic(
        installed.journal_file, {"from": "0.1.0", "to": "0.2.0", "step": "migrating", "backup": backup_path}
    )
    assert ops.status(installed)["interrupted"]
    with pytest.raises(LifecycleError, match="recover"):
        ops.update(installed, new_pkg(tmp_path, vendor), log=lambda m: None)
    out = ops.recover(installed, log=lambda m: None)
    assert out["restored"]
    assert sqlite3.connect(installed.db_file).execute("select count(*) from cards").fetchone()[0] == n_cards
    assert not installed.journal_file.exists() and not installed.release_dir("0.2.0").exists()
    with pytest.raises(LifecycleError, match="no interrupted"):
        ops.recover(installed)


def test_recover_when_the_update_actually_finished(installed, tmp_path, vendor):
    ops.update(installed, new_pkg(tmp_path, vendor), log=lambda m: None)
    ops._write_json_atomic(installed.journal_file, {"from": "0.1.0", "to": "0.2.0", "step": "switching", "backup": ""})
    assert ops.recover(installed, log=lambda m: None) == {"restored": False}
    assert ops.read_state(installed)["current"] == "0.2.0"


def test_prune_keeps_current_and_previous(installed, tmp_path, vendor):
    ops.update(installed, new_pkg(tmp_path, vendor, "0.2.0"), log=lambda m: None)
    ops.update(installed, new_pkg(tmp_path, vendor, "0.3.0", "0010"), log=lambda m: None)
    assert ops.prune(installed, log=lambda m: None) == ["0.1.0"]
    assert installed.release_dir("0.2.0").exists() and installed.release_dir("0.3.0").exists()


def test_history_survives_a_damaged_line(installed):
    with installed.history_file.open("a") as fh:
        fh.write("{not json\n\n[1,2]\n")
    assert ops.read_history(installed)[0]["action"] == "install"


def test_state_file_damage_reads_as_not_installed(tmp_path):
    root = ops.Root(tmp_path)
    root.state_file.write_text("{{{")
    assert ops.read_state(root) == {"current": None, "previous": None}


# ---------- real processes: the smoke test and the command line ----------
def test_the_smoke_test_really_starts_the_application(installed, monkeypatch):
    monkeypatch.undo()  # use the real smoke() here, with the test environment's own shelfsense
    monkeypatch.setattr(ops, "_release_python", lambda root, version: Path(sys.executable))
    ops.run_release(installed, "0.1.0", ["migrate"])
    ops.smoke(installed, "0.1.0", installed.db_url)


def test_the_smoke_test_reports_data_from_a_newer_version(installed, monkeypatch, tmp_path):
    monkeypatch.undo()
    monkeypatch.setattr(ops, "_release_python", lambda root, version: Path(sys.executable))
    future = tmp_path / "future.db"
    con = sqlite3.connect(future)
    con.execute("create table alembic_version (version_num text)")
    con.execute("insert into alembic_version values ('9999')")  # a schema this release has never heard of
    con.commit()
    con.close()
    with pytest.raises(LifecycleError, match="stopped while starting"):
        ops.smoke(installed, "0.1.0", "sqlite:///" + future.as_posix())


def test_the_smoke_test_reports_a_release_that_crashes(installed, monkeypatch, tmp_path):
    monkeypatch.undo()
    broken = tmp_path / "broken.py"
    broken.write_text("import sys; print('boom: cannot import'); sys.exit(3)")
    monkeypatch.setattr(ops, "_release_python", lambda root, version: Path(sys.executable))
    monkeypatch.setattr(ops, "app_env", lambda root, **extra: {**__import__("os").environ, **extra})
    real_popen = ops.subprocess.Popen
    monkeypatch.setattr(ops.subprocess, "Popen", lambda cmd, **kw: real_popen([sys.executable, str(broken)], **kw))
    with pytest.raises(LifecycleError, match="stopped while starting: boom"):
        ops.smoke(installed, "0.1.0", installed.db_url)


def test_cli_status_and_verify_and_errors(installed, tmp_path, vendor, capsys):
    from shelfsense_lifecycle.__main__ import main

    assert main(["--root", str(installed.path), "status"]) == 0
    assert "ShelfSense 0.1.0" in capsys.readouterr().out
    pkg = new_pkg(tmp_path, vendor)
    assert main(["--root", str(installed.path), "verify", str(pkg)]) == 0
    assert "OK: shelfsense 0.2.0" in capsys.readouterr().out
    (pkg / "config.sample.toml").write_text("x")
    assert main(["--root", str(installed.path), "verify", str(pkg)]) == 1
    assert "altered" in capsys.readouterr().err
    assert main(["--root", str(installed.path), "update", str(pkg), "--dry-run"]) == 1
    assert main(["--root", str(installed.path), "rollback"]) == 1
    assert "no previous" in capsys.readouterr().err
    assert main(["--root", str(tmp_path / "nothing"), "status"]) == 0
    assert "Nothing installed" in capsys.readouterr().out


# ------------------------------------------------------------------ the entry scripts must not touch the package
def _release_script():
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "release.py"
    spec = importlib.util.spec_from_file_location("release_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_entry_script(tmp_path: Path, name: str, text: str) -> list[Path]:
    """Run an entry script next to a stand-in `tools` folder, with bytecode writing ON; list the files left behind."""
    import os
    import subprocess

    pkg = tmp_path / "pkg"
    shutil.rmtree(pkg, ignore_errors=True)
    tools = pkg / "tools" / "shelfsense_lifecycle"
    tools.mkdir(parents=True)
    (tools / "__init__.py").write_text("")
    (tools / "__main__.py").write_text("def main(argv):\n    return 0\n")
    (pkg / name).write_text(text, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(pkg / name), "--root", str(tmp_path / "root")], env=env, capture_output=True, check=False
    )
    assert done.returncode == 0, done.stderr
    return sorted(pkg.rglob("*"))


@pytest.mark.parametrize("which", ["install.py", "manage.py"])
def test_running_an_entry_script_adds_no_files_to_the_signed_package(tmp_path, which):
    """Found by CI: importing the tools wrote __pycache__ into the package, so the installer refused it as tampered."""
    text = _release_script().INSTALL_PY if which == "install.py" else ops.MANAGE_PY
    files = _run_entry_script(tmp_path, which, text)
    assert not [p for p in files if "__pycache__" in p.parts or p.suffix == ".pyc"], files
    # a control, so this test cannot pass for the wrong reason: without the fix the folder DOES get bytecode
    unfixed = _run_entry_script(tmp_path, which, text.replace("sys.dont_write_bytecode = True", "pass"))
    assert [p for p in unfixed if "__pycache__" in p.parts], "the control run did not write bytecode"
