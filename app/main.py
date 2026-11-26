import asyncio
import base64
import hashlib
import json
import os
import re
import time
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
from uuid import uuid4

import cv2
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError, model_validator
from sqlalchemy import select

from . import catalog, demo, recognition, routing, storage, vision
from .routing import Strict
from .security import Actor, authorize, principals
from .storage import get_map, now, row, rows

DEMO = os.getenv("DIW_DEMO", "1") == "1"
LIBRARY = os.getenv("DIW_PUBLIC_LIBRARY", demo.LIBRARY)
FLOOR = os.getenv("DIW_PUBLIC_FLOOR", demo.FLOOR)
STALE_HOURS = max(1, int(os.getenv("DIW_STALE_HOURS", "72")))
RETENTION_DAYS = min(30, max(0, int(os.getenv("DIW_PHOTO_RETENTION_DAYS", "0"))))
CLOUD = os.getenv("DIW_CLOUD_REVIEW", "1") == "1"
STORE = storage.Store()
PHOTOS = STORE.runtime / "photos"
STATIC = storage.ROOT / "web"
actors = principals(DEMO)
staff = authorize(("admin", "librarian"))
admin = authorize(("admin",))
verifier = authorize(("verifier",))
employee = authorize(("admin", "librarian", "verifier"))


def purge_photos():
    if not PHOTOS.exists():
        return
    with STORE.write() as connection:
        expired = rows(connection.execute(select(storage.recognitions.c.recognition_id).where(
            storage.recognitions.c.photo_expires_at <= now())))
        for record in expired:
            (PHOTOS / (record["recognition_id"] + ".jpg")).unlink(missing_ok=True)
        for item in PHOTOS.glob("*.jpg"):
            if time.time() - item.stat().st_mtime > 86400:
                known = connection.execute(select(storage.recognitions.c.recognition_id).where(
                    storage.recognitions.c.recognition_id == item.stem)).first()
                if not known:
                    item.unlink(missing_ok=True)


async def clean_periodically():
    while True:
        await asyncio.to_thread(purge_photos)
        await asyncio.sleep(3600)


@asynccontextmanager
async def startup(_):
    if not DEMO and not actors:
        raise RuntimeError("DIW_TOKENS_JSON is required when demo mode is disabled")
    STORE.initialize()
    if DEMO:
        with STORE.write() as connection:
            demo.seed(connection)
    cleaner = asyncio.create_task(clean_periodically())
    try:
        yield
    finally:
        cleaner.cancel()
        with suppress(asyncio.CancelledError):
            await cleaner


app = FastAPI(title="数智文枢", version="1.0.0", lifespan=startup)


class BodyTooLarge(Exception):
    pass


class BodyLimit:
    def __init__(self, app, size):
        self.app, self.size = app, size

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        received = 0

        async def limited():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.size:
                raise BodyTooLarge
            return message

        await self.app(scope, limited, send)


app.add_middleware(BodyLimit, size=14 * 1024 * 1024)


@app.exception_handler(BodyTooLarge)
async def too_large(_, __):
    return JSONResponse({"detail": "请求文件过大"}, status_code=413)


@app.exception_handler(RequestValidationError)
async def invalid_input(_, error):
    safe = [{key: item[key] for key in ("type", "loc", "msg")} for item in error.errors()]
    return JSONResponse({"detail": safe}, status_code=422)


def fail(status, detail):
    raise HTTPException(status, detail)


def scoped(connection, table, library, **filters):
    return row(connection.execute(select(table).where(table.c.library_id == library).filter_by(**filters)))


def current_model(connection, library, version=None):
    query = select(storage.models.c.document).where(storage.models.c.library_id == library)
    if version:
        query = query.where(storage.models.c.version == version)
    value = connection.execute(query.order_by(storage.models.c.published_at.desc())).scalar()
    return value or recognition.BASELINE


def audit(connection, actor, action, detail):
    connection.execute(storage.audit.insert().values(event_id=uuid4().hex, library_id=actor.library,
                       actor=actor.actor, action=action, detail=detail, created_at=now()))


def session(connection, session_id, library=LIBRARY):
    record = scoped(connection, storage.sessions, library, session_id=session_id)
    if not record:
        fail(404, "查找会话不存在")
    return record


