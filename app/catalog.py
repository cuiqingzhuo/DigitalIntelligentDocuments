import csv
import io
import re
import unicodedata
from datetime import UTC, datetime, timedelta

from pydantic import Field, ValidationError, field_validator
from sqlalchemy import select

from .routing import IDENTIFIER, Strict
from .storage import editions, holdings, now, row, rows

STATUS = ("available", "loaned", "unavailable", "unknown")


def normalize(value):
    return "".join(char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum())


class Edition(Strict):
    edition_id: str = Field(pattern=IDENTIFIER.pattern)
    title: str = Field(min_length=1, max_length=255)
    author: str = Field(default="", max_length=255)
    publisher: str = Field(default="", max_length=255)
    year: int | None = Field(default=None, ge=1000, le=2100)
    volume: str = Field(default="", max_length=255)
    isbn: str | None = None

    @field_validator("isbn", mode="before")
    @classmethod
    def isbn_check(cls, value):
        if not value:
            return None
        value = unicodedata.normalize("NFKC", str(value)).replace("-", "").replace(" ", "").upper()
        if re.fullmatch(r"[0-9]{13}", value) and sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(value)) % 10 == 0:
            return value
        if re.fullmatch(r"[0-9]{9}[0-9X]", value) and sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(value)) % 11 == 0:
            prefix = "978" + value[:9]
            checksum = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(prefix))
            return prefix + str(-checksum % 10)
        raise ValueError("ISBN 校验失败")


class Holding(Strict):
    holding_id: str = Field(pattern=IDENTIFIER.pattern)
    edition_id: str = Field(pattern=IDENTIFIER.pattern)
    barcode: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    call_number: str = Field(default="", max_length=100)
    status: str = Field(pattern=r"^(available|loaned|unavailable|unknown)$")
    synced_at: datetime

    @field_validator("synced_at")
    @classmethod
    def timestamp_check(cls, value):
        if value.tzinfo is None:
            raise ValueError("同步时间须含时区")
        return value.astimezone(UTC)


def library_rows(connection, table, library):
    return rows(connection.execute(select(table).where(table.c.library_id == library)))


def edition_holdings(connection, library, edition_id):
    return rows(connection.execute(select(holdings).where(
        holdings.c.library_id == library, holdings.c.edition_id == edition_id
    ).order_by(holdings.c.holding_id)))


def summarize(record, copies, stale_hours):
    times = [copy["synced_at"] for copy in copies]
    stale_cutoff = datetime.now(UTC) - timedelta(hours=stale_hours)
    stale = bool(times) and any(datetime.fromisoformat(item) < stale_cutoff for item in times)
    available = sum(copy["status"] == "available" for copy in copies)
    status = ("unknown" if not copies or all(copy["status"] == "unknown" for copy in copies)
              else "available" if available else "loaned" if all(copy["status"] == "loaned" for copy in copies)
              else "unavailable")
    return {**record, "available": available, "holding_count": len(copies), "status": status,
            "stale": stale, "synced_at": max(times, default=None)}


def search(connection, library, query, field, page, limit, stale_hours):
    lookup = normalize(query)
    copies = {}
    for copy in library_rows(connection, holdings, library):
        copies.setdefault(copy["edition_id"], []).append(copy)
    results = []
    for edition in library_rows(connection, editions, library):
        local_copies = copies.get(edition["edition_id"], [])
        searchable = {key: [edition[key] or ""] for key in ("title", "author", "isbn")}
        searchable["call_number"] = [copy["call_number"] for copy in local_copies]
        if lookup and field in ("all", "isbn") and re.fullmatch(r"[0-9]{9}[0-9x]", lookup):
            try:
                searchable["isbn"].append(lookup if Edition.isbn_check(lookup) == edition["isbn"] else "")
            except ValueError:
                pass
        values = [normalize(value) for key, items in searchable.items() if field in ("all", key) for value in items]
        if not lookup or any(lookup in value for value in values):
            rank = 0 if lookup in values else 1 if normalize(edition["title"]).startswith(lookup) else 2
            results.append((rank, edition["title"], edition["edition_id"], edition, local_copies))
    results.sort(key=lambda item: item[:3])
    return {"items": [summarize(item[3], item[4], stale_hours) for item in results[(page-1)*limit:page*limit]],
            "total": len(results), "page": page, "limit": limit}


def upsert(connection, table, library, record, identity):
    condition = (table.c.library_id == library) & (table.c[identity] == record[identity])
    existing = row(connection.execute(select(table).where(condition)))
    if existing == record:
        return "unchanged"
    if existing:
        if table is holdings and existing["synced_at"] > record["synced_at"]:
            return "older"
        connection.execute(table.update().where(condition).values(**record))
        return "updated"
    connection.execute(table.insert().values(**record))
    return "inserted"


def import_catalog(connection, library, csv_editions, csv_holdings, preview):
    report = {"inserted": 0, "updated": 0, "unchanged": 0, "older": 0, "errors": [], "preview": preview}
    transaction = connection.begin_nested()
    try:
        for name, content, model, table, identity in (
            ("editions", csv_editions, Edition, editions, "edition_id"),
            ("holdings", csv_holdings, Holding, holdings, "holding_id"),
        ):
            if not content.strip():
                continue
            reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")), strict=True)
            seen = set()
            records = {}
            barcodes = {}
            barcode_by_holding = {}
            if table is holdings:
                records = {record["edition_id"] for record in library_rows(connection, editions, library)}
                barcodes = {record["barcode"]: record["holding_id"] for record in library_rows(connection, holdings, library)}
                barcode_by_holding = {holding: code for code, holding in barcodes.items()}
            try:
                reader.fieldnames
                if not set(key for key, value in model.model_fields.items() if value.is_required()) <= set(reader.fieldnames or []):
                    report["errors"].append({"file": name, "line": 1, "error": "缺少必填列"})
                    continue
                for raw in reader:
                    if not any(raw.values()):
                        continue
                    try:
                        if None in raw:
                            raise ValueError("列数多于表头")
                        data = {key: value for key, value in raw.items() if value is not None and key in model.model_fields}
                        if model is Edition and not data.get("year"):
                            data["year"] = None
                        record = model.model_validate(data).model_dump()
                        if record[identity] in seen:
                            raise ValueError("批次中有重复编号")
                        if table is holdings:
                            record["synced_at"] = record["synced_at"].isoformat(timespec="microseconds")
                            if record["edition_id"] not in records:
                                raise ValueError("关联版本不存在")
                            if barcodes.get(record["barcode"], record[identity]) != record[identity]:
                                raise ValueError("条码已被其他复本使用")
                        seen.add(record[identity])
                        state = upsert(connection, table, library, {**record, "library_id": library}, identity)
                        if table is holdings and state != "older":
                            previous_barcode = barcode_by_holding.get(record[identity])
                            if previous_barcode and previous_barcode != record["barcode"]:
                                del barcodes[previous_barcode]
                            barcodes[record["barcode"]] = record[identity]
                            barcode_by_holding[record[identity]] = record["barcode"]
                        report[state] += 1
                    except (ValidationError, ValueError) as error:
                        message = "；".join(item["loc"][0] + " " + item["msg"] for item in error.errors()) if isinstance(error, ValidationError) else str(error)
                        report["errors"].append({"file": name, "line": reader.line_num, "error": message})
            except csv.Error as error:
                report["errors"].append({"file": name, "line": reader.line_num, "error": str(error)})
        if preview:
            transaction.rollback()
        else:
            transaction.commit()
    except BaseException:
        transaction.rollback()
        raise
    return report
