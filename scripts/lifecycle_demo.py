"""One-command rehearsal of the whole offline lifecycle, in a throw-away folder.

    python scripts/lifecycle_demo.py [--keep] [--wheels-from DIR] [--work DIR]

It builds a 0.1.0 package and a 0.2.0 package that ships a database change, then, with the network blocked:
  0. proves the block works (a probe, and a grandchild started with a clean environment, are both refused)
  1. installs 0.1.0 and loads demo data
  2. refuses a tampered 0.2.0, a foreign-signed one and a downgrade
  3. applies 0.2.0 (backup, rehearsal on a copy, migration, start-up check, switch)
  4. rolls back to 0.1.0 and proves the data is as it was
How the network is blocked on the target-machine steps: every Python process (install.py, manage.py, pip, the new
release's migrate and start-up check, and anything they start) loads a `sitecustomize` that refuses every connection and
name lookup that is not to this machine (127.0.0.1, ::1, localhost), logs the attempt, and passes itself on to each
child process, even one started with a cleaned environment as the installer does. A dead proxy is set as well.
At the end the log must be empty: no step tried to reach the network. pip also gets `--no-index`.
With --keep the folder stays (0.2.0 waits in incoming/) so the Updates page can be tried: see docs/deployment.md.
Needs the dev extras (cryptography) and internet for the build step unless --wheels-from is given.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess  # noqa: S404
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RELEASE = [sys.executable, str(REPO / "scripts" / "release.py")]
OFFLINE = {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost"}
NETGUARD = r'''"""Written by scripts/lifecycle_demo.py: refuse non-local connections here and in every child process."""

import errno
import os
import socket
import subprocess

_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG = os.environ.get("AISYS_NETGUARD_LOG", "")
_LOCAL = ("127.", "::1", "localhost", "0.0.0.0", "::")


def _local(host):
    host = host.decode() if isinstance(host, bytes) else str(host or "")
    return host == "" or host.lower().startswith(_LOCAL)


def _refuse(what):
    if _LOG:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {what}\n")
    raise OSError(errno.ENETUNREACH, f"network blocked by lifecycle_demo: {what}")


_connect, _connect_ex, _getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo


def connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _local(address[0]):
        _refuse(f"connect {address[0]}:{address[1]}")
    return _connect(self, address)


def connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _local(address[0]):
        _refuse(f"connect {address[0]}:{address[1]}")
    return _connect_ex(self, address)


def getaddrinfo(host, *args, **kwargs):
    if not _local(host):
        _refuse(f"lookup {host}")
    return _getaddrinfo(host, *args, **kwargs)


socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, getaddrinfo
_popen_init = subprocess.Popen.__init__


def _popen(self, *args, **kwargs):
    env = dict(os.environ if kwargs.get("env") is None else kwargs["env"])
    paths = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join([_DIR, *[p for p in paths if p != _DIR]])
    env["AISYS_NETGUARD_LOG"] = _LOG
    kwargs["env"] = env
    _popen_init(self, *args, **kwargs)


subprocess.Popen.__init__ = _popen
'''
PROBE = """
import socket, subprocess, sys
try:
    socket.create_connection(("192.0.2.1", 443), timeout=2)
    sys.exit("probe: the connection was NOT blocked")
except OSError as exc:
    assert "blocked by lifecycle_demo" in str(exc), exc
