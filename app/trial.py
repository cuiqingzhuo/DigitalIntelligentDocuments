import csv
import io
import json

from fastapi import APIRouter, Depends, Header, Response
from pydantic import Field, model_validator
from sqlalchemy import select

from . import storage
from .routing import IDENTIFIER, Strict
from .security import Actor, authorize
from .storage import now, row, rows

trial_router = APIRouter(prefix="/trial", tags=["试点核验"])
verifier = authorize(("verifier",))


class Answer(Strict):
    task_id: str = Field(pattern=IDENTIFIER.pattern)
    participant_code: str = Field(pattern=IDENTIFIER.pattern)
    method: str = Field(pattern=r"^(app|manual)$")
    matched_pair_id: str = Field(default="", max_length=80)
    target_edition_id: str = Field(pattern=IDENTIFIER.pattern)
    target_holding_id: str | None = Field(default=None, pattern=IDENTIFIER.pattern)
    allowed_holding_ids: list[str] = Field(min_length=1, max_length=100)
    presence: str = Field(pattern=r"^(visible|absent)$")
    difficulty: str = Field(min_length=1, max_length=100)
    target_quads: list[list[list[float]]] = Field(default_factory=list, max_length=80)
    device: str = Field(default="", max_length=100)
    lighting: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def check(self):
        if self.method == "manual" and self.presence != "visible":
            raise ValueError("人工对照须为在架任务")
        if self.target_holding_id is not None and self.allowed_holding_ids != [self.target_holding_id]:
            raise ValueError("指定复本任务只允许该复本")
        if self.presence == "absent" and self.target_quads:
            raise ValueError("负例不能包含目标标注框")
        if self.target_quads:
            from .recognition import Observation
            for quad in self.target_quads:
                Observation(quad=quad, text="")
        return self


class TrialEvent(Strict):
    session_id: str | None = Field(default=None, max_length=100)
    action: str = Field(pattern=r"^(start|milestone|take|finish)$")
    milestone: str | None = Field(default=None, pattern=r"^(search|arrival|capture|feedback)$")
    barcode: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    manual_edition_id: str | None = Field(default=None, pattern=IDENTIFIER.pattern)
    witnesses: list[str] = Field(default_factory=list, max_length=2)
    elapsed_s: float = Field(ge=0, le=3600)
    outcome: str = Field(default="finished", pattern=r"^(finished|timeout|abandoned|negative|unverified)$")

    @model_validator(mode="after")
    def action_fields(self):
        if (self.action == "milestone") != bool(self.milestone):
            raise ValueError("步骤记录须选择一个步骤")
        if self.action == "start" and self.elapsed_s != 0:
            raise ValueError("展示任务卡时记录 0 秒")
        if self.action != "take" and (self.barcode or self.manual_edition_id or self.witnesses):
            raise ValueError("核验证据只用于实际取书")
        return self


@trial_router.get("/tasks")
def tasks(actor: Actor = Depends(verifier)):
    from .main import STORE
    with STORE.read() as connection:
        documents = rows(connection.execute(select(storage.answers).filter_by(library_id=actor.library)))
        result = rows(connection.execute(select(storage.trials).filter_by(library_id=actor.library)))
    return {"items": [item["document"] for item in documents],
            "records": [item["document"] for item in result]}