def idempotency(key, payload):
    if not key or not re.fullmatch(r"[A-Za-z0-9._:-]{8,100}", key):
        fail(400, "需要有效的 Idempotency-Key 请求头")
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def replay(connection, scope, key, digest):
    record = row(connection.execute(select(storage.events).filter_by(scope=scope, request_key=key)))
    if record and record["digest"] != digest:
        fail(409, "请求键已用于不同内容")
    return record["result"] if record else None


def remember(connection, scope, key, digest, result):
    connection.execute(storage.events.insert().values(scope=scope, request_key=key, digest=digest, result=result))
    return result


@app.get("/health")
def health():
    with STORE.read() as connection:
        connection.execute(select(1))
    return {"status": "ok"}


@app.get("/config")
def config():
    return {"library": LIBRARY, "floor": FLOOR, "demo": DEMO, "cloud_review": CLOUD,
            "photo_retention_days": RETENTION_DAYS, "stale_hours": STALE_HOURS}


@app.get("/editions")
def editions(q: str = Query("", max_length=120), field: str = Query("all", pattern=r"^(all|title|author|isbn|call_number)$"),
             page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=200)):
    with STORE.read() as connection:
        return catalog.search(connection, LIBRARY, q, field, page, limit, STALE_HOURS)


@app.get("/editions/{edition_id}/holdings")
def holdings(edition_id: str):
    with STORE.read() as connection:
        record = scoped(connection, storage.editions, LIBRARY, edition_id=edition_id)
        if not record:
            fail(404, "版本不存在")
        copies = catalog.edition_holdings(connection, LIBRARY, edition_id)
        return {"edition": catalog.summarize(record, copies, STALE_HOURS),
                "items": [{k: v for k, v in item.items() if k != "barcode"} for item in copies]}


@app.get("/cache/pilot")
def pilot_cache():
    from collections import defaultdict
    with STORE.read() as connection:
        book_rows = catalog.library_rows(connection, storage.editions, LIBRARY)
        copies = defaultdict(list)
        for item in catalog.library_rows(connection, storage.holdings, LIBRARY):
            copies[item["edition_id"]].append({key: value for key, value in item.items() if key != "barcode"})
    book_rows.sort(key=lambda item: (not copies.get(item["edition_id"]), item["edition_id"]))
    books = [catalog.summarize(item, copies.get(item["edition_id"], []), STALE_HOURS) for item in book_rows[:2000]]
    return {"books": books, "copies": {item["edition_id"]: copies.get(item["edition_id"], []) for item in books},
            "updated_at": now(), "complete": len(book_rows) <= 2000}


def published(connection, library, floor):
    record = get_map(connection, library, floor)
    if not record:
        return {"library_id": library, "floor": floor, "revision": 0, "updated_at": None,
                "document": {"name": floor, "service_label": "楼层入口服务台", "nodes": [], "edges": [],
                             "faces": [], "blocks": [], "image": None}}
    return {key: value for key, value in record.items() if key not in ("actor", "slot")}


@app.get("/maps/{floor}")
def get_floor(floor: str, start: str = "", face_id: str = ""):
    with STORE.read() as connection:
        record = published(connection, LIBRARY, floor)
    document = record["document"]
    start = start or next((node["id"] for node in document["nodes"] if node["kind"] == "entrance"), "")
    path = routing.route(document, start, face_id) if face_id else []
    return {**record, "start": start, "face_id": face_id, "route": path, "reachable": bool(path)}


def face_data(connection, library, floor, face_id):
    record = published(connection, library, floor)
    face = next((item for item in record["document"]["faces"] if item["id"] == face_id), None)
    if face is None and record["revision"] == 0 and routing.IDENTIFIER.fullmatch(face_id):
        return {"id": face_id, "label": face_id, "shelf": face_id.rsplit("-", 1)[0],
                "side": face_id[-1] if face_id.endswith(("-A", "-B")) else None, "direction": None,
                "marker_status": "unknown", "library_id": library, "floor": floor,
                "map_revision": 0, "service_label": record["document"]["service_label"]}
    if not face or face["marker_status"] == "retired":
        fail(404, "找不到该架面，请前往服务台")
    return {**face, "library_id": library, "floor": floor, "map_revision": record["revision"],
            "service_label": record["document"]["service_label"]}


