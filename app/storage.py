import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    JSON, CheckConstraint, Column, ForeignKeyConstraint, Integer, MetaData,
    String, Table, UniqueConstraint, create_engine, event, select,
)

ROOT = Path(__file__).resolve().parents[1]
metadata = MetaData()


def table(name, *columns):
    return Table(name, metadata, *columns)


def key(name):
    return Column(name, String(100), primary_key=True)


def field(name, size=255, **options):
    return Column(name, String(size), nullable=False, **options)


editions = table(
    "editions", key("library_id"), key("edition_id"), field("title"), field("author"),
    field("publisher"), Column("year", Integer), field("volume"), Column("isbn", String(13)),
)
holdings = table(
    "holdings", key("library_id"), key("holding_id"), field("edition_id", 100),
    field("barcode", 100), field("call_number", 100), field("status", 20), field("synced_at", 35),
    ForeignKeyConstraint(["library_id", "edition_id"], ["editions.library_id", "editions.edition_id"]),
    UniqueConstraint("library_id", "barcode"),
)
maps = table(
    "maps", key("library_id"), key("floor"), key("slot"),
    Column("revision", Integer, nullable=False), Column("document", JSON, nullable=False),
    field("actor", 100), field("updated_at", 35),
)
map_history = table(
    "map_history", key("library_id"), key("floor"), Column("revision", Integer, primary_key=True),
    Column("document", JSON, nullable=False), field("actor", 100), field("updated_at", 35),
)
models = table(
    "models", key("library_id"), key("version"), Column("document", JSON, nullable=False),
    field("actor", 100), field("published_at", 35),
)
sessions = table(
    "sessions", key("session_id"), field("library_id", 100), field("edition_id", 100),
    Column("holding_id", String(100)), field("floor", 100), field("face_id", 100),
    field("scanned_face_id", 100), field("created_at", 35), field("model_version", 100),
    ForeignKeyConstraint(["library_id", "edition_id"], ["editions.library_id", "editions.edition_id"]),
    ForeignKeyConstraint(["library_id", "holding_id"], ["holdings.library_id", "holdings.holding_id"]),
)
recognitions = table(
    "recognitions", key("recognition_id"), field("session_id", 100),
    Column("document", JSON, nullable=False), field("created_at", 35),
    Column("photo_expires_at", String(35)), Column("photo_purpose", String(50)),
    ForeignKeyConstraint(["session_id"], ["sessions.session_id"]),
)
events = table("request_events", key("scope"), key("request_key"), field("digest", 64),
               Column("result", JSON, nullable=False))
verifications = table(
    "verifications", key("session_id"), key("source"), key("stage"),
    Column("document", JSON, nullable=False), field("created_at", 35),
    ForeignKeyConstraint(["session_id"], ["sessions.session_id"]),
    CheckConstraint("stage IN ('first', 'final')"),
    CheckConstraint("source IN ('reader', 'independent')"),
)
attempts = table(
    "verification_attempts", key("attempt_id"), field("session_id", 100), field("source", 20),
    Column("document", JSON, nullable=False), field("created_at", 35),
    ForeignKeyConstraint(["session_id"], ["sessions.session_id"]),
)
workorders = table(
    "workorders", key("workorder_id"), field("library_id", 100), field("session_id", 100),
    field("face_id", 100), field("reason", 30), field("status", 20), field("finding", 40),
    field("assignee", 100), field("note", 2000), field("receipt", 2000), field("updated_at", 35),
    field("created_at", 35), Column("revision", Integer, nullable=False),
    UniqueConstraint("session_id", "reason"),
    ForeignKeyConstraint(["session_id"], ["sessions.session_id"]),
)
audit = table(
    "audit", key("event_id"), field("library_id", 100), field("actor", 100),
    field("action", 40), Column("detail", JSON, nullable=False), field("created_at", 35),
)
answers = table(
    "trial_answers", key("library_id"), key("task_id"), Column("document", JSON, nullable=False),
)
trials = table(
    "trial_results", key("library_id"), key("task_id"), Column("document", JSON, nullable=False),
)


def now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def row(result):
    value = result.mappings().first()
    return dict(value) if value is not None else None


def rows(result):
    return [dict(value) for value in result.mappings()]


class Store:
    def __init__(self, url=None):
        self.runtime = Path(os.getenv("DIW_RUNTIME", str(ROOT / "runtime")))
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.url = url or os.getenv("DIW_DATABASE_URL", f"sqlite:///{(self.runtime / 'library.db').as_posix()}")
        self.engine = create_engine(self.url, pool_pre_ping=True)
        self.sqlite = self.engine.dialect.name == "sqlite"
        if self.sqlite:
            event.listen(self.engine, "connect", self.configure_sqlite)

    @staticmethod
    def configure_sqlite(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")

    @contextmanager
    def read(self):
        with self.engine.connect() as connection:
            yield connection

    @contextmanager
    def write(self):
        with self.engine.begin() as connection:
            if self.sqlite:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                # Serialize pilot writes, including the read preceding an idempotent insert.
                connection.exec_driver_sql("SELECT pg_advisory_xact_lock(76136429)")
            yield connection

    def initialize(self):
        with self.write() as connection:
            metadata.create_all(connection)


def get_map(connection, library, floor, slot="published"):
    query = select(maps).where(maps.c.library_id == library, maps.c.floor == floor, maps.c.slot == slot)
    return row(connection.execute(query))