@trial_router.post("/tasks")
def freeze_task(body: Answer, actor: Actor = Depends(verifier)):
    from .main import STORE, audit, current_model, fail, scoped
    from .catalog import library_rows
    import hashlib
    document = body.model_dump()
    with STORE.write() as connection:
        if scoped(connection, storage.answers, actor.library, task_id=body.task_id):
            fail(409, "答案已锁定，请创建新任务号")
        if not scoped(connection, storage.editions, actor.library, edition_id=body.target_edition_id):
            fail(422, "目标版本不存在")
        for item in body.allowed_holding_ids:
            copy = scoped(connection, storage.holdings, actor.library, holding_id=item)
            if not copy or copy["edition_id"] != body.target_edition_id:
                fail(422, f"复本 {item} 与目标版本不匹配")
        data = sorted(library_rows(connection, storage.editions, actor.library), key=lambda item: item["edition_id"])
        copies = sorted(library_rows(connection, storage.holdings, actor.library), key=lambda item: item["holding_id"])
        fingerprint = hashlib.sha256(json.dumps([data, copies], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        document.update(frozen_at=now(), model_version=current_model(connection, actor.library)["version"],
                        catalog_fingerprint=fingerprint)
        connection.execute(storage.answers.insert().values(library_id=actor.library, task_id=body.task_id,
                           document=document))
        audit(connection, actor, "trial_freeze", {"task_id": body.task_id})
    return document


@trial_router.post("/tasks/{task_id}/events")
def record_event(task_id: str, body: TrialEvent, actor: Actor = Depends(verifier),
                 key: str | None = Header(None, alias="Idempotency-Key")):
    from .main import STORE, audit, book_difference, fail, idempotency, remember, replay, save_verification, scoped, session
    from .catalog import library_rows
    import hashlib
    scope = f"trial:{actor.library}:{task_id}"
    digest = idempotency(key, body.model_dump())
    with STORE.write() as connection:
        prior = replay(connection, scope, key, digest)
        if prior:
            return prior
        answer = scoped(connection, storage.answers, actor.library, task_id=task_id)
        if not answer:
            fail(404, "任务答案不存在")
        expected = answer["document"]
        app_task = expected["method"] == "app"
        existing = scoped(connection, storage.trials, actor.library, task_id=task_id)
        previous_session = existing["document"]["session_id"] if existing else None
        session_id = body.session_id or previous_session
        if not app_task and session_id:
            fail(422, "常规人工对照没有 App 会话")
        if previous_session and previous_session != session_id:
            fail(409, "任务已绑定其他会话")
        data = sorted(library_rows(connection, storage.editions, actor.library), key=lambda item: item["edition_id"])
        copies = sorted(library_rows(connection, storage.holdings, actor.library), key=lambda item: item["holding_id"])
        fingerprint = hashlib.sha256(json.dumps([data, copies], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if fingerprint != expected["catalog_fingerprint"]:
            fail(409, "书目或复本数据版本已变更，请使用冻结环境记录试验")
        record = session(connection, session_id, actor.library) if session_id else None
        if record and (record["edition_id"] != expected["target_edition_id"] or
                       record["holding_id"] != expected["target_holding_id"] or
                       record["model_version"] != expected["model_version"]):
            fail(409, "会话的目标版本、复本或模型与冻结任务不符")
        state = existing["document"] if existing else {**expected, "session_id": session_id, "first": None,
                                                         "final": None, "events": [], "outcome": "in_progress"}
        if state["outcome"] != "in_progress":
            fail(409, "任务已结束")
        state["session_id"] = session_id
        if state["events"] and body.elapsed_s < state["events"][-1]["elapsed_s"]:
            fail(422, "经过时间不能早于上一条事件")
        observations = rows(connection.execute(select(storage.recognitions).filter_by(session_id=session_id)
                            .order_by(storage.recognitions.c.created_at))) if session_id else []
        state["recognitions"] = [item["document"] for item in observations]
        state["unique_hint"] = bool(observations and observations[0]["document"]["state"] == "unique")
        if body.action == "start":
            if state["events"]:
                fail(409, "任务已经开始")
            state["events"].append({"event": "start", "elapsed_s": 0, "at": now(), "actor": actor.actor})
        elif body.action == "milestone":
            if not app_task or any(event.get("milestone") == body.milestone for event in state["events"]):
                fail(409, "该任务步骤已经记录或不适用")
            state["events"].append({"event": "milestone", "milestone": body.milestone,
                                    "elapsed_s": body.elapsed_s, "at": now(), "actor": actor.actor})
        elif body.action == "take":
            if expected["presence"] == "absent" or body.elapsed_s > 120:
                fail(422, "该任务不允许记录此次取书")
            holding = None
            correct = False
            if body.barcode:
                if body.manual_edition_id:
                    fail(422, "请选择一种核验证据")
                holding = scoped(connection, storage.holdings, actor.library, barcode=body.barcode)
                correct = bool(holding and holding["holding_id"] in expected["allowed_holding_ids"])
                evidence = "barcode"
            elif body.manual_edition_id:
                if expected["target_holding_id"] or len({name.strip() for name in body.witnesses if name.strip()}) != 2:
                    fail(422, "人工版本核验需要两名不同核验人员，指定复本必须具备实物标识")
                if not scoped(connection, storage.editions, actor.library, edition_id=body.manual_edition_id):
                    fail(422, "人工核验版本不存在")
                correct = body.manual_edition_id == expected["target_edition_id"]
                evidence = "manual_double"
            else:
                evidence = "none"
            take = {"outcome": "correct" if correct else "pending" if evidence == "none" or (body.barcode and not holding) else "wrong",
                    "evidence": evidence, "holding_id": holding["holding_id"] if holding else None,
                    "barcode": body.barcode, "manual_edition_id": body.manual_edition_id,
                    "witnesses": body.witnesses, "at": now(), "elapsed_s": body.elapsed_s, "actor": actor.actor}
            if record:
                take.update(book_difference(connection, record, holding))
                save_verification(connection, session_id, "independent", take)
            if state["first"] is None:
                state["first"] = take
            state["events"].append(take)
            if correct:
                state.update(final=take, outcome="success", elapsed_s=body.elapsed_s)
        else:
            if expected["presence"] == "absent":
                if not observations:
                    fail(422, "负例须先完成首张画面的识别")
                outcome = "negative"
            else:
                outcome = body.outcome if body.outcome in ("timeout", "abandoned", "unverified") else "unverified"
            elapsed = 120 if outcome in ("timeout", "abandoned") else min(120, body.elapsed_s)
            state.update(outcome=outcome, elapsed_s=elapsed)
        state.update(updated_at=now())
        if existing:
            connection.execute(storage.trials.update().where(storage.trials.c.library_id == actor.library,
                               storage.trials.c.task_id == task_id).values(document=state))
        else:
            connection.execute(storage.trials.insert().values(library_id=actor.library, task_id=task_id, document=state))
        audit(connection, actor, "trial_event", {"task_id": task_id, "action": body.action})
        return remember(connection, scope, key, digest, state)


def flattened(result):
    first = result.get("first") or {}
    final = result.get("final") or {}
    columns = ("task_id", "participant_code", "method", "matched_pair_id", "target_edition_id",
               "target_holding_id", "allowed_holding_ids", "presence", "difficulty", "unique_hint",
               "elapsed_s", "outcome", "model_version", "device", "lighting", "catalog_fingerprint")
    return {**{key: result.get(key) for key in columns}, "first_holding_id": first.get("holding_id"),
            "final_holding_id": final.get("holding_id"), "evidence": first.get("evidence"),
            "first_outcome": first.get("outcome"), "final_outcome": final.get("outcome")}


@trial_router.get("/export")
def export(format: str = "csv", actor: Actor = Depends(verifier)):
    from .main import STORE, fail
    with STORE.read() as connection:
        records = rows(connection.execute(select(storage.trials).filter_by(library_id=actor.library)
                                         .order_by(storage.trials.c.task_id)))
    data = [item["document"] for item in records]
    if format == "json":
        return {"items": data}
    if format != "csv":
        fail(400, "导出格式须为 csv 或 json")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(flattened({})))
    writer.writeheader()
    for item in data:
        record = flattened(item)
        record["allowed_holding_ids"] = "|".join(record["allowed_holding_ids"])
        for key, value in record.items():
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                record[key] = "'" + value
        writer.writerow(record)
    return Response("\ufeff" + output.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="trial-results.csv"'})


@trial_router.get("/tasks/{task_id}/photos/{recognition_id}")
def trial_photo(task_id: str, recognition_id: str, actor: Actor = Depends(verifier)):
    from fastapi.responses import FileResponse
    from .main import PHOTOS, STORE, fail, scoped
    with STORE.read() as connection:
        task = scoped(connection, storage.trials, actor.library, task_id=task_id)
        entry = row(connection.execute(select(storage.recognitions).filter_by(recognition_id=recognition_id)))
    if not task or not entry or task["document"]["session_id"] != entry["session_id"] or not entry["photo_expires_at"] or entry["photo_expires_at"] <= now():
        fail(404, "照片未保存或已到期")
    path = PHOTOS / (entry["recognition_id"] + ".jpg")
    if not path.is_file():
        fail(404, "照片未保存或已到期")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