@app.get("/shelf-faces/{face_id}")
def shelf_face(face_id: str, floor: str = FLOOR, expected: str = ""):
    try:
        normalized = routing.marker(face_id, LIBRARY, floor)
    except ValueError as error:
        fail(400, str(error))
    with STORE.read() as connection:
        result = face_data(connection, LIBRARY, floor, normalized)
    return {**result, "matches_selected": not expected or expected == normalized}


@app.get("/shelf/{library}/{floor}/{face_id}", include_in_schema=False)
def shelf_link(library: str, floor: str, face_id: str):
    if library != LIBRARY:
        fail(404, "此架标属于其他馆")
    shelf_face(face_id, floor)
    return FileResponse(STATIC / "index.html")


class SessionInput(Strict):
    edition_id: str = Field(pattern=routing.IDENTIFIER.pattern)
    holding_id: str | None = Field(default=None, pattern=routing.IDENTIFIER.pattern)
    floor: str = Field(default=FLOOR, pattern=routing.IDENTIFIER.pattern)
    face_id: str = Field(pattern=routing.IDENTIFIER.pattern)
    scanned_face_id: str = Field(min_length=1, max_length=500)


@app.post("/sessions")
def create_session(body: SessionInput, key: str | None = Header(None, alias="Idempotency-Key")):
    scope = f"{LIBRARY}:session"
    digest = idempotency(key, body.model_dump())
    with STORE.write() as connection:
        prior = replay(connection, scope, key, digest)
        if prior:
            return prior
        if not scoped(connection, storage.editions, LIBRARY, edition_id=body.edition_id):
            fail(404, "所选书目版本不存在")
        if body.holding_id:
            holding = scoped(connection, storage.holdings, LIBRARY, holding_id=body.holding_id)
            if not holding or holding["edition_id"] != body.edition_id or holding["status"] != "available":
                fail(422, "所选复本当前不可借或与版本不匹配")
        try:
            actual = routing.marker(body.scanned_face_id, LIBRARY, body.floor)
        except ValueError as error:
            fail(400, str(error))
        face_data(connection, LIBRARY, body.floor, body.face_id)
        face = face_data(connection, LIBRARY, body.floor, actual)
        if body.face_id != actual:
            fail(409, f"实际位置为 {actual}，请选择改用此架面或返回路线")
        model = current_model(connection, LIBRARY)
        record = {**body.model_dump(), "session_id": uuid4().hex, "library_id": LIBRARY,
                  "scanned_face_id": actual, "created_at": now(), "model_version": model["version"]}
        connection.execute(storage.sessions.insert().values(**record))
        return remember(connection, scope, key, digest, {**record, "face": face})


class RecognitionInput(Strict):
    capture_scope: str = Field(pattern=r"^(left_visible|partial)$")
    upload_consent: bool = False
    image_base64: str | None = Field(default=None, max_length=11_184_812)
    demo_sample: str | None = Field(default=None, pattern=r"^(target|wrong|absent)$")
    observations: list[recognition.Observation] | None = Field(default=None, max_length=80)
    device_display: recognition.DeviceDisplay | None = None
    quality: list[str] = Field(default_factory=list, max_length=8)
    device_model: str | None = Field(default=None, max_length=80)
    device_ms: int | None = Field(default=None, ge=0, le=300_000)

    @model_validator(mode="after")
    def source(self):
        if sum(value is not None for value in (self.image_base64, self.demo_sample, self.observations)) != 1:
            raise ValueError("请选择一种识别输入")
        if any(len(flag) > 160 for flag in self.quality):
            raise ValueError("画质说明过长")
        if self.device_display and self.observations is None:
            raise ValueError("本机复核记录需要书脊观察结果")
        return self


