"""Install, update, rollback, recover and status for an ShelfSense installation folder.

The folder ("root") looks like this and nothing in it is ever hidden from the operator:

    releases/<version>/venv   one self-contained environment per version (old ones are kept for rollback)
    state.json                which release is current and which is previous (switched atomically, last)
    history.jsonl             one line per install / update / rollback / recover
    config/                   config.toml, secret.key, licence.json, trusted.pub (survive every update)
    data/                     the database and its backups (never inside a release folder)
    tools/                    this package, so the commands work even when a release is broken
    run/app.lock              held while the application runs

An update is deliberately boring and ordered so that the only irreversible moment is the last one:
verify -> back up -> install beside the old version -> rehearse on a COPY of the data -> migrate the real data ->
start it once to prove it works -> switch the pointer. Anything failing before the switch puts the data back from the
backup and leaves the old version exactly as it was.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess  # noqa: S404 - runs this installation's own python, never a shell
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from shelfsense_lifecycle import bundle as bundles
from shelfsense_lifecycle import licence
from shelfsense_lifecycle.bundle import LifecycleError, parse_version

Log = Callable[[str], None]
PIP_TIMEOUT_S = 900
STEP_TIMEOUT_S = 600
SMOKE_TIMEOUT_S = 60
MAX_VENV_PATH = 150  # Windows allows 260 characters per path; the longest file inside a venv adds about 100


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Root:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.releases = self.path / "releases"
        self.state_file = self.path / "state.json"
        self.history_file = self.path / "history.jsonl"
        self.journal_file = self.path / "update-in-progress.json"
        self.config_dir = self.path / "config"
        self.config_file = self.config_dir / "config.toml"
        self.secret_file = self.config_dir / "secret.key"
        self.licence_file = self.config_dir / "licence.json"
        self.trusted_file = self.config_dir / "trusted.pub"
        self.data_dir = self.path / "data"
        self.backup_dir = self.data_dir / "backups"
        self.db_file = self.data_dir / "shelfsense.db"
        self.incoming = self.path / "incoming"
        self.tools = self.path / "tools"
        self.lock_file = self.path / "run" / "app.lock"

    def release_dir(self, version: str) -> Path:
        return self.releases / version

    @property
    def db_url(self) -> str:
        return "sqlite:///" + self.db_file.as_posix()


# ---------- small files: state, history, journal ----------
def _write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_state(root: Root) -> dict:
    try:
        raw = json.loads(root.state_file.read_text("utf-8"))
        return {"current": raw.get("current"), "previous": raw.get("previous")}
    except (OSError, ValueError, AttributeError):
        return {"current": None, "previous": None}


def write_state(root: Root, current: str | None, previous: str | None) -> None:
    _write_json_atomic(root.state_file, {"current": current, "previous": previous})


def add_history(root: Root, **entry) -> None:
    root.path.mkdir(parents=True, exist_ok=True)
    with root.history_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now(), **entry}) + "\n")


def read_history(root: Root, limit: int = 50) -> list[dict]:
    rows: list[dict] = []
    try:
        lines = root.history_file.read_text("utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except ValueError:
            continue  # a damaged line never hides the rest of the history
    return rows[-limit:][::-1]


# ---------- running the application ----------
def venv_python(release: Path) -> Path:
    return release / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _release_python(root: Root, version: str) -> Path:
    path = venv_python(root.release_dir(version))
    if not path.is_file():
        raise LifecycleError(f"Release {version} is not installed correctly (no python at {path}).")
    return path


def app_env(root: Root, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP")}
    env.update(
        SHELFSENSE_HOME=str(root.path),
        SHELFSENSE_CONFIG=str(root.config_file),
        SHELFSENSE_DATABASE_URL=root.db_url,
        SHELFSENSE_BACKUP_DIR=str(root.backup_dir),
        PYTHONIOENCODING="utf-8",
        PYTHONNOUSERSITE="1",
    )
    try:
        env["SHELFSENSE_SECRET_KEY"] = root.secret_file.read_text("utf-8").strip()
    except OSError:
        pass
    env.update(extra)
    return env


def run_release(
    root: Root, version: str, args: list[str], env: dict[str, str] | None = None, timeout: int = STEP_TIMEOUT_S
):
    """Run `python -m shelfsense.cli <args>` of one release; returns the CompletedProcess (no raise on failure)."""
    python = _release_python(root, version)
    cmd = [str(python), "-m", "shelfsense.cli", "--config", str(root.config_file), *args]
    return subprocess.run(  # noqa: S603 - fixed argument list, no shell
        cmd,
        env=env or app_env(root),
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def _tail(proc) -> str:
    text = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    return " | ".join(text[-4:])[:600] if text else "(no output)"


class RunLock:
    """Held by `manage run` while the app is up, so an update can tell the app is running without guessing."""

    def __init__(self, path: Path):
        self.path = path
        self._fh: IO[bytes] | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def is_running(root: Root) -> bool:
    lock = RunLock(root.lock_file)
    if lock.acquire():
        lock.release()
        return False
    return True


def run_app(root: Root, log: Log = print) -> int:
    state = read_state(root)
    if not state["current"]:
        raise LifecycleError("Nothing is installed here yet. Run install first.")
    lock = RunLock(root.lock_file)
    if not lock.acquire():
        raise LifecycleError("The application is already running from this folder.")
    try:
        python = _release_python(root, state["current"])
        log(f"starting ShelfSense {state['current']} (Ctrl+C to stop)")
        proc = subprocess.run(  # noqa: S603
            [str(python), "-m", "shelfsense.cli", "--config", str(root.config_file), "run"],
            env=app_env(root),
            check=False,
        )
        return proc.returncode
    except KeyboardInterrupt:
        return 0
    finally:
        lock.release()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def smoke(root: Root, version: str, database_url: str, log: Log = print) -> None:
    """Start the release for real on a spare port and require it to report ready. Raises LifecycleError if it cannot."""
    port = _free_port()
    python = _release_python(root, version)
    env = app_env(
        root,
        SHELFSENSE_DATABASE_URL=database_url,
        SHELFSENSE_PORT=str(port),
        SHELFSENSE_HOST="127.0.0.1",
        SHELFSENSE_TLS_CERTFILE="",  # the check talks plain HTTP on a spare local port
        SHELFSENSE_TLS_KEYFILE="",
    )
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(  # noqa: S603
            [str(python), "-m", "shelfsense.cli", "--config", str(root.config_file), "run"],
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + SMOKE_TIMEOUT_S
            last = "no answer yet"
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    out.seek(0)
                    text = out.read().decode("utf-8", "replace").strip().splitlines()
                    raise LifecycleError(f"Release {version} stopped while starting: " + " | ".join(text[-4:])[:500])
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=3) as resp:  # noqa: S310
                        if resp.status == 200:
                            return
                except urllib.error.HTTPError as exc:
                    last = f"HTTP {exc.code}"
                except (urllib.error.URLError, OSError) as exc:
                    last = type(exc).__name__
                time.sleep(0.5)
            raise LifecycleError(f"Release {version} did not become ready within {SMOKE_TIMEOUT_S} s ({last}).")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


# ---------- building a release folder ----------
def _create_release(root: Root, b: bundles.Bundle, log: Log) -> None:
    """Make releases/<version>/ with its own virtual environment, installed only from the bundle's wheels."""
    target = root.release_dir(b.version)
    if os.name == "nt" and len(str(target / "venv")) > MAX_VENV_PATH:
        raise LifecycleError(
            f"The installation folder is too deep for Windows ({len(str(target / 'venv'))} characters before the files "
            f"inside it; the limit is {MAX_VENV_PATH}). Choose a shorter folder such as C:/shelfsense."
        )
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    log(f"creating environment for {b.version}")
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "venv", str(target / "venv")], capture_output=True, text=True, timeout=300, check=False
    )
    if proc.returncode != 0:
        raise LifecycleError("Could not create the environment for the new version: " + _tail(proc))
    python = venv_python(target)
    pip = [
        str(python), "-m", "pip", "--disable-pip-version-check", "install", "--no-index",
        "--find-links", str(b.wheels), f"shelfsense=={b.version}",
    ]  # fmt: skip
    log("installing from the package's own wheels (no internet used)")
    proc = subprocess.run(pip, capture_output=True, text=True, timeout=PIP_TIMEOUT_S, env=app_env(root))  # noqa: S603
    if proc.returncode != 0:
        raise LifecycleError("Installing the package failed: " + _tail(proc))
    (target / "release.json").write_text(json.dumps(b.manifest, indent=2), encoding="utf-8")