clean = {"SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}  # like the installer: no PYTHONPATH
child = subprocess.run([sys.executable, "-c", "import socket; socket.getaddrinfo('pypi.org', 443)"], env=clean,
                       capture_output=True, text=True)
assert child.returncode != 0 and "blocked by lifecycle_demo" in child.stderr, child.stderr
print("network block active in this process and in a child started with a clean environment")
"""
GUARD: dict[str, str] = {}  # PYTHONPATH and log location, set once the guard is written
MIGRATION = '''"""demo schema change shipped by the 0.2.0 update package"""

import sqlalchemy as sa
from alembic import op

revision = "{rev}"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("update_marker", sa.Column("id", sa.Integer, primary_key=True), sa.Column("note", sa.String(100)))


def downgrade() -> None:
    op.drop_table("update_marker")
'''


def run(cmd: list[str], offline: bool = False, expect_fail: bool = False) -> str:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}  # run as a fresh machine would
    env.update({**OFFLINE, **GUARD} if offline else {})
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)  # noqa: S603
    out = (proc.stdout + proc.stderr).strip()
    if expect_fail and proc.returncode == 0:
        sys.exit(f"EXPECTED A REFUSAL but it succeeded: {' '.join(cmd[-3:])}\n{out}")
    if not expect_fail and proc.returncode != 0:
        sys.exit(f"FAILED: {' '.join(cmd)}\n{out}")
    return out


def count(db: Path, table: str) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608 - fixed names
    finally:
        con.close()


def has_table(db: Path, table: str) -> bool:
    con = sqlite3.connect(db)
    try:
        return bool(con.execute("select 1 from sqlite_master where name=?", (table,)).fetchone())
    finally:
        con.close()


def step(text: str) -> None:
    print(f"\n=== {text}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--wheels-from", type=Path)
    ap.add_argument("--work", type=Path)
    a = ap.parse_args()
    work = a.work or Path(tempfile.mkdtemp(prefix="aisys-demo-"))
    work.mkdir(parents=True, exist_ok=True)
    wf = ["--wheels-from", str(a.wheels_from)] if a.wheels_from else []
    keys, other, dist, root = work / "keys", work / "other-keys", work / "dist", work / "install"
    step("Build machine: keys, licence and two signed packages")
    run([*RELEASE, "keygen", "--out", str(keys)])
    run([*RELEASE, "keygen", "--out", str(other)])
    run(
        [
            *RELEASE,
            "licence",
            "--key",
            str(keys / "private.key"),
            "--customer",
            "Demo Library",
            "--expires",
            "2999-12-31",
            "--out",
            str(work / "licence.json"),
        ]
    )
    print(
        run(
            [*RELEASE, "build", "--key", str(keys / "private.key"), "--out", str(dist), "--notes", "first release", *wf]
        ).splitlines()[-1]
    )
    src2 = work / "src2"
    shutil.copytree(
        REPO,
        src2,
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
    versions = src2 / "src" / "aisys" / "migrations" / "versions"
    head = max(
        int(f.name[:4]) for f in versions.glob("[0-9][0-9][0-9][0-9]_*.py")
    )  # the demo change comes after the last real one
    rev, down = f"{head + 1:04d}", f"{head:04d}"
    (versions / f"{rev}_update_marker.py").write_text(MIGRATION.format(rev=rev, down=down))
    print(
        run(
            [
                *RELEASE,
                "build",
                "--key",
                str(keys / "private.key"),
                "--source",
                str(src2),
                "--version",
                "0.2.0",
                "--out",
                str(dist),
                "--notes",
                "adds the update_marker table",
                *wf,
            ]
        ).splitlines()[-1]
    )
    foreign = work / "dist-foreign"
    print(
        run(
            [
                *RELEASE,
                "build",
                "--key",
                str(other / "private.key"),
                "--source",
                str(src2),
                "--version",
                "0.3.0",
                "--out",
                str(foreign),
                *wf,
            ]
        ).splitlines()[-1]
    )

    manage = [sys.executable, str(root / "manage.py")]
    step("Target machine: block the network and prove the block works")
    guard_dir, guard_log = work / "netguard", work / "netguard.log"
    guard_dir.mkdir()
    (guard_dir / "sitecustomize.py").write_text(NETGUARD, encoding="utf-8")
    GUARD.update(PYTHONPATH=str(guard_dir), AISYS_NETGUARD_LOG=str(guard_log))
    print(run([sys.executable, "-c", PROBE], offline=True))
    guard_log.unlink()  # the probe's two refusals; from here on the log must stay empty

    step("Target machine, NO network: install 0.1.0")
    print(
        run(
            [
                sys.executable,
                str(dist / "aisys-0.1.0" / "install.py"),
                "--root",
                str(root),
                "--licence",
                str(work / "licence.json"),
                "--trusted-key",
                str(keys / "public.key"),
                "--seed-demo",
            ],
            offline=True,
        )
    )
    db = root / "data" / "aisys.db"
    users, cards = count(db, "users"), count(db, "cards")
    print(f"data after install: {users} users, {cards} cards")

    step("Refusals (nothing may change)")
    tampered = work / "tampered"
    shutil.copytree(dist / "aisys-0.2.0", tampered)
    with (tampered / "config.sample.toml").open("a") as fh:
        fh.write("\n# tampered\n")
    print("tampered :", run([*manage, "update", str(tampered)], offline=True, expect_fail=True))
    print("foreign  :", run([*manage, "update", str(foreign / "aisys-0.3.0.zip")], offline=True, expect_fail=True))
    print("downgrade:", run([*manage, "update", str(dist / "aisys-0.1.0.zip")], offline=True, expect_fail=True))

    step("Dry run, then apply 0.2.0 (offline)")
    print(run([*manage, "update", str(dist / "aisys-0.2.0.zip"), "--dry-run"], offline=True))
    print(run([*manage, "update", str(dist / "aisys-0.2.0.zip")], offline=True))
    assert has_table(db, "update_marker") and count(db, "users") == users and count(db, "cards") == cards
    print(f"after update: update_marker table present, still {users} users and {cards} cards")

    step("Something goes wrong after the update: roll back")
    con = sqlite3.connect(db)
    con.execute("delete from cards")
    con.commit()
    con.close()
    print(f"(simulated damage: cards now {count(db, 'cards')})")
    print("without --yes:", run([*manage, "rollback"], offline=True, expect_fail=True))
    print(run([*manage, "rollback", "--yes"], offline=True))
    assert not has_table(db, "update_marker") and count(db, "users") == users and count(db, "cards") == cards
    print(f"after rollback: schema back to {down}, {users} users and {cards} cards as before the update")
    print(run([*manage, "status"], offline=True))
    attempts = guard_log.read_text(encoding="utf-8") if guard_log.exists() else ""
    if attempts:
        sys.exit(f"A target-machine step tried to reach the network:\n{attempts}")
    print("network log empty: no target-machine step tried to reach the network")
    print("\nALL CHECKS PASSED")
    if a.keep:
        shutil.copyfile(dist / "aisys-0.2.0.zip", root / "incoming" / "aisys-0.2.0.zip")
        print(f"\nkept in {work}\nstart it:  python {root / 'manage.py'} run   (0.2.0 is waiting in incoming/)")
    else:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
