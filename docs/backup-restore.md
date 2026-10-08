# Backup and restore (AC10, NFR01)

A backup is a complete, verified copy of all library data. You can **compare** the live data with any backup, table by
table, to prove nothing has changed, and **restore** a backup if something goes wrong. A restore first saves the current
data as a safety copy, so a restore can itself be undone.

## What makes a backup trustworthy

- Made with SQLite's online backup API, so it is safe while the app is running. It is one self-contained file (no `-wal`
  or `-shm` beside it) with a small `.json` manifest: label, time, schema revision, checksum, and the row count of every table.
- **Verified when made** (SQLite integrity check) and **again before any restore** (integrity, checksum against the
  manifest, and that the schema is one this version knows). A damaged, altered or newer-version backup is refused, and
  nothing is touched.
- Taken automatically before every spreadsheet import (`pre-migration-batchN-...`) and before every restore (`pre-restore-...`).

## Proof that data is intact: the fingerprint

For every table, the row count and a SHA-256 checksum over its rows in a fixed order. Comparing the live database with a
backup lists each table as **Identical**, **Different**, or only in one of them. `sessions` and `audit_events` grow
just by using the app, so they are shown as "changed with use" and are never counted as damage. The `migration_batches`
and `migration_rows` tables are the import log: after a failed import they correctly say "failed", so the screen reports
"library data identical, only the import log differs".

## The restore procedure

**From the page** (administrators, **Backups**):
1. **Compare** the suspect backup with the live data to see exactly what differs.
2. **Restore…** asks you to type the word `RESTORE`.
3. The system saves a safety copy (`pre-restore-...`), copies the backup over the live database, runs an integrity check, and compares **every** table with the backup. If anything does not match, it puts the safety copy back and says so.
4. The page shows the proof table. People signed in may have to sign in again (a session made after the backup is not in it).

**From the command line** (stop the application first):

```
python -m aisys.cli backup create --label before-update
python -m aisys.cli backup list
python -m aisys.cli backup verify <file>
python -m aisys.cli backup compare <file>
python -m aisys.cli backup restore <file> --yes
```

Without `--yes`, `restore` only explains what it would do. A `<file>` is a name in the backup folder or a path.

**While a restore runs** (a few seconds), everyone else gets a clear "being restored, try again shortly" message (HTTP 503
with `Retry-After`). Without this, a change made by someone else in the middle would be lost or half-applied. It also
means two restores cannot run at once. The mode is always switched off again, even if the restore is refused or fails.

**If the database is busy** (another process is writing), the restore waits up to 20 seconds and then gives up with
"nothing was changed" instead of hanging; a short lock is simply waited out. Run it again, or stop the application first.

A backup made by an **older version** is restored and then brought up to the current schema automatically (the proof is
taken before the upgrade, so it shows the data is the backup's data). A backup from a **newer** version is refused.

## Housekeeping rules

- **Plain file names only.** Backups are named with letters, digits, dots, dashes and underscores. A `.db` file with
  spaces or special characters in the folder is not offered (nor accepted by the API); the page names such files so you can
  rename them. Labels you type are turned into plain ASCII names.
- **A damaged or odd manifest never breaks the list.** A manifest that is missing, unreadable or full of wrong values is
  treated as "no manifest"; a checksum in it that does not match the file is reported, never trusted.
- **The list is paged** (newest first, 15 on the page) because a folder can hold hundreds of files.
- Keep tests and real backups apart: the automated tests use a temporary folder, never `data/backups`.

## The drills (development only, 404 in production)

The Backups page has a **Disaster drill** so a reviewer can see the whole thing without risking real data:

- **Full drill (damaging change):** takes a backup, then imitates a faulty sync that wipes balances and blocks, blanks half the shelf locations, deletes every fifth tag link and closes every loan. It shows which tables differ, restores the backup, and shows every table identical again.
- **Failed spreadsheet import:** *Make the next import fail* arms a fault; then commit any import on **Data migration**. It fails after inserting its rows, rolls back, and the backup it took stays. **Compare** that backup to see that all library data is unchanged. (This uses the real commit code.)

## Design decisions

| Decision | Why | Cost |
|---|---|---|
| Restore through SQLite's backup API, in place, from the page or CLI | Works while the app runs and keeps the app's database file; no copying files by hand | A restore briefly locks the database; sessions newer than the backup are lost |
| A safety copy before every restore, and a re-check afterwards that reverts on any mismatch | A restore must never make things worse, and must be undoable | One more backup file per restore |
| Whole-database fingerprint as the proof | "Prove the original data remains intact" needs evidence per table, not a message | Reads every row (about 3 s for a 12 MB database) |
| Refuse damaged, altered or newer backups before touching anything | Restoring a bad file is worse than not restoring | A hand-edited backup must be re-made |
| Typed `RESTORE` confirmation, audit-logged | Restore replaces all current data | An extra step |
| Maintenance mode during a restore (503 for everyone else) | No lost or half-applied changes from people working at the same moment; faster, and no concurrent restores | The app is unavailable for a few seconds |
| A deadline when the database is busy | Python's own backup call would retry forever and hang the web request | A restore can ask you to try again |
| No automatic deletion of old backups | Deleting the wrong one is worse than using disk space | The folder grows; delete files by hand |
| Backup files and manifests are plain files in `[backup] dir` | Easy to copy to another machine or media | Keep that folder safe: it holds all the data |

## Known limits

- SQLite only (file databases). Other databases would need their own backup hook.
- Backups sit on the same machine unless you copy them off; copy them regularly.
- Restoring replaces everything, including the audit log and sessions up to the backup's moment (the restore itself is logged afterwards).
- The checks cover data, not the application files; reinstall or roll back the application separately (AC09).
