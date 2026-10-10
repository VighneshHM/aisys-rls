"""Command line for the installation lifecycle: python -m aisys_lifecycle --root <folder> <command>."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess  # noqa: S404
import sys
from pathlib import Path

from aisys_lifecycle import ops
from aisys_lifecycle.bundle import LifecycleError, load_public_key, open_bundle


def _print_status(s: dict) -> None:
    if not s["installed"]:
        print(f"Nothing installed in {s['root']}")
        return
    print(f"AISYS {s['current']}  (previous: {s['previous'] or 'none'})  in {s['root']}")
    print(f"  application: {'RUNNING' if s['running'] else 'stopped'}   schema {s['schema_revision'] or '?'}")
    lic = s["licence"]
    print(
        f"  licence: {lic['status']}"
        + (
            f" - {lic.get('customer', '')}, expires {lic.get('expires') or 'never'}"
            if lic["status"] != "missing"
            else ""
        )
    )
    if lic.get("problem"):
        print(f"           {lic['problem']}")
    if s["interrupted"]:
        print("  WARNING: an update was interrupted. Run 'recover'.")
    for h in s["history"][:5]:
        mark = "ok  " if h.get("ok") else "FAIL"
        print(f"  {h['at']}  {mark} {h['action']:<8} {h.get('from', '') or ''} -> {h.get('to', '') or ''}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aisys-manage", description="Install, update and roll back AISYS offline.")
    parser.add_argument("--root", required=True, help="the installation folder")
    sub = parser.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", help="first install from a package")
    i.add_argument("--bundle", required=True, help="package folder (or .zip)")
    i.add_argument("--licence", required=True, help="signed licence file")
    i.add_argument("--trusted-key", help="vendor public key file (supplied separately from the package)")
    i.add_argument(
        "--trust-bundle-key", action="store_true", help="trust the key inside the package (check its fingerprint)"
    )
    i.add_argument("--seed-demo", action="store_true", help="load synthetic demo users and cards")
    u = sub.add_parser("update", help="apply an update package")
    u.add_argument("bundle", help="package folder or .zip (a bare name is looked up in the incoming folder)")
    u.add_argument("--dry-run", action="store_true", help="check the package and say what would happen")
    u.add_argument("--force", action="store_true", help="continue even if the application seems to be running")
    r = sub.add_parser("rollback", help="go back to the previous version")
    r.add_argument("--yes", action="store_true", help="confirm that data entered since the update is set aside")
    r.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("status", help="what is installed")
    s.add_argument("--json", action="store_true")
    v = sub.add_parser("verify", help="check a package without installing it")
    v.add_argument("bundle")
    sub.add_parser("doctor", help="check this computer and the installation (environment checklist)")
    ca = sub.add_parser("create-admin", help="create the first administrator (asks for the password)")
    ca.add_argument("--username", required=True)
    ca.add_argument("--name", default="", help="the name shown on pages")
    sub.add_parser("recover", help="finish or undo an interrupted update")
    sub.add_parser("prune", help="delete releases older than the previous one")
    sub.add_parser("run", help="start the application")
    sub.add_parser("history", help="list installs, updates and rollbacks")
    args = parser.parse_args(argv)
    root = ops.Root(args.root)

    try:
        if args.cmd == "install":
            ops.install(
                Path(args.bundle), root, Path(args.licence),
                trusted_key_file=Path(args.trusted_key) if args.trusted_key else None,
                trust_bundle_key=args.trust_bundle_key, seed_demo=args.seed_demo,
            )  # fmt: skip
            print(f"\nStart it with:  python {root.path / 'manage.py'} run")
        elif args.cmd == "update":
            src = Path(args.bundle)
            if not src.exists() and (root.incoming / args.bundle).exists():
                src = root.incoming / args.bundle
            if args.dry_run:
                b, plan = ops.plan_update(root, src)
                b.close()
                print(json.dumps(plan, indent=2))
                print("Dry run: the package is valid and could be applied. Nothing was changed.")
            else:
                ops.update(root, src, force=args.force)
        elif args.cmd == "rollback":
            if args.dry_run:
                print(json.dumps(ops.rollback_plan(root), indent=2))
            else:
                ops.rollback(root, yes=args.yes)
        elif args.cmd == "status":
            st = ops.status(root)
            print(json.dumps(st, indent=2) if args.json else "", end="")
            if not args.json:
                _print_status(st)
        elif args.cmd == "verify":
            keys = [load_public_key(root.trusted_file.read_text("utf-8"))]
            b = open_bundle(Path(args.bundle), keys)
            print(
                f"OK: {b.manifest['name']} {b.version} ({b.manifest['kind']}), signed by {b.signer}, "
                f"{len(b.manifest['files'])} files intact"
            )
            b.close()
        elif args.cmd == "doctor":
            state = ops.read_state(root)
            if not state["current"]:
                raise LifecycleError("Nothing is installed here yet. Run install first.")
            proc = ops.run_release(root, state["current"], ["doctor"])
            print(proc.stdout.rstrip())
            return proc.returncode
        elif args.cmd == "create-admin":
            state = ops.read_state(root)
            if not state["current"]:
                raise LifecycleError("Nothing is installed here yet. Run install first.")
            password = os.environ.get("AISYS_ADMIN_PASSWORD", "")
            if not password:
                password = getpass.getpass("Password: ")
                if getpass.getpass("Password again: ") != password:
                    raise LifecycleError("The two passwords differ")
            cmd = ["create-admin", "--username", args.username, "--name", args.name]
            proc = ops.run_release(root, state["current"], cmd, env=ops.app_env(root, AISYS_ADMIN_PASSWORD=password))
            print((proc.stdout or "").rstrip())
            if proc.returncode:
                print((proc.stderr or "").rstrip(), file=sys.stderr)
            return proc.returncode
        elif args.cmd == "recover":
            ops.recover(root)
        elif args.cmd == "prune":
            ops.prune(root)
        elif args.cmd == "run":
            return ops.run_app(root)
        elif args.cmd == "history":
            for h in ops.read_history(root):
                print(json.dumps(h))
    except LifecycleError as exc:
        print("error:", exc, file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        print("error:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