@app.post("/sessions/{session_id}/recognitions")
def submit_recognition(session_id: str, body: RecognitionInput,
                       key: str | None = Header(None, alias="Idempotency-Key")):
    payload = body.model_dump(mode="json")
    if body.image_base64:
        payload["image_base64"] = hashlib.sha256(body.image_base64.encode()).hexdigest()
    scope, digest = f"recognition:{session_id}", idempotency(key, payload)
    started = time.perf_counter()
    with STORE.read() as connection:
        record = session(connection, session_id)
        prior = replay(connection, scope, key, digest)
        if prior:
            return prior
        model = current_model(connection, record["library_id"], record["model_version"])
        all_books = catalog.library_rows(connection, storage.editions, record["library_id"])
        all_copies = rows(connection.execute(select(storage.holdings.c.edition_id, storage.holdings.c.call_number)
                         .where(storage.holdings.c.library_id == record["library_id"])))
        face = face_data(connection, record["library_id"], record["floor"], record["scanned_face_id"])
    image, expires, purpose = None, None, None
    source = "device_ocr"
    quality = body.quality
    if body.image_base64 is not None or body.demo_sample:
        if body.image_base64 is not None and not body.upload_consent:
            fail(403, "须单独同意上传此照片")
        if body.image_base64 is not None and not CLOUD:
            fail(503, "本馆未启用云复核，请使用端侧识别")
        if body.demo_sample and not DEMO:
            fail(404, "演示样本不可用")
        source = "demo_image" if body.demo_sample else "cloud"
        try:
            if body.demo_sample:
                from PIL import Image
                with Image.open(STATIC / "samples" / f"{body.demo_sample}.jpg") as sample:
                    image = sample.convert("RGB")
            else:
                image = vision.decode_image(body.image_base64)
                purpose = "recognition_review"
            detector = model.get("detector")
            if detector:
                detector = str(storage.ROOT / "models" / detector)
                vision.check_checkpoint(detector, model["detector_sha256"])
            observations, quality = vision.analyze(image, detector)
        except ValueError as error:
            fail(422, str(error))
    else:
        observations = [item.model_dump() for item in body.observations]
    if body.device_display:
        source = "android_local"
        try:
            result = recognition.recorded_device_result(
                body.device_display, observations, all_books, record["edition_id"],
                record["holding_id"], body.capture_scope, quality,
            )
        except ValueError as error:
            fail(422, str(error))
    else:
        result = recognition.recognize(
            observations, all_books, all_copies, record["edition_id"], record["holding_id"],
            body.capture_scope, quality, model,
        )
    recognition_id = uuid4().hex
    if purpose and RETENTION_DAYS:
        expires = (datetime.now(UTC) + timedelta(days=RETENTION_DAYS)).isoformat(timespec="microseconds")
    result.update(recognition_id=recognition_id, session_id=session_id, source=source,
                  face_direction=face["direction"], upload_consent=body.upload_consent,
                  duration_ms=round((time.perf_counter() - started) * 1000), device_ms=body.device_ms,
                  device_model=body.device_model, photo_expires_at=expires,
                  photo_purpose=purpose, created_at=now())
    path = None
    try:
        with STORE.write() as connection:
            prior = replay(connection, scope, key, digest)
            if prior:
                return prior
            if expires:
                PHOTOS.mkdir(exist_ok=True)
                path = PHOTOS / (recognition_id + ".jpg")
                path.write_bytes(vision.jpg_bytes(image))
            connection.execute(storage.recognitions.insert().values(
                recognition_id=recognition_id, session_id=session_id, document=result,
                created_at=result["created_at"], photo_purpose=purpose, photo_expires_at=expires,
            ))
            return remember(connection, scope, key, digest, result)
    except BaseException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise


def verification_states(connection, session_id, source):
    data = rows(connection.execute(select(storage.verifications).filter_by(session_id=session_id, source=source)))
    return {stage: next((item["document"] for item in data if item["stage"] == stage), None)
            for stage in ("first", "final")}


def save_verification(connection, session_id, source, result):
    states = verification_states(connection, session_id, source)
    created = now()
    connection.execute(storage.attempts.insert().values(
        attempt_id=uuid4().hex, session_id=session_id, source=source, document=result, created_at=created,
    ))
    if states["first"] is None:
        connection.execute(storage.verifications.insert().values(
            session_id=session_id, source=source, stage="first", document=result, created_at=created))
        states["first"] = result
    if states["final"] is None and result["outcome"] == "correct":
        connection.execute(storage.verifications.insert().values(
            session_id=session_id, source=source, stage="final", document=result, created_at=created))
        states["final"] = result
    return {**states, "current": result}


