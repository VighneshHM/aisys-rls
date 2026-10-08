# Performance results (D6, NFR05)

The SOP leaves performance targets to the candidate ("we will define and publish our own"). These are the targets, how they were
measured, and the results.

## Method

`python scripts/perf_report.py` builds a throw-away database with **20,000 synthetic holdings** (the same generator as the sample
spreadsheet, seed 42: 19,500 valid, 350 invalid, 150 duplicate rows), imports them through the real API, adds 600 members and
3,000 open loans, and times each step. Repeated steps report the **median and the 95th percentile of 10 to 40 requests**. The
concurrency step starts a real HTTP server on a spare port and drives it with ten clients. The numbers below are from the
development machine (Windows 11, Python 3.13, one process, SSD); a server will differ, so use them as a shape, not a promise.

## Targets and results

| Step | Target | Result | Verdict |
|---|---|---|---|
| Start the application (migrations and wiring) | under 5 s | 0.56 s | met |
| Import 20,000 rows: upload, validate, find duplicates | under 60 s | 13.1 s (19,500 valid, 350 invalid, 150 duplicate) | met |
| Commit the import (backup first, one transaction, reconcile) | under 60 s | 7.4 s (19,500 items, reconciliation balanced) | met |
| Catalogue search, one word | 95th percentile under 300 ms | 117 ms median, 124 ms p95 | met |
| Catalogue search, two words | under 300 ms | 118 / 124 ms | met |
| Catalogue search, an unfinished word (as typed) | under 300 ms | 119 / 131 ms | met |
| Catalogue search, a fragment inside a word (substring fallback) | under 300 ms | 121 / 138 ms | met |
| Public catalogue (OPAC) search | under 300 ms | 117 / 130 ms | met |
| Virtual bookshelf, first 300 books of the biggest location (4,704 books) | under 500 ms | 247 / 371 ms | met |
| Shelves list, every location with counts | under 200 ms | 25 / 26 ms | met |
| Dashboard numbers and charts (20,000 items, 3,000 loans) | under 200 ms | 10 / 11 ms | met |
| A report over 20,000 items; a report over 3,000 loans | under 200 ms | 11 / 12 ms; 9 / 11 ms | met |
| Audit log page (25 events with total) | under 200 ms | 15 / 17 ms | met |
| Reminder pass: scan 3,000 loans, queue and deliver 3,000 messages | under 60 s | 17.3 s | met |
| The same pass when nothing is new | under 5 s | 1.0 s | met |
| Ten clients at once over HTTP, 25 requests each (no pause between requests) | 95th percentile under 3 s | 18 requests/s; median 57 ms, p95 1.8 s | met (see note) |
| Back up the 34 MB database | under 30 s | 1.2 s | met |
| Verify the backup (integrity, checksum, schema) | under 30 s | 0.4 s | met |
| Compare live data with the backup, table by table | under 30 s | 1.2 s | met |
| Restore (safety copy, copy back, re-prove every table) | under 30 s | 3.5 s | met |

*Note on concurrency:* the ten clients send their next request the moment the last one is answered, which is far heavier than
ten people at desks. The server is one Python process, so searches (about 120 ms of work each) queue behind each other and the
slowest few wait close to two seconds; with a person's pause between requests the same ten desks need about ten requests a
second, well inside what one process answers.

## Other measured times

| Step | Result | Where measured |
|---|---|---|
| Offline install of the package (private environment from 23 wheels, database, start-up check) | about 53 s | `scripts/lifecycle_demo.py` |
| Offline update including backup, rehearsal on a copy, migration and start-up check | about 45 s | same |
| Rollback including the data restore and a start-up check | about 3.5 s | same |
| A restore while four clients are using the application | about 3 s; other users get a clean "please try again" for those seconds | `test_backup`, probe in `docs/security-checks.md` |

## What the measurements changed

The first run of this script found that **catalogue search took 1.3 to 3.3 seconds** at 20,000 items: the new full-text match ran
beside the older "contains" matching, which forced every row to be scanned for every search. The search now uses the full-text index
and falls back to substring matching only when the index finds nothing, which brought it to about 120 ms (`docs/defect-log.md`, D-028). The same script showed that a
single member with thousands of loans makes the member page slow; the script now spreads loans over many members as a library
does, and the page-size limits cap every list.

## Limits

One process on one machine; SQLite allows one writer at a time (reads do not block); the figures grow roughly with the number of
items for the substring fallback and with the number of matches for ranked full-text results. No soak test (hours of use) and no
test on a server-class machine has been done.
