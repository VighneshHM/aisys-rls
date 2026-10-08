# ruff: noqa: E501  (this script prints a report)
"""Measure how the application performs with a library of 20,000 items, and print the results as a table.

    python scripts/perf_report.py [--rows 20000] [--seed 42]

Everything runs in this process against a throw-away database (no network, so the numbers are the application's own:
routing, validation, SQLite). Each timed step prints the elapsed seconds; the repeated steps print median and 95th
percentile in milliseconds. The results recorded in docs/performance-results.md come from this script.
"""

from __future__ import annotations

import argparse
import platform
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from aisys import backup  # noqa: E402
from aisys.app import create_app  # noqa: E402
from aisys.cli import seed_demo  # noqa: E402
from aisys.config import Settings  # noqa: E402
from aisys.devtools import sample_data  # noqa: E402
from aisys.models import Item, Loan, Member  # noqa: E402
from aisys.notifications import runner  # noqa: E402
from aisys.notifications.providers import NotificationConfig, build_providers  # noqa: E402

rows_out: list[tuple[str, str, str]] = []


def record(step: str, result: str, note: str = "") -> None:
    rows_out.append((step, result, note))
    print(f"{step:<58} {result:<22} {note}", flush=True)


def timed(fn):
    t = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - t


def percentiles(samples_ms: list[float]) -> str:
    ordered = sorted(samples_ms)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    return f"{statistics.median(ordered):.0f} ms / {p95:.0f} ms"