def _remove_release(root: Root, version: str) -> None:
    shutil.rmtree(root.release_dir(version), ignore_errors=True)


def _check_python(manifest: dict) -> None:
    need = manifest.get("python_min")
    if need and sys.version_info[:2] < tuple(int(x) for x in str(need).split(".")[:2]):
        raise LifecycleError(f"This package needs Python {need} or newer; this computer has {sys.version.split()[0]}.")


def _trusted(root: Root) -> list[bytes]:
    try:
        return [bundles.load_public_key(root.trusted_file.read_text("utf-8"))]
    except OSError as exc:
        raise LifecycleError("This installation has no trusted key (config/trusted.pub is missing).") from exc


def _copy_tools(b: bundles.Bundle, root: Root) -> None:
    src = b.root / "tools"
    if not src.is_dir():
        return
    new = root.path / "tools.new"
    shutil.rmtree(new, ignore_errors=True)
    shutil.copytree(src, new)
    shutil.rmtree(root.tools, ignore_errors=True)
    os.replace(new, root.tools)


# ---------- install ----------
def install(
    source: Path,
    root: Root,
    licence_file: Path,
    trusted_key_file: Path | None = None,
    trust_bundle_key: bool = False,
    seed_demo: bool = False,
    log: Log = print,
) -> dict:
    if read_state(root)["current"]:
        raise LifecycleError(f"ShelfSense {read_state(root)['current']} is already installed here. Use update instead.")
    if trusted_key_file is None and not trust_bundle_key:
        raise LifecycleError(
            "Say which key to trust: --trusted-key <file> (the vendor's key, supplied separately), or "
            "--trust-bundle-key to trust the key shipped in the package (then compare the fingerprint it prints)."
        )
    if trusted_key_file is not None:
        keys = [bundles.load_public_key(Path(trusted_key_file).read_text("utf-8"))]
    else:
        keys = [bundles.load_public_key((_peek_root(source) / "vendor.pub").read_text("utf-8"))]
        log(f"trusting the key shipped in the package: fingerprint {bundles.key_id(keys[0])}")
    b = bundles.open_bundle(source, keys)
    try:
        log(
            f"package {b.version} ({b.manifest['kind']}) verified: signature and "
            f"{len(b.manifest['files'])} file checksums"
        )
        _check_python(b.manifest)
        lic = licence.require_valid(licence_file, keys)
        log(f"activated for {lic['customer'] or 'this site'} (valid until {lic['expires'] or 'no expiry'})")

        for d in (root.config_dir, root.data_dir, root.backup_dir, root.incoming, root.releases):
            d.mkdir(parents=True, exist_ok=True)
        root.trusted_file.write_text(_b64(keys[0]), encoding="utf-8")
        shutil.copyfile(licence_file, root.licence_file)
        if not root.secret_file.exists():
            root.secret_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
            try:
                root.secret_file.chmod(0o600)
            except OSError:
                pass
        if not root.config_file.exists():
            sample = b.root / "config.sample.toml"
            if sample.is_file():
                shutil.copyfile(sample, root.config_file)

        try:
            _create_release(root, b, log)
            log("preparing the database")
            proc = run_release(root, b.version, ["migrate"])
            if proc.returncode != 0:
                raise LifecycleError("Preparing the database failed: " + _tail(proc))
            if seed_demo:
                log("loading the demo library (staff, patrons, books and months of activity; about a minute)")
                proc = run_release(root, b.version, ["seed-demo", "--quiet"])
                if proc.returncode != 0:
                    raise LifecycleError("Loading the demo data failed: " + _tail(proc))
            log("starting it once to prove it works")
            smoke(root, b.version, root.db_url, log)
        except Exception:
            _remove_release(root, b.version)
            raise
        _copy_tools(b, root)
        _write_manage(root)
        write_state(root, b.version, None)
        add_history(root, action="install", to=b.version, kind=b.manifest["kind"], ok=True, signer=b.signer,
                    schema_to=b.manifest["schema_revision"])  # fmt: skip
        log(f"installed ShelfSense {b.version} in {root.path}")
        return {"version": b.version, "root": str(root.path)}
    finally:
        b.close()