def book_difference(connection, record, holding):
    target = scoped(connection, storage.editions, record["library_id"], edition_id=record["edition_id"])
    actual = scoped(connection, storage.editions, record["library_id"], edition_id=holding["edition_id"]) if holding else None
    visible = ("edition_id", "title", "publisher", "year", "volume", "isbn")
    return {"expected": {key: target[key] for key in visible},
            "actual": {key: actual[key] for key in visible} if actual else None}


class ReadVerification(Strict):
    barcode: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


@app.post("/sessions/{session_id}/verifications")
def verify_reader(session_id: str, body: ReadVerification,
                  key: str | None = Header(None, alias="Idempotency-Key")):
    scope, digest = f"reader-verify:{session_id}", idempotency(key, body.model_dump())
    with STORE.write() as connection:
        record = session(connection, session_id)
        prior = replay(connection, scope, key, digest)
        if prior:
            return prior
        if body.barcode is None:
            result = {"outcome": "pending", "evidence": "none", "holding_id": None, "at": now()}
        else:
            holding = scoped(connection, storage.holdings, LIBRARY, barcode=body.barcode)
            correct = (holding and record["edition_id"] == holding["edition_id"]
                       and (not record["holding_id"] or record["holding_id"] == holding["holding_id"]))
            result = {"outcome": "correct" if correct else "wrong" if holding else "pending",
                      "evidence": "reader_barcode", "holding_id": holding["holding_id"] if holding else None,
                      "barcode": body.barcode, "at": now(), **book_difference(connection, record, holding)}
        states = save_verification(connection, session_id, "reader", result)
        return remember(connection, scope, key, digest, states)


class Feedback(Strict):
    session_id: str
    reason: str = Field(pattern=r"^(found|retake|wrong_edition|marker_damage|suspected_misplaced|unrecognized)$")
    note: str = Field(default="", max_length=2000)


@app.post("/feedback")
def feedback(body: Feedback, key: str | None = Header(None, alias="Idempotency-Key")):
    scope, digest = f"feedback:{body.session_id}", idempotency(key, body.model_dump())
    with STORE.write() as connection:
        record = session(connection, body.session_id)
        prior = replay(connection, scope, key, digest)
        if prior:
            return prior
        result = {"feedback_id": uuid4().hex, "reason": body.reason, "status": "self_reported", "workorder_id": None}
        if body.reason != "found":
            existing = scoped(connection, storage.workorders, LIBRARY, session_id=body.session_id, reason=body.reason)
            if not existing:
                stamp = now()
                existing = dict(workorder_id=uuid4().hex, library_id=LIBRARY, session_id=body.session_id,
                                face_id=record["scanned_face_id"], reason=body.reason, status="open", finding="",
                                assignee="", note=body.note, receipt="", created_at=stamp, updated_at=stamp, revision=1)
                connection.execute(storage.workorders.insert().values(**existing))
            result.update(workorder_id=existing["workorder_id"], status=existing["status"])
        return remember(connection, scope, key, digest, result)


@app.get("/admin/me")
def admin_me(actor: Actor = Depends(employee)):
    return {"actor": actor.actor, "role": actor.role, "library": actor.library}


class CatalogImport(Strict):
    editions_csv: str = Field(default="", max_length=6_000_000)
    holdings_csv: str = Field(default="", max_length=6_000_000)
    preview: bool = True


@app.post("/admin/imports")
def imports(body: CatalogImport, actor: Actor = Depends(staff)):
    with STORE.write() as connection:
        result = catalog.import_catalog(connection, actor.library, body.editions_csv, body.holdings_csv, body.preview)
        if not body.preview:
            audit(connection, actor, "catalog_import", {**result, "errors": len(result["errors"])})
    return result


class MapDraft(Strict):
    floor: str = Field(pattern=routing.IDENTIFIER.pattern)
    revision: int = Field(ge=0)
    document: routing.FloorMap


class MapPublish(Strict):
    floor: str = Field(pattern=routing.IDENTIFIER.pattern)
    revision: int = Field(ge=1)


@app.get("/admin/maps/{floor}/draft")
def map_draft(floor: str, actor: Actor = Depends(staff)):
    with STORE.read() as connection:
        draft = get_map(connection, actor.library, floor, "draft")
        public = published(connection, actor.library, floor)
    return {"draft": draft, "published": public}


