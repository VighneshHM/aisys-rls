"""Migration pipeline: stage -> validate (dry run) -> commit -> reconcile -> rollback.

Safety properties (SOP NFR01, AC01, AC10):
  * The library tables are only ever INSERTed into; existing rows are never updated or deleted by a commit.
  * Rows are validated in a staging table first; nothing touches the library until `commit_batch`.
  * Commit takes a verified database backup, then inserts everything in ONE transaction (all or nothing).
  * A fingerprint of all pre-existing records is compared before/after to prove they are unchanged.
  * Rollback removes exactly the rows created by the batch.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, exists, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aisys import audit, backup
from aisys.config import Settings
from aisys.migration import mapping as mapping_mod
from aisys.migration import parsing, validation
from aisys.models import BibRecord, Item, MigrationBatch, MigrationRow

CHUNK = 1000


class MigrationError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _count(db: Session, model, *where) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*where)) or 0


def _chunks(seq: list, n: int = CHUNK) -> Iterator[list]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# ------------------------------------------------------------------ stage
def stage_upload(
    db: Session, *, filename: str, data: bytes, actor: str, mapping: dict[str, str] | None = None, ip: str = ""
) -> MigrationBatch:
    try:
        headers, rows = parsing.read_table(filename, data)
    except parsing.ParseError as exc:
        audit.record(
            db,
            actor=actor,
            action="migration.upload",
            outcome="failure",
            detail={"filename": filename, "reason": str(exc)},
            ip=ip,
        )
        db.commit()
        raise MigrationError(422, str(exc)) from exc

    suggested, unmapped = mapping_mod.suggest_mapping(headers)
    chosen = mapping if mapping is not None else suggested
    batch = MigrationBatch(
        filename=filename[:255],
        file_sha256=hashlib.sha256(data).hexdigest(),
        status="staged",
        mapping=chosen,
        headers=headers,
        created_by=actor,
        summary={"source_rows": len(rows), "suggested_mapping": suggested, "unmapped_headers": unmapped},
    )
    db.add(batch)
    db.flush()
    for part in _chunks(
        [
            {
                "batch_id": batch.id,
                "row_no": i,
                "raw": r,
                "mapped": {},
                "status": "staged",
                "errors": [],
                "duplicate_of": "",
            }
            for i, r in rows
        ]
    ):
        db.execute(insert(MigrationRow), part)
    audit.record(
        db,
        actor=actor,
        action="migration.upload",
        entity="migration_batch",
        entity_id=batch.id,
        detail={"filename": batch.filename, "rows": len(rows), "sha256": batch.file_sha256},
        ip=ip,
    )
    db.commit()
    validate_batch(db, batch, actor=actor, ip=ip)
    return batch


def get_batch(db: Session, batch_id: int) -> MigrationBatch:
    batch = db.get(MigrationBatch, batch_id)
    if batch is None:
        raise MigrationError(404, "Migration batch not found")
    return batch


# ------------------------------------------------------------------ validate (dry run)
def set_mapping(db: Session, batch: MigrationBatch, new_mapping: dict[str, str], *, actor: str, ip: str = "") -> None:
    if batch.status not in ("staged", "validated"):
        raise MigrationError(409, f"Mapping cannot be changed once a batch is {batch.status}")
    batch.mapping = {k: v for k, v in new_mapping.items() if v}
    audit.record(
        db,
        actor=actor,
        action="migration.mapping",
        entity="migration_batch",
        entity_id=batch.id,
        detail={"mapping": batch.mapping},
        ip=ip,
    )
    db.commit()
    validate_batch(db, batch, actor=actor, ip=ip)


def validate_batch(db: Session, batch: MigrationBatch, *, actor: str, ip: str = "") -> dict:
    """Dry run: classify every staged row. Writes only to the staging table, never to the library."""
    if batch.status not in ("staged", "validated"):
        raise MigrationError(409, f"Batch is {batch.status}; it can no longer be validated")

    problems = mapping_mod.check_mapping(batch.mapping, batch.headers)
    base = {k: v for k, v in batch.summary.items() if k in ("source_rows", "suggested_mapping", "unmapped_headers")}
    if problems:
        batch.status = "staged"
        batch.summary = {**base, "mapping_problems": problems}
        db.commit()
        return batch.summary

    taken_acc = {a.casefold() for a in db.scalars(select(Item.accession_no))}
    taken_bc = {b.casefold() for b in db.scalars(select(Item.barcode).where(Item.barcode.is_not(None)))}
    existing_keys = set(db.scalars(select(BibRecord.dedupe_key)))
    seen_acc: dict[str, int] = {}
    seen_bc: dict[str, int] = {}
    new_keys: set[str] = set()
    reused_keys: set[str] = set()

    status_counts: Counter[str] = Counter()
    error_codes: Counter[str] = Counter()
    warning_codes: Counter[str] = Counter()
    rows_with_warnings = 0

    for row in db.scalars(select(MigrationRow).where(MigrationRow.batch_id == batch.id).order_by(MigrationRow.row_no)):
        values = {f: row.raw.get(h, "") for f, h in batch.mapping.items()}
        res = validation.validate_row(values)
        issues = [i.as_dict() for i in res.issues]
        row.mapped, row.duplicate_of = res.clean, ""

        if not res.ok:
            row.status = "invalid"
        else:
            acc, bc = res.clean["accession_no"].casefold(), (res.clean["barcode"] or "").casefold()
            dup = ""
            if acc in seen_acc:
                dup, code = f"row {seen_acc[acc]} in this file", "duplicate_accession"
            elif acc in taken_acc:
                dup, code = "existing library item", "duplicate_accession"
            elif bc and bc in seen_bc:
                dup, code = f"row {seen_bc[bc]} in this file", "duplicate_barcode"
            elif bc and bc in taken_bc:
                dup, code = "existing library item", "duplicate_barcode"
            if dup:
                row.status, row.duplicate_of = "duplicate", dup
                issues.append(
                    {
                        "field": code.split("_")[1],
                        "code": code,
                        "severity": "error",
                        "message": f"Duplicate of {dup}; row skipped",
                    }
                )
            else:
                row.status = "valid"
                seen_acc[acc] = row.row_no
                if bc:
                    seen_bc[bc] = row.row_no
                (reused_keys if res.clean["dedupe_key"] in existing_keys else new_keys).add(res.clean["dedupe_key"])
        row.errors = issues
        status_counts[row.status] += 1
        for i in issues:
            if i["severity"] == "error":
                error_codes[i["code"]] += 1
            elif row.status == "valid":  # a warning on a skipped row is moot: nothing is imported
                warning_codes[i["code"]] += 1
        rows_with_warnings += any(i["severity"] == "warning" for i in issues) and row.status == "valid"

    batch.status = "validated"
    batch.summary = {
        **base,
        "valid": status_counts["valid"],
        "invalid": status_counts["invalid"],
        "duplicate": status_counts["duplicate"],
        "valid_with_warnings": rows_with_warnings,
        "error_codes": dict(error_codes),
        "warning_codes": dict(warning_codes),
        "bibs_to_create": len(new_keys),
        "bibs_reused": len(reused_keys),
        "library_items_before": _count(db, Item),
        "library_bibs_before": _count(db, BibRecord),
    }
    audit.record(
        db,
        actor=actor,
        action="migration.validate",
        entity="migration_batch",
        entity_id=batch.id,
        detail={k: batch.summary[k] for k in ("valid", "invalid", "duplicate")},
        ip=ip,
    )
    db.commit()
    return batch.summary


# ------------------------------------------------------------------ commit
def _fingerprint(db: Session, exclude_batch_id: int) -> str:
    """Hash of every library record NOT created by this batch (proves existing data is untouched)."""
    h = hashlib.sha256()
    not_mine_item = or_(Item.batch_id.is_(None), Item.batch_id != exclude_batch_id)
    for r in db.execute(
        select(
            Item.id,
            Item.bib_id,
            Item.accession_no,
            Item.barcode,
            Item.status,
            Item.location,
            Item.call_number,
            Item.item_type,
            Item.price,
            Item.acquired_on,
        )
        .where(not_mine_item)
        .order_by(Item.id)
    ):
        h.update(repr(tuple(r)).encode())
    not_mine_bib = or_(BibRecord.batch_id.is_(None), BibRecord.batch_id != exclude_batch_id)
    for r in db.execute(
        select(
            BibRecord.id,
            BibRecord.dedupe_key,
            BibRecord.title,
            BibRecord.author,
            BibRecord.isbn,
            BibRecord.pub_year,
            BibRecord.publisher,
        )
        .where(not_mine_bib)
        .order_by(BibRecord.id)
    ):
        h.update(repr(tuple(r)).encode())
    return h.hexdigest()


def commit_batch(db: Session, settings: Settings, batch: MigrationBatch, *, actor: str, ip: str = "") -> dict:
    if batch.status != "validated":
        raise MigrationError(409, f"Only a validated batch can be committed (this one is {batch.status})")
    expected_valid = batch.summary.get("valid", 0)
    if expected_valid == 0:
        raise MigrationError(422, "There are no valid rows to migrate")

    # The library may have changed since the dry run (another batch, manual edits): re-check first.
    fresh = validate_batch(db, batch, actor=actor, ip=ip)
    if fresh.get("valid") != expected_valid:
        raise MigrationError(409, "Library data changed since validation; review the refreshed summary and retry")

    try:
        info = backup.create_backup(settings.database_url, settings.backup_dir, f"pre-migration-batch{batch.id}")
    except backup.BackupError as exc:
        audit.record(
            db,
            actor=actor,
            action="migration.commit",
            outcome="failure",
            entity="migration_batch",
            entity_id=batch.id,
            detail={"reason": f"backup: {exc}"},
            ip=ip,
        )
        db.commit()
        raise MigrationError(500, f"Backup failed, migration not started: {exc}") from exc

    before_print = _fingerprint(db, batch.id)
    items_before, bibs_before = _count(db, Item), _count(db, BibRecord)
    batch_id = batch.id
    try:
        rows = list(
            db.scalars(
                select(MigrationRow)
                .where(MigrationRow.batch_id == batch_id, MigrationRow.status == "valid")
                .order_by(MigrationRow.row_no)
            )
        )
        key_to_bib: dict[str, int] = {}
        keys = {r.mapped["dedupe_key"] for r in rows}
        for part in _chunks(sorted(keys), 500):
            key_to_bib.update(
                dict(db.execute(select(BibRecord.dedupe_key, BibRecord.id).where(BibRecord.dedupe_key.in_(part))).all())
            )
        now = datetime.now(UTC)
        new_bibs, seen = [], set(key_to_bib)
        for r in rows:
            m = r.mapped
            if m["dedupe_key"] in seen:
                continue
            seen.add(m["dedupe_key"])
            new_bibs.append(
                {
                    "dedupe_key": m["dedupe_key"],
                    "title": m["title"],
                    "author": m["author"],
                    "isbn": m["isbn"],
                    "publisher": m["publisher"],
                    "pub_year": m["pub_year"],
                    "edition": m["edition"],
                    "language": m["language"],
                    "batch_id": batch_id,
                    "created_at": now,
                }
            )
        for part in _chunks(new_bibs):
            res = db.execute(insert(BibRecord).returning(BibRecord.id, BibRecord.dedupe_key), part)
            key_to_bib.update({k: i for i, k in res.all()})

        for part in _chunks(
            [
                {
                    "bib_id": key_to_bib[r.mapped["dedupe_key"]],
                    "accession_no": r.mapped["accession_no"],
                    "barcode": r.mapped["barcode"],
                    "call_number": r.mapped["call_number"],
                    "location": r.mapped["location"],
                    "item_type": r.mapped["item_type"],
                    "status": r.mapped["status"],
                    "is_reference": r.mapped["is_reference"],
                    "price": Decimal(r.mapped["price"]) if r.mapped["price"] else None,
                    "acquired_on": date.fromisoformat(r.mapped["acquired_on"]) if r.mapped["acquired_on"] else None,
                    "batch_id": batch_id,
                    "created_at": now,
                }
                for r in rows
            ]
        ):
            db.execute(insert(Item), part)
        db.execute(
            update(MigrationRow)
            .where(MigrationRow.batch_id == batch_id, MigrationRow.status == "valid")
            .values(status="migrated")
        )

        summary = dict(batch.summary)
        summary["reconciliation"] = _reconcile(db, summary, batch_id, items_before, bibs_before, before_print)
        batch.summary, batch.status = summary, "committed"
        batch.committed_at, batch.backup_path = now, str(info.path)
        audit.record(
            db,
            actor=actor,
            action="migration.commit",
            entity="migration_batch",
            entity_id=batch_id,
            detail={
                "migrated": summary["reconciliation"]["migrated"],
                "backup": info.path.name,
                "balanced": summary["reconciliation"]["balanced"],
            },
            ip=ip,
        )
        db.commit()
    except Exception as exc:
        db.rollback()  # nothing from this batch reaches the library
        failed = db.get(MigrationBatch, batch_id)
        failed.status = "failed"
        failed.summary = {
            **failed.summary,
            "failure": f"{type(exc).__name__}: {exc}"[:500],
            "backup_path": str(info.path),
        }
        audit.record(
            db,
            actor=actor,
            action="migration.commit",
            outcome="failure",
            entity="migration_batch",
            entity_id=batch_id,
            detail={"reason": type(exc).__name__},
            ip=ip,
        )
        db.commit()
        raise MigrationError(500, "Migration failed and was rolled back; library data is unchanged") from exc
    return batch.summary["reconciliation"]


def _reconcile(
    db: Session, summary: dict, batch_id: int, items_before: int, bibs_before: int, before_print: str
) -> dict:
    migrated = _count(db, MigrationRow, MigrationRow.batch_id == batch_id, MigrationRow.status == "migrated")
    invalid, dup = summary["invalid"], summary["duplicate"]
    items_after, bibs_after = _count(db, Item), _count(db, BibRecord)
    items_from_batch = _count(db, Item, Item.batch_id == batch_id)
    bibs_from_batch = _count(db, BibRecord, BibRecord.batch_id == batch_id)
    rec = {
        "source_rows": summary["source_rows"],
        "migrated": migrated,
        "rejected_invalid": invalid,
        "skipped_duplicate": dup,
        "items_before": items_before,
        "items_after": items_after,
        "items_added": items_after - items_before,
        "bibs_before": bibs_before,
        "bibs_after": bibs_after,
        "bibs_created": bibs_from_batch,
        "existing_records_unchanged": _fingerprint(db, batch_id) == before_print,
    }
    rec["balanced"] = (
        rec["source_rows"] == migrated + invalid + dup
        and rec["items_added"] == migrated == items_from_batch
        and bibs_after - bibs_before == bibs_from_batch
        and rec["existing_records_unchanged"]
    )
    return rec


# ------------------------------------------------------------------ rollback
def rollback_batch(db: Session, batch: MigrationBatch, *, actor: str, ip: str = "") -> dict:
    if batch.status != "committed":
        raise MigrationError(409, f"Only a committed batch can be rolled back (this one is {batch.status})")
    batch_id = batch.id
    rec = batch.summary["reconciliation"]
    try:
        removed_items = db.execute(delete(Item).where(Item.batch_id == batch_id)).rowcount
        still_used = exists().where(Item.bib_id == BibRecord.id)
        removed_bibs = db.execute(delete(BibRecord).where(BibRecord.batch_id == batch_id, ~still_used)).rowcount
        retained = _count(db, BibRecord, BibRecord.batch_id == batch_id)
        db.execute(
            update(MigrationRow)
            .where(MigrationRow.batch_id == batch_id, MigrationRow.status == "migrated")
            .values(status="reverted")
        )
        result = {
            "items_removed": removed_items,
            "bibs_removed": removed_bibs,
            "bibs_retained_in_use": retained,
            "items_now": _count(db, Item),
            "restored_to_pre_migration_counts": _count(db, Item) == rec["items_before"],
        }
        batch.summary = {**batch.summary, "rollback": result}
        batch.status, batch.rolled_back_at = "rolled_back", datetime.now(UTC)
        audit.record(
            db,
            actor=actor,
            action="migration.rollback",
            entity="migration_batch",
            entity_id=batch_id,
            detail=result,
            ip=ip,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        audit.record(
            db,
            actor=actor,
            action="migration.rollback",
            outcome="failure",
            entity="migration_batch",
            entity_id=batch_id,
            detail={"reason": "items in use"},
            ip=ip,
        )
        db.commit()
        raise MigrationError(
            409, "Rollback refused: items from this batch are already in use (for example tagged or on loan)"
        ) from exc
    return result


# ------------------------------------------------------------------ reports
def _csv_safe(value: object) -> str:
    """Neutralise spreadsheet formula injection in exported cells."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def errors_csv(db: Session, batch: MigrationBatch) -> Iterator[str]:
    headers = batch.headers
    buf = io.StringIO()
    writer = csv.writer(buf)

    def flush() -> str:
        data = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return data

    writer.writerow(["row_no", "row_status", "severity", "field", "code", "message", "duplicate_of", *headers])
    yield flush()
    q = (
        select(MigrationRow)
        .where(MigrationRow.batch_id == batch.id)
        .order_by(MigrationRow.row_no)
        .execution_options(yield_per=500)
    )
    for row in db.scalars(q):
        for issue in row.errors:
            writer.writerow(
                [
                    row.row_no,
                    row.status,
                    issue["severity"],
                    issue["field"],
                    issue["code"],
                    _csv_safe(issue["message"]),
                    row.duplicate_of,
                    *[_csv_safe(row.raw.get(h, "")) for h in headers],
                ]
            )
        yield flush()