def _peek_root(source: Path) -> Path:
    source = Path(source)
    if source.is_dir():
        return bundles._find_root(source)
    raise LifecycleError(
        "--trust-bundle-key needs the package unpacked as a folder (a zip cannot be read before it is verified)."
    )


def _b64(key: bytes) -> str:
    import base64

    return base64.b64encode(key).decode("ascii")


def _write_manage(root: Root) -> None:
    (root.path / "manage.py").write_text(MANAGE_PY, encoding="utf-8")


MANAGE_PY = '''"""Operator entry point: python manage.py status | run | update <pkg> | rollback | recover | prune."""
import os
import sys

sys.dont_write_bytecode = True  # keep the installed tools exactly as they were delivered
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))

from shelfsense_lifecycle.__main__ import main  # noqa: E402

sys.exit(main(["--root", HERE, *sys.argv[1:]]))
'''


# ---------- update ----------
def _version_of_backup(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("backup created:"):
            return line.split(":", 1)[1].split("  (")[0].strip()
    raise LifecycleError("The backup tool did not say where it saved the backup: " + out[:200])


def _restore_data(root: Root, version: str, backup_path: str, log: Log) -> None:
    log("putting the original data back from the backup")
    proc = run_release(root, version, ["backup", "restore", backup_path, "--yes"])
    if proc.returncode != 0:
        raise LifecycleError(
            f"The data could NOT be restored automatically ({_tail(proc)}). Your backup is intact at {backup_path}; "
            f"restore it by hand (docs/deployment.md, 'If an update goes wrong')."
        )


def plan_update(root: Root, source: Path) -> tuple[bundles.Bundle, dict]:
    """Everything that can be checked without changing anything. Raises LifecycleError on the first problem."""
    state = read_state(root)
    if not state["current"]:
        raise LifecycleError("Nothing is installed here yet. Run install first.")
    if root.journal_file.exists():
        raise LifecycleError("An earlier update was interrupted. Run 'recover' first (see docs/deployment.md).")
    b = bundles.open_bundle(source, _trusted(root))
    try:
        _check_python(b.manifest)
        if parse_version(b.version) <= parse_version(state["current"]):
            raise LifecycleError(
                f"This package is version {b.version} but {state['current']} is installed. "
                "An update must be newer; to go back use rollback."
            )
        floor = b.manifest.get("min_from_version")
        if floor and parse_version(state["current"]) < parse_version(floor):
            raise LifecycleError(
                f"This update needs at least version {floor} installed first (you have {state['current']})."
            )
        lic = licence.check_licence(root.licence_file, _trusted(root))
        if lic["status"] != "valid" and b.manifest["kind"] != "security":
            raise LifecycleError(
                "Activation problem: "
                + lic["problem"]
                + " (security updates are still accepted without a valid licence.)"
            )
        size = sum(p.stat().st_size for p in b.root.rglob("*") if p.is_file())
        free = shutil.disk_usage(root.path).free
        if free < size * 3 + (root.db_file.stat().st_size * 3 if root.db_file.exists() else 0):
            raise LifecycleError(f"Not enough free disk space ({free // 2**20} MB free) for this update.")
        return b, {
            "from": state["current"],
            "to": b.version,
            "kind": b.manifest["kind"],
            "notes": b.manifest.get("notes", ""),
            "schema_from": _release_schema(root, state["current"]),
            "schema_to": b.manifest["schema_revision"],
            "signer": b.signer,
            "files": len(b.manifest["files"]),
        }
    except Exception:
        b.close()
        raise


def _release_schema(root: Root, version: str) -> str:
    try:
        return str(
            json.loads((root.release_dir(version) / "release.json").read_text("utf-8")).get("schema_revision", "")
        )
    except (OSError, ValueError):
        return ""


def update(root: Root, source: Path, force: bool = False, log: Log = print) -> dict:
    b, plan = plan_update(root, source)
    if is_running(root) and not force:
        b.close()
        raise LifecycleError("The application is running. Stop it first (an update restarts it anyway).")
    old, new = plan["from"], plan["to"]
    backup_path = ""
    step = "starting"
    migrated = False
    prior = read_state(root)

    def journal(name: str) -> None:
        nonlocal step
        step = name
        _write_json_atomic(
            root.journal_file, {"from": old, "to": new, "step": name, "backup": backup_path, "at": now()}
        )

    try:
        log(f"update {old} -> {new} ({plan['kind']}) verified: signature and {plan['files']} file checksums")
        journal("backup")
        proc = run_release(root, old, ["backup", "create", "--label", f"pre-update-{new}"])
        if proc.returncode != 0:
            raise LifecycleError("Could not back up the data, so nothing was changed: " + _tail(proc))
        backup_path = _version_of_backup(proc.stdout)
        proc = run_release(root, old, ["backup", "verify", backup_path])
        if proc.returncode != 0:
            raise LifecycleError("The backup did not verify, so nothing was changed: " + _tail(proc))
        log(f"backed up the data and verified the backup: {Path(backup_path).name}")

        journal("installing")
        _create_release(root, b, log)

        journal("rehearsal")
        log("rehearsing the data upgrade on a copy (your real data is not touched)")
        with tempfile.TemporaryDirectory(prefix="shelfsense-rehearsal-", ignore_cleanup_errors=True) as tmp:
            copy = Path(tmp) / "rehearsal.db"
            shutil.copyfile(backup_path, copy)
            url = "sqlite:///" + copy.as_posix()
            env = app_env(root, SHELFSENSE_DATABASE_URL=url)
            proc = run_release(root, new, ["migrate"], env=env)
            if proc.returncode != 0:
                raise LifecycleError(
                    "The data upgrade failed on the copy, so the real data was not changed: " + _tail(proc)
                )
            smoke(root, new, url, log)
        log("rehearsal passed")

        journal("migrating")
        migrated = True
        proc = run_release(root, new, ["migrate"])
        if proc.returncode != 0:
            raise LifecycleError("The data upgrade failed: " + _tail(proc))
        journal("verifying")
        log("starting the new version once to prove it works")
        smoke(root, new, root.db_url, log)

        journal("switching")
        write_state(root, new, old)
        _copy_tools(b, root)
        add_history(
            root, action="update", **{"from": old}, to=new, kind=plan["kind"], ok=True, backup=backup_path,
            schema_from=plan["schema_from"], schema_to=plan["schema_to"], signer=b.signer, notes=plan["notes"],
        )  # fmt: skip
        root.journal_file.unlink(missing_ok=True)
        log(f"now running {new}. The previous version {old} is kept, so you can roll back.")
        return {**plan, "backup": backup_path}
    except Exception as exc:
        log(f"update failed at '{step}': {exc}")
        problem = str(exc)
        try:
            if migrated and backup_path:
                _restore_data(root, old, backup_path, log)
        except LifecycleError as restore_error:
            problem += " | " + str(restore_error)
            add_history(
                root, action="update", **{"from": old}, to=new, ok=False, backup=backup_path, step=step, problem=problem
            )
            raise LifecycleError(problem) from exc  # the journal stays so 'recover' can be tried
        _remove_release(root, new)
        write_state(root, prior["current"], prior["previous"])
        root.journal_file.unlink(missing_ok=True)
        add_history(
            root, action="update", **{"from": old}, to=new, ok=False, backup=backup_path, step=step, problem=problem
        )
        raise LifecycleError(f"{problem}  The old version {old} is still current and its data is as it was.") from exc
    finally:
        b.close()


# ---------- rollback ----------
def _last_update_to(root: Root, version: str) -> dict | None:
    for item in read_history(root, 500):
        if item.get("action") == "update" and item.get("ok") and item.get("to") == version:
            return item
    return None


def rollback_plan(root: Root) -> dict:
    state = read_state(root)
    if not state["current"] or not state["previous"]:
        raise LifecycleError("There is no previous version to go back to.")
    prev, cur = state["previous"], state["current"]
    _release_python(root, prev)
    entry = _last_update_to(root, cur) or {}
    schema_changed = bool(entry) and entry.get("schema_from") != entry.get("schema_to")
    backup_path = entry.get("backup", "")
    if schema_changed and not (backup_path and Path(backup_path).is_file()):
        raise LifecycleError(
            "The update changed the database layout, but its backup is missing, so it cannot be undone automatically "
            f"(expected {backup_path or 'a recorded backup'})."
        )
    return {"from": cur, "to": prev, "schema_changed": schema_changed, "backup": backup_path if schema_changed else ""}


def rollback(root: Root, yes: bool = False, log: Log = print) -> dict:
    plan = rollback_plan(root)
    if is_running(root):
        raise LifecycleError("The application is running. Stop it first.")
    prev, cur = plan["to"], plan["from"]
    if plan["schema_changed"]:
        proc = run_release(root, prev, ["backup", "verify", plan["backup"]])
        if proc.returncode != 0:
            raise LifecycleError("The pre-update backup is not healthy, so nothing was changed: " + _tail(proc))
        if not yes:
            raise LifecycleError(
                f"Going back to {prev} puts the data back as it was just before the update "
                f"({Path(plan['backup']).name}). Anything entered since the update will not be in it "
                "(it is kept in a safety copy). Repeat with --yes to continue."
            )
        _restore_data(root, prev, plan["backup"], log)
    else:
        log("the update did not change the database layout, so no data needs restoring")
    log(f"starting {prev} once to prove it works")
    smoke(root, prev, root.db_url, log)
    write_state(root, prev, None)
    add_history(root, action="rollback", **{"from": cur}, to=prev, ok=True, data_restored=plan["schema_changed"],
                backup=plan["backup"])  # fmt: skip
    log(f"rolled back: now running {prev}. {cur} is still on disk for inspection (prune removes it).")
    return plan


# ---------- recover an interrupted update ----------
def recover(root: Root, log: Log = print) -> dict:
    try:
        journal = json.loads(root.journal_file.read_text("utf-8"))
    except (OSError, ValueError):
        raise LifecycleError("There is no interrupted update to recover.") from None
    state = read_state(root)
    old, new, step, backup_path = journal.get("from"), journal.get("to"), journal.get("step"), journal.get("backup", "")
    if state["current"] != old:
        # the switch happened; only the cleanup was missed
        root.journal_file.unlink(missing_ok=True)
        log("the update had actually completed; cleared the leftover marker")
        return {"restored": False}
    restored = False
    if step in ("migrating", "verifying", "switching") and backup_path:
        _restore_data(root, old, backup_path, log)
        restored = True
    if new and new != state["current"]:
        _remove_release(root, new)
    root.journal_file.unlink(missing_ok=True)
    add_history(root, action="recover", **{"from": old}, to=new, ok=True, step=step, data_restored=restored)
    log(
        f"recovered: {old} is current and its data is "
        f"{'restored from ' + Path(backup_path).name if restored else 'untouched'}"
    )
    return {"restored": restored}


def prune(root: Root, log: Log = print) -> list[str]:
    state = read_state(root)
    keep = {state["current"], state["previous"]}
    gone = []
    if root.releases.is_dir():
        for d in sorted(root.releases.iterdir()):
            if d.is_dir() and d.name not in keep:
                shutil.rmtree(d, ignore_errors=True)
                gone.append(d.name)
    log("removed old releases: " + (", ".join(gone) if gone else "none"))
    return gone


# ---------- status ----------
def status(root: Root) -> dict:
    state = read_state(root)
    out = {
        "root": str(root.path),
        "manage": str(root.path / "manage.py"),
        "incoming_dir": str(root.incoming),
        "installed": bool(state["current"]),
        "current": state["current"],
        "previous": state["previous"],
        "running": is_running(root) if state["current"] else False,
        "interrupted": root.journal_file.exists(),
        "schema_revision": _release_schema(root, state["current"]) if state["current"] else "",
        "licence": {"status": "missing"},
        "history": read_history(root, 20),
        "incoming": [],
        "database": str(root.db_file),
    }
    if root.trusted_file.is_file():
        try:
            out["licence"] = licence.check_licence(root.licence_file, _trusted(root))
        except LifecycleError as exc:
            out["licence"] = {"status": "invalid", "problem": str(exc)}
    if root.incoming.is_dir():
        out["incoming"] = sorted(p.name for p in root.incoming.iterdir() if p.suffix.lower() == ".zip" or p.is_dir())
    return out


def database_ok(root: Root) -> bool:
    try:
        con = sqlite3.connect(f"file:{root.db_file.as_posix()}?mode=ro", uri=True)
        try:
            return con.execute("pragma quick_check").fetchone()[0] == "ok"
        finally:
            con.close()
    except sqlite3.Error:
        return False