@app.put("/admin/maps/draft")
def save_draft(body: MapDraft, actor: Actor = Depends(staff)):
    document = body.document.model_dump()
    if body.document.image:
        try:
            image = vision.decode_image(body.document.image, max_size=3_000_000)
            document["image"] = base64.b64encode(vision.jpg_bytes(image)).decode("ascii")
        except ValueError as error:
            fail(422, str(error))
    if any(face["id"] != face["id"].upper() for face in document["faces"]):
        fail(422, "架面编号须大写")
    with STORE.write() as connection:
        existing = get_map(connection, actor.library, body.floor, "draft")
        public = get_map(connection, actor.library, body.floor)
        revision = max((item["revision"] for item in (existing, public) if item), default=0)
        if revision != body.revision:
            fail(409, "草稿或地图已变更，请刷新")
        value = dict(library_id=actor.library, floor=body.floor, slot="draft", revision=revision+1,
                     document=document, actor=actor.actor, updated_at=now())
        if existing:
            connection.execute(storage.maps.update().where(
                storage.maps.c.library_id == actor.library, storage.maps.c.floor == body.floor,
                storage.maps.c.slot == "draft").values(**value))
        else:
            connection.execute(storage.maps.insert().values(**value))
        audit(connection, actor, "map_draft", {"floor": body.floor, "revision": value["revision"]})
    return value


@app.post("/admin/maps/publish")
def publish(body: MapPublish, actor: Actor = Depends(admin)):
    with STORE.write() as connection:
        draft = get_map(connection, actor.library, body.floor, "draft")
        if not draft or draft["revision"] != body.revision:
            fail(409, "草稿已变更，请刷新预览")
        value = {**draft, "slot": "published", "actor": actor.actor, "updated_at": now()}
        existing = get_map(connection, actor.library, body.floor)
        if existing:
            connection.execute(storage.maps.update().where(
                storage.maps.c.library_id == actor.library, storage.maps.c.floor == body.floor,
                storage.maps.c.slot == "published").values(**value))
        else:
            connection.execute(storage.maps.insert().values(**value))
        connection.execute(storage.map_history.insert().values(**{k: v for k, v in value.items() if k != "slot"}))
        connection.execute(storage.maps.delete().where(storage.maps.c.library_id == actor.library,
                           storage.maps.c.floor == body.floor, storage.maps.c.slot == "draft"))
        audit(connection, actor, "map_publish", {"floor": body.floor, "revision": value["revision"]})
        return value


