# Data migration (FR10, AC01) - design, rules and evidence

## What it does
Imports a spreadsheet (`.xlsx` or `.csv`, ~20,000 rows tested) of library holdings into the catalogue **without
modifying existing records**. One spreadsheet row = one physical copy (item); bibliographic data is shared between copies.

```
upload -> STAGE (every row kept verbatim) -> MAP columns -> VALIDATE (dry run, library untouched)
       -> COMMIT (verified backup, one all-or-nothing transaction, insert-only) -> RECONCILE -> [ROLLBACK]
```

| State | Meaning |
|---|---|
| `staged` | Rows stored; required columns not yet mapped |
| `validated` | Every row classified; safe to review/commit; library still untouched |
| `committed` | Valid rows inserted; reconciliation report stored |
| `rolled_back` | Rows created by this batch removed |
| `failed` | Commit aborted and rolled back by the database; backup kept |

## Technical decisions (and why)
| Decision | Why | Trade-off |
|---|---|---|
| **Staging table** holds raw + normalised rows | Satisfies "import into staging, reject invalid rows"; lets users review before anything is written; keeps evidence for reports | Extra storage (~20k JSON rows) |
| **Insert-only commit**, never update/delete existing rows | NFR01: cannot corrupt existing records; duplicates are *skipped and reported*, not merged | Re-importing a corrected row needs a new accession no. or a manual fix |
| **Backup before commit** (SQLite online backup, integrity-checked) | NFR01/AC10: a restorable snapshot exists for every import | SQLite only; other DBs need their own backup hook |
| **Single transaction** for all inserts | A failure leaves the library exactly as it was (tested by injecting a failure after the inserts) | One long write lock (about 7 s for 20k rows) |
| **Fingerprint of pre-existing records** hashed before and after | Turns "existing data unchanged" from a claim into a check | Hashes key fields, not every column |
| **Rollback by `batch_id`** on items and bib records | Removes exactly what the batch created; refuses (409) if items are already referenced elsewhere (FK) | Bib records still used by later batches are kept and reported |
| **Mapping-driven columns** with alias + fuzzy matching, user-overridable | The reviewers' file layout is unknown (assumption A-01) | Fuzzy cutoff is deliberately strict; borderline headers need a manual choice |
| **Warnings vs errors** | Legacy data is messy: drop a bad year but keep the book; reject only what would corrupt the catalogue | Policy choices are listed below and can be tightened |
| **Row numbers = spreadsheet rows** (header = row 1, blanks counted) | Error reports must point at the line a person sees in Excel | CSV rows with embedded newlines are counted per record |

## Duplicate policy
First valid occurrence wins. A row is a **duplicate** (skipped, status `duplicate`) when its **accession number** or **barcode**
(case-insensitive) already exists in the file (earlier valid row) *or* in the library. Several copies sharing an ISBN are *not*
duplicates: they become extra items on one bibliographic record. Records without an ISBN are grouped by normalised
title + author + year.

## Validation rules (data dictionary)
| Field | Rule | Failure |
|---|---|---|
| accession_no | required; 1-32 of letters, digits, `. _ / -` | error `required_missing` / `accession_format` |
| title | required; <= 500 chars | error |
| barcode | optional; 1-64 of letters, digits, `. _ -` | error `barcode_format` |
| isbn | optional; ISBN-10/13 with valid check digit (hyphens, `ISBN:` labels ignored) | error `isbn_invalid` |
| price | optional; currency symbols/thousand separators tolerated; must be >= 0 | error `price_invalid` |
| acquired_on | optional; ISO or day-first dates (`dd/mm/yyyy`...), not in the future | error `date_invalid` |
| author, publisher, edition, language, call_number, location | optional; length limits (300/300/100/50/100/100) | error `too_long` |
| pub_year | optional; 1000..next year; a 4-digit year is extracted from text | **warning** `year_invalid` (value dropped) |
| item_type | book, reference, serial, media, other (aliases accepted); empty = book | **warning** `item_type_unknown` (stored as `other`) |
| status | available, lost, withdrawn, damaged (aliases accepted); empty = available | **warning** `status_unknown` |
| status "issued/on loan" | loans are not migrated (no loan history in the source) | **warning** `loan_not_migrated` (set to available) |
| any text | control characters removed, whitespace collapsed | n/a |

An item of type *reference* is flagged `is_reference` (used later to block circulation, AC04).

## API (permission `migration.run`)
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/migrations` | multipart `file` (+ optional `mapping` JSON); stages and validates |
| GET | `/api/migrations`, `/api/migrations/{id}` | list / detail (summary, mapping, reconciliation) |
| GET | `/api/migrations/{id}/rows?status=&limit=&offset=` | inspect staged rows and per-row issues |
| GET | `/api/migrations/{id}/errors.csv` | downloadable issue report (formula-injection safe) |
| PUT | `/api/migrations/{id}/mapping` | change mapping and re-validate |
| POST | `/api/migrations/{id}/validate` \| `commit` \| `rollback` | lifecycle actions |

## Security notes
Permission-gated and audited (upload, mapping, validate, commit, rollback). File type allow-list, 30 MB / 200,000-row limits.
Cells are inserted into the UI as text only (never HTML). Exported CSV cells starting with `= + - @` are prefixed with `'`.
Uploaded files are parsed in memory and never written to disk or executed.

## Stale results (library changed after a file was checked)
A batch is checked against the library as it is *at that moment*. If the library changes afterwards (another batch is committed
or rolled back), the saved result would be wrong, so the system stamps each check with a fingerprint of the accession numbers,
barcodes and bibliographic keys it compared against and **re-checks automatically** when the fingerprint differs:

* Opening a batch, listing its rows or downloading its report re-checks it first; the page shows a one-off notice with the old and
  new numbers (for example duplicates 193 -> 2). The history marks such batches **Needs re-check**.
* **Commit never uses unreviewed numbers:** if the library changed, commit re-checks and refuses with `409` so the refreshed counts
  can be read first. The database's unique constraints are a second line of defence (a clash aborts the whole transaction).
* Every automatic re-check is audited (`migration.revalidate`, with before/after counts).

## Evidence
* Synthetic data generator: `python -m aisys.devtools.sample_data --rows 20000 --out samples` (deterministic; the manifest lists the
  exact number of deliberate defects, and tests assert the importer finds exactly those).
* 20,000-row `.xlsx` on the development laptop: **upload + validate 13.1 s, commit 7.4 s** (`docs/performance-results.md`), reconciliation balanced,
  rollback returns to the pre-migration count (`tests/test_migration.py::test_full_20000_row_xlsx_import_reconcile_and_rollback`).
* Atomicity proven by failure injection; existing records proven unchanged by fingerprint; backup verified readable and holding
  the pre-migration state.

## Sample reports
Committed examples of the two reports, made from `samples/library_200.xlsx` by `python scripts/sample_reports.py`:
`docs/samples/validation-report.csv` (the problem list), `docs/samples/reconciliation-report.json` (the proof after commit) and
`docs/samples/README.md` (every column and field explained). `python scripts/sample_reports.py --check` says whether they are current.

## Known limitations
* Only the first worksheet of an `.xlsx` is read. Dates stored as text are parsed day-first (dd/mm vs mm/dd is ambiguous).
* Restoring a backup is guided and proven (table by table): see docs/backup-restore.md.
* Loans/fines/members are out of scope for this importer (holdings only).
