"""Deterministic SYNTHETIC library spreadsheet with a known number of deliberate defects.

No real titles, people or identifiers: titles are assembled from word lists, authors from invented
names, ISBNs are computed with valid check digits. The same seed always yields the same file, and
the returned manifest states exactly how many rows each rule should reject, so tests can assert it.

    python -m shelfsense.devtools.sample_data --rows 20000 --out samples
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from openpyxl import Workbook

HEADERS = ["Acc. No.", "Barcode", "Book Title", "Author(s)", "ISBN", "Publisher", "Year", "Edition", "Language",
           "Call No", "Location", "Type", "Status", "Price (INR)", "Date Acquired"]  # fmt: skip

ADJ = ["Silent", "Hidden", "Modern", "Ancient", "Digital", "Quantum", "Practical", "Applied", "Advanced",
       "Essential", "Elementary", "Comparative", "Illustrated", "Collected", "Complete", "Strategic", "Urban",
       "Coastal"]  # fmt: skip
NOUN = ["Algorithms", "Rivers", "Economics", "Chemistry", "Poetry", "Networks", "Mathematics", "Architecture",
        "Biology", "History", "Philosophy", "Mechanics", "Linguistics", "Ecology", "Statistics", "Astronomy",
        "Databases", "Geometry", "Sociology", "Medicine"]  # fmt: skip
FIRST = ["Asha", "Bruno", "Carla", "Dev", "Elena", "Farid", "Gita", "Hugo", "Isla", "Jon", "Kavya", "Liam", "Mira",
         "Noor", "Omar", "Priya", "Quinn", "Rohan", "Sara", "Tomas"]  # fmt: skip
LAST = ["Alder", "Bhatt", "Castell", "Dorsey", "Ebert", "Fontaine", "Gupta", "Hollis", "Iyer", "Jansen", "Kaur",
        "Lindqvist", "Mehta", "Novak", "Okafor", "Petrov", "Quist", "Rao", "Sato", "Thorne"]  # fmt: skip
PUBLISHERS = ["Northgate Press", "Lakeside Books", "Harbour & Finch", "Meridian Publishing", "Cedar House"]
LOCATIONS = ["Main Library", "Science Section", "Humanities Section", "Technology Section", "Reference Room"]
SAMPLE_DATE_START = date(2005, 1, 1)


def isbn13(rng: random.Random) -> str:
    body = "978" + "".join(str(rng.randint(0, 9)) for _ in range(9))
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(body))
    return body + str((10 - total % 10) % 10)


def build_rows(n: int = 20000, seed: int = 42) -> tuple[list[str], list[list[str]], dict]:
    rng = random.Random(seed)
    clean_head = min(1000, n // 5)  # first rows are guaranteed clean so duplicates can point at them
    pool_size = max(10, int(n * 0.7))
    bibs: list[dict[str, Any]] = []
    for i in range(pool_size):
        bibs.append({
            "title": f"{rng.choice(ADJ)} {rng.choice(NOUN)}, Volume {i + 1}",
            "author": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            "isbn": isbn13(rng) if rng.random() < 0.85 else "",
            "publisher": rng.choice(PUBLISHERS), "year": rng.randint(1975, 2024),
            "dewey": f"{rng.randint(1, 999):03d}.{rng.randint(1, 99)}",
        })  # fmt: skip

    rows: list[list[str]] = []
    for i in range(n):
        b = bibs[i % pool_size] if i < pool_size else rng.choice(bibs)  # every bib used; extras are 2nd copies
        kind = rng.choices(["Book", "Reference", "Serial"], weights=[93, 5, 2])[0]
        acquired = SAMPLE_DATE_START + timedelta(days=rng.randint(0, 7000))
        rows.append([
            f"ACC{i + 1:06d}", f"BC{100000 + i}", b["title"], b["author"], b["isbn"], b["publisher"], str(b["year"]),
            rng.choice(["", "2nd", "3rd"]), "English", f"{b['dewey']} {b['author'].split()[-1][:3].upper()}",
            "Reference Room" if kind == "Reference" else rng.choice(LOCATIONS[:4]), kind, "Available",
            f"{rng.randint(150, 4500)}.{rng.choice(['00', '50'])}", acquired.strftime("%d/%m/%Y"),
        ])  # fmt: skip

    free = list(range(clean_head, n))
    rng.shuffle(free)

    def take(k: int) -> list[int]:
        out, rest = free[:k], free[k:]
        free[:] = rest
        return out

    scale = n / 20000
    plan = {  # rule -> number of rows (scaled for small files, at least 1)
        "missing_title": 60, "missing_accession": 40, "bad_accession": 30, "bad_isbn": 120, "bad_price": 50,
        "bad_date": 40, "future_date": 10, "dup_accession": 100, "dup_barcode": 50,
        "unknown_type": 200, "bad_year": 80, "status_issued": 150,
    }  # fmt: skip
    counts = {k: max(1, round(v * scale)) for k, v in plan.items()}
    col = {h: i for i, h in enumerate(HEADERS)}
    future = (date.today() + timedelta(days=400)).strftime("%d/%m/%Y")

    for i in take(counts["missing_title"]):
        rows[i][col["Book Title"]] = ""
    for i in take(counts["missing_accession"]):
        rows[i][col["Acc. No."]] = ""
    for i in take(counts["bad_accession"]):
        rows[i][col["Acc. No."]] = f"ACC {i}#bad"
    for i in take(counts["bad_isbn"]):
        good = rows[i][col["ISBN"]] or "9780000000002"
        rows[i][col["ISBN"]] = good[:-1] + str((int(good[-1]) + 1) % 10)  # break the check digit
    for i in take(counts["bad_price"]):
        rows[i][col["Price (INR)"]] = rng.choice(["N/A", "free", "abc", "-50"])
    for i in take(counts["bad_date"]):
        rows[i][col["Date Acquired"]] = rng.choice(["31/02/2020", "2020-13-45", "yesterday", "00/00/0000"])
    for i in take(counts["future_date"]):
        rows[i][col["Date Acquired"]] = future
    for i in take(counts["dup_accession"]):
        rows[i][col["Acc. No."]] = rows[rng.randrange(clean_head)][col["Acc. No."]]
    for i in take(counts["dup_barcode"]):
        rows[i][col["Barcode"]] = rows[rng.randrange(clean_head)][col["Barcode"]]
    for i in take(counts["unknown_type"]):
        rows[i][col["Type"]] = "Novel"
    for i in take(counts["bad_year"]):
        rows[i][col["Year"]] = "unknown"
    for i in take(counts["status_issued"]):
        rows[i][col["Status"]] = "Issued"

    invalid = sum(counts[k] for k in ("missing_title", "missing_accession", "bad_accession", "bad_isbn", "bad_price",
                                      "bad_date", "future_date"))  # fmt: skip
    dup = counts["dup_accession"] + counts["dup_barcode"]
    manifest = {
        "rows": n, "seed": seed, "defects": counts, "expected_invalid": invalid, "expected_duplicate": dup,
        "expected_valid": n - invalid - dup,
        "expected_valid_with_warnings": counts["unknown_type"] + counts["bad_year"] + counts["status_issued"],
    }  # fmt: skip
    return HEADERS, rows, manifest


def write_xlsx(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Holdings")
    ws.append(headers)
    for r in rows:
        ws.append(r)
    wb.save(path)


def write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="samples")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    headers, rows, manifest = build_rows(args.rows, args.seed)
    stem = f"library_{args.rows}"
    write_xlsx(out / f"{stem}.xlsx", headers, rows)
    write_csv(out / f"{stem}.csv", headers, rows)
    (out / f"{stem}.manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"wrote {stem}.xlsx/.csv/.manifest.json in {out}/")
    print(json.dumps({k: manifest[k] for k in manifest if k.startswith("expected")}))


if __name__ == "__main__":
    main()