@app.get("/admin/shelf-faces/{floor}/{face_id}/qr")
def shelf_qr(floor: str, face_id: str, actor: Actor = Depends(staff)):
    with STORE.read() as connection:
        record = get_map(connection, actor.library, floor)
        if not record:
            fail(404, "请先发布地图")
        face = face_data(connection, actor.library, floor, face_id)
    if face["marker_status"] != "active":
        fail(409, "仅可生成有效架标的二维码")
    payload = f"diw://shelf/{actor.library}/{floor}/{face_id}"
    image = cv2.QRCodeEncoder_create().encode(payload)
    image = cv2.copyMakeBorder(image, 3, 3, 3, 3, cv2.BORDER_CONSTANT, value=255)
    image = cv2.resize(image, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    success, data = cv2.imencode(".png", image)
    if not success:
        fail(500, "无法生成架标")
    return Response(data.tobytes(), media_type="image/png")


class ModelInput(Strict):
    version: str = Field(pattern=routing.IDENTIFIER.pattern)
    matcher: str = Field(pattern=r"^(baseline|trained)$")
    unique_threshold: float = Field(ge=.5, le=1)
    margin: float = Field(gt=0, le=1)
    candidate_threshold: float = Field(gt=0, le=1)
    detector: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+\.pt$")


@app.get("/models/current")
def get_model():
    with STORE.read() as connection:
        value = current_model(connection, LIBRARY)
    return {k: v for k, v in value.items() if k not in ("detector", "rank_weights", "detector_sha256")}


@app.get("/admin/models/current")
def get_staff_model(actor: Actor = Depends(admin)):
    with STORE.read() as connection:
        value = current_model(connection, actor.library)
    return {k: v for k, v in value.items() if k not in ("detector", "rank_weights", "detector_sha256")}


@app.post("/admin/models/publish")
def publish_model(body: ModelInput, actor: Actor = Depends(admin)):
    if body.candidate_threshold > body.unique_threshold:
        fail(422, "候选阈值不能高于唯一阈值")
    document = body.model_dump()
    if body.matcher == "trained":
        weights = recognition.rank_weights()
        if not weights:
            fail(422, "缺少已训练的重排模型文件")
        document["rank_weights"] = weights
    if body.detector:
        file = storage.ROOT / "models" / body.detector
        if not file.is_file():
            fail(422, "缺少检测模型文件")
        if find_spec("ultralytics") is None:
            fail(422, "此部署尚未安装旋转框模型运行依赖")
        with file.open("rb") as stream:
            document["detector_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    with STORE.write() as connection:
        if scoped(connection, storage.models, actor.library, version=body.version):
            fail(409, "模型版本已存在")
        connection.execute(storage.models.insert().values(
            library_id=actor.library, version=body.version, document=document, actor=actor.actor, published_at=now()))
        audit(connection, actor, "model_publish", {"version": body.version})
    return {"version": body.version}


@app.get("/admin/workorders")
def list_orders(face_id: str = "", status: str = "", actor: Actor = Depends(staff)):
    with STORE.read() as connection:
        query = select(storage.workorders).where(storage.workorders.c.library_id == actor.library)
        if face_id:
            query = query.filter_by(face_id=face_id)
        if status:
            query = query.filter_by(status=status)
        return {"items": rows(connection.execute(query.order_by(storage.workorders.c.created_at.desc()).limit(200)))}


class OrderUpdate(Strict):
    revision: int = Field(ge=1)
    status: str = Field(pattern=r"^(assigned|investigating|closed)$")
    finding: str = Field(default="", pattern=r"^(|on_shelf|misplaced|marker_damaged|unresolved)$")
    receipt: str = Field(default="", max_length=2000)


@app.patch("/admin/workorders/{workorder_id}")
def update_order(workorder_id: str, body: OrderUpdate, actor: Actor = Depends(staff)):
    with STORE.write() as connection:
        existing = scoped(connection, storage.workorders, actor.library, workorder_id=workorder_id)
        if not existing:
            fail(404, "工单不存在")
        if existing["revision"] != body.revision:
            fail(409, "工单已变更，请刷新列表")
        if existing["assignee"] and existing["assignee"] != actor.actor and actor.role != "admin":
            fail(403, "工单已由其他馆员接单")
        transitions = {"open": ("assigned",), "assigned": ("investigating", "closed"),
                       "investigating": ("investigating", "closed"), "closed": ()}
        if body.status not in transitions[existing["status"]]:
            fail(409, "工单不能进行此状态变更")
        if body.status == "closed" and (not body.finding or not body.receipt):
            fail(422, "关闭前须填写核查结论和回执")
        updated = {**existing, **body.model_dump(), "assignee": existing["assignee"] or actor.actor,
                   "updated_at": now(), "revision": body.revision+1}
        connection.execute(storage.workorders.update().where(
            storage.workorders.c.workorder_id == workorder_id).values(**updated))
        audit(connection, actor, "workorder_update", {"id": workorder_id, "status": body.status, "finding": body.finding})
    return updated


@app.get("/workorders/{workorder_id}")
def workorder_receipt(workorder_id: str, session_id: str):
    with STORE.read() as connection:
        session(connection, session_id)
        record = scoped(connection, storage.workorders, LIBRARY, workorder_id=workorder_id, session_id=session_id)
        if not record:
            fail(404, "工单不存在")
    return {key: record[key] for key in ("workorder_id", "face_id", "status", "finding", "receipt", "updated_at")}


from .trial import trial_router

app.include_router(trial_router)


@app.get("/", include_in_schema=False)
def reader():
    return FileResponse(STATIC / "index.html")


@app.get("/workbench", include_in_schema=False)
def workbench():
    return FileResponse(STATIC / "workbench.html")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/assets/samples/{name}.jpg", include_in_schema=False)
def sample_image(name: str):
    if not DEMO or name not in ("target", "wrong", "absent"):
        fail(404, "演示样本不可用")
    return FileResponse(STATIC / "samples" / f"{name}.jpg")


app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