def repeat(fn, n: int) -> str:
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        r = fn()
        assert r.status_code == 200, (r.status_code, r.text[:200])
        samples.append((time.perf_counter() - t) * 1000)
    return percentiles(samples)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="aisys-perf-"))
    settings = Settings(
        database_url=f"sqlite:///{(tmp / 'perf.db').as_posix()}", backup_dir=str(tmp / "bk"), log_level="CRITICAL"
    )
    print(f"machine: {platform.platform()}, Python {platform.python_version()}, {args.rows} rows, seed {args.seed}\n")
    app, startup = timed(lambda: create_app(settings))
    seed_demo(settings)
    record("Start the application (migrations + wiring), empty database", f"{startup:.2f} s")
    with TestClient(app) as c:
        lib = {
            "Authorization": "Bearer " + c.post("/api/auth/card-login", json={"uid": "STAFFLIB0001"}).json()["token"]
        }
        admin = {
            "Authorization": "Bearer " + c.post("/api/auth/card-login", json={"uid": "STAFFADMIN01"}).json()["token"]
        }

        headers, rows, manifest = sample_data.build_rows(args.rows, args.seed)
        xlsx = tmp / "library.xlsx"
        sample_data.write_xlsx(xlsx, headers, rows)
        r, t = timed(lambda: c.post("/api/migrations", headers=lib, files={"file": (xlsx.name, xlsx.read_bytes())}))
        assert r.status_code == 201, r.text
        s = r.json()["summary"]
        record(
            f"Import {args.rows:,} spreadsheet rows: upload, validate, find duplicates",
            f"{t:.1f} s",
            f"{s['valid']:,} valid, {s['invalid']:,} invalid, {s['duplicate']:,} duplicate",
        )
        bid = r.json()["id"]
        r, t = timed(lambda: c.post(f"/api/migrations/{bid}/commit", headers=lib))
        rec = r.json()["summary"]["reconciliation"]
        record(
            "Commit the import (backup first, one transaction, reconcile)",
            f"{t:.1f} s",
            f"{rec['migrated']:,} items, balanced={rec['balanced']}",
        )

        # --- searching 20,000 items (median / 95th percentile of repeated requests)
        titles = ["lighthouse", "garden", "history", "mathematics", "network", "orchard", "quiet", "atlas"]
        i = iter(range(10**6))
        record(
            "Catalogue search, one word",
            repeat(
                lambda: c.get("/api/catalogue/items", params={"q": titles[next(i) % 8], "limit": 25}, headers=lib), 40
            ),
            "median / 95th percentile",
        )
        record(
            "Catalogue search, two words",
            repeat(
                lambda: c.get(
                    "/api/catalogue/items",
                    params={"q": titles[next(i) % 8] + " " + titles[(next(i) + 3) % 8], "limit": 25},
                    headers=lib,
                ),
                40,
            ),
        )
        record(
            "Catalogue search, unfinished word (as typed)",
            repeat(lambda: c.get("/api/catalogue/items", params={"q": "gard", "limit": 25}, headers=lib), 40),
        )
        record(
            "Catalogue search, a fragment inside a word",
            repeat(lambda: c.get("/api/catalogue/items", params={"q": "ighthou", "limit": 25}, headers=lib), 40),
        )
        record(
            "Public catalogue (OPAC) search",
            repeat(lambda: c.get("/api/opac/search", params={"q": "garden"}), 40) if _no_limit() else "n/a",
        )
        with app.state.session_factory() as db:
            top = db.execute(
                text("SELECT location, count(*) n FROM items GROUP BY location ORDER BY n DESC LIMIT 1")
            ).first()
        record(
            f"Virtual bookshelf, first 300 of the biggest location ({top[1]:,} books)",
            repeat(lambda: c.get("/api/shelves/view", params={"code": top[0]}, headers=lib), 10),
        )
        record("Shelves list (every location with counts)", repeat(lambda: c.get("/api/shelves", headers=lib), 10))

        # --- loans, reports and dashboards over 20,000 items
        with app.state.session_factory() as db:
            people = []
            for n in range(600):  # 600 members with a few loans each, as a library has
                m = Member(
                    member_no=f"P{n:04d}",
                    name=f"Perf Member {n}",
                    email=f"perf{n}@example.org",
                    phone="+15550100001" if n % 2 else "",
                )
                db.add(m)
                people.append(m)
            db.flush()
            ids = list(db.scalars(select(Item.id).limit(3000)))
            today = datetime.now(UTC).replace(hour=17, minute=0, second=0, microsecond=0)
            for n, item_id in enumerate(ids):
                db.add(
                    Loan(
                        member_id=people[n % 600].id,
                        item_id=item_id,
                        due_at=today + timedelta(days=n % 3),
                        out_by="perf",
                    )
                )
            db.execute(
                text(
                    "UPDATE items SET status = 'on_loan' WHERE id IN (SELECT item_id FROM loans WHERE returned_at IS NULL)"
                )
            )
            db.commit()
        record(
            "Dashboard numbers and charts (3,000 open loans, 20,000 items)",
            repeat(lambda: c.get("/api/reports/charts", headers=lib), 10),
        )
        record(
            "A report over 20,000 items (tagged items)",
            repeat(lambda: c.get("/api/reports/tagged_items", headers=lib), 10),
        )
        record(
            "A report over 3,000 loans (circulation)",
            repeat(lambda: c.get("/api/reports/circulation", headers=lib), 10),
        )
        record(
            "Audit log page (25 events, with total)",
            repeat(
                lambda: c.get(
                    "/api/audit/events?limit=25",
                    headers={
                        "Authorization": "Bearer "
                        + c.post("/api/auth/card-login", json={"uid": "STAFFAUDIT01"}).json()["token"]
                    },
                ),
                10,
            ),
        )

        # --- reminders for a day's worth of due loans
        providers = build_providers(NotificationConfig())
        out, t = timed(lambda: runner.run_once(app.state.session_factory, providers, NotificationConfig()))
        record(
            "Reminder pass: scan 3,000 loans, queue and deliver",
            f"{t:.1f} s",
            f"{out['reminders']['queued']:,} messages queued, {out['delivery']['sent']:,} sent",
        )
        out, t = timed(lambda: runner.run_once(app.state.session_factory, providers, NotificationConfig()))
        record("Reminder pass again (nothing new to send)", f"{t:.2f} s", f"{out['reminders']['queued']} queued")

        # --- many people at once, over real HTTP (a real server on a spare port, ten clients)
        import socket

        import httpx
        import uvicorn

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
        threading.Thread(target=server.run, daemon=True).start()
        while not server.started:
            time.sleep(0.05)
        token = lib["Authorization"]

        def worker(n: int) -> list[float]:
            samples = []
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", headers={"Authorization": token}, timeout=30
            ) as client:
                for k in range(25):
                    t = time.perf_counter()
                    if k % 2:
                        r = client.get("/api/catalogue/items", params={"q": titles[(n + k) % 8], "limit": 15})
                    else:
                        r = client.get(f"/api/circulation/members/P{(n * 25 + k) % 600:04d}")
                    assert r.status_code == 200, r.text[:100]
                    samples.append((time.perf_counter() - t) * 1000)
            return samples

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=10) as pool:
            samples = [x for batch in pool.map(worker, range(10)) for x in batch]
        wall = time.perf_counter() - t0
        server.should_exit = True
        record(
            "10 desks at once over HTTP, 25 requests each (searches and member lookups)",
            f"{len(samples) / wall:.0f} requests/s",
            f"median / 95th percentile {percentiles(samples)}",
        )

        # --- backup, proof and restore of the full database
        size = Path(backup.sqlite_file(settings.database_url)).stat().st_size / 2**20
        info, t = timed(lambda: backup.create_backup(settings.database_url, settings.backup_dir, "perf"))
        record(f"Back up the database ({size:.0f} MB)", f"{t:.1f} s")
        _, t = timed(lambda: backup.verify_backup(info.path))
        record("Verify the backup (integrity, checksum, schema)", f"{t:.1f} s")
        _, t = timed(lambda: backup.compare(backup.sqlite_file(settings.database_url), info.path))
        record("Compare live data with the backup, table by table", f"{t:.1f} s")
        _, t = timed(
            lambda: c.post(f"/api/backups/{info.path.name}/restore", headers=admin, json={"confirm": "RESTORE"})
        )
        record("Restore it (safety copy, copy back, re-prove every table)", f"{t:.1f} s")
    print("\n| Step | Result | Note |\n|---|---|---|")
    for step, result, note in rows_out:
        print(f"| {step} | {result} | {note} |")
    print(f"\n(threads: {threading.active_count()}; working folder {tmp})")


def _no_limit() -> bool:
    from aisys import opac_routes

    opac_routes._hits.clear()
    opac_routes.LIMIT_PER_MINUTE = 10**6  # the measurement is not a visitor
    return True


if __name__ == "__main__":
    main()
