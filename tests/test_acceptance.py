import base64
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app import main, storage

LIBRARIAN = {"Authorization": "Bearer demo-librarian"}
ADMIN = {"Authorization": "Bearer demo-admin"}
VERIFIER = {"Authorization": "Bearer demo-verifier"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    pg_url = os.getenv("DIW_TEST_DATABASE_URL")
    admin_engine = create_engine(pg_url) if pg_url else None
    schema = "test_" + uuid4().hex
    if admin_engine:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
        url = make_url(pg_url).update_query_dict({"options": f"-csearch_path={schema}"})
    else:
        url = "sqlite:///" + (tmp_path / "test.db").as_posix()
    store = storage.Store(url)
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "PHOTOS", tmp_path / "photos")
    try:
        with TestClient(main.app) as browser:
            yield browser
    finally:
        store.engine.dispose()
        if admin_engine:
            with admin_engine.begin() as connection:
                connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
            admin_engine.dispose()


def post(client, url, body, key, actors=None):
    return client.post(url, json=body, headers={"Idempotency-Key": key, **(actors or {})})


def new_session(client, key="test-session", **options):
    body = {"edition_id": "DS-3", "face_id": "A01-A", "scanned_face_id": "diw://shelf/demo/F1/A01-A", **options}
    response = post(client, "/sessions", body, key)
    assert response.status_code == 200, response.text
    return response.json()


def test_editions_accessibility_and_map_routing(client):
    assert client.get("/health").json() == {"status": "ok"}
    editions = client.get("/editions", params={"q": "数据结构"}).json()["items"]
    assert [(book["volume"], book["available"]) for book in editions] == [("第2版", 1), ("第3版", 2)]
    for field, query in (("call_number", "TP311.12/03"), ("isbn", "978-7-302-51183-5")):
        assert client.get("/editions", params={"q":query, "field":field}).json()["items"][0]["edition_id"] == "DS-3"
    assert client.get("/editions", params={"q":"xyznone"}).json()["total"] == 0
    assert client.get("/editions", params={"q":"现代操作系统"}).json()["items"][0]["status"] == "loaned"
    assert all("barcode" not in book for book in client.get("/editions/DS-3/holdings").json()["items"])
    path = client.get("/maps/F1", params={"start":"A01-B","face_id":"A01-A"}).json()
    assert [point["id"] for point in path["route"]][::len(path["route"])-1] == ["face01b", "face01a"]
    assert client.get("/maps/F4", params={"face_id":"A03-A"}).json()["reachable"] is False
    assert client.get("/shelf-faces/A01-B", params={"expected":"A01-A"}).json()["matches_selected"] is False
    assert post(client, "/sessions", {"edition_id":"DS-3","face_id":"A01-A","scanned_face_id":"A01-B"},
                "different-face").status_code == 409
    qr = client.get("/admin/shelf-faces/F1/A01-A/qr", headers=ADMIN)
    image = cv2.imdecode(np.frombuffer(qr.content, np.uint8), cv2.IMREAD_GRAYSCALE)
    payload, *_ = cv2.QRCodeDetector().detectAndDecode(image)
    assert payload == "diw://shelf/demo/F1/A01-A"


def test_imports_map_publish_and_permissions(client, monkeypatch):
    csv_editions = "edition_id,title,year,isbn\nlocal1,试点书名,2025,\n"
    csv_holdings = ("holding_id,edition_id,barcode,status,synced_at\n"
                    "hl1,local1,AAA,available,2020-01-01T00:00:00+08:00\n"
                    "hl2,missing,BBB,available,2020-01-01T00:00:00+08:00\n"
                    "hl3,local1,%%,available,2020-01-01T00:00:00+08:00\n")
    body = {"editions_csv":csv_editions, "holdings_csv":csv_holdings, "preview":True}
    assert client.post("/admin/imports", json=body).status_code == 401
    preview = client.post("/admin/imports", json=body, headers=LIBRARIAN).json()
    assert preview["inserted"] == 2 and len(preview["errors"]) == 2
    assert client.get("/editions/local1/holdings").status_code == 404
    body["preview"] = False
    assert client.post("/admin/imports", json=body, headers=LIBRARIAN).json()["inserted"] == 2
    assert client.post("/admin/imports", json=body, headers=LIBRARIAN).json()["unchanged"] == 2
    assert client.get("/editions/local1/holdings").json()["edition"]["stale"]
    old = client.get("/maps/F1").json()
    changed = {**old["document"], "edges":[{**edge,"accessible":False} for edge in old["document"]["edges"]]}
    draft = {"floor":"F1","revision":old["revision"],"document":changed}
    assert client.put("/admin/maps/draft",json=draft,headers=LIBRARIAN).json()["revision"] == 2
    assert client.put("/admin/maps/draft",json=draft,headers=LIBRARIAN).status_code == 409
    assert client.get("/maps/F1",params={"face_id":"A01-A"}).json()["reachable"]
    publish = {"floor":"F1","revision":2}
    assert client.post("/admin/maps/publish",json=publish,headers=LIBRARIAN).status_code == 403
    assert client.post("/admin/maps/publish",json=publish,headers=ADMIN).status_code == 200
    assert not client.get("/maps/F1",params={"face_id":"A01-A"}).json()["reachable"]
    monkeypatch.setitem(main.actors,"other-admin",main.Actor("其他馆员","admin","other"))
    headers = {"Authorization":"Bearer other-admin"}
    assert client.get("/admin/maps/F1/draft",headers=headers).json()["published"]["revision"] == 0
    assert client.get("/trial/tasks",headers=ADMIN).status_code == 403


def test_recognition_slots_and_wrong_book(client):
    session_id = new_session(client)["session_id"]
    url = f"/sessions/{session_id}/recognitions"
    for sample, expectation in (("target", "unique"), ("wrong", "unrecognized"), ("absent", "unrecognized")):
        response = post(client,url,{"capture_scope":"partial","demo_sample":sample},f"sample-{sample}")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["state"] == expectation, result
        if expectation == "unique":
            assert result["candidates"][0]["column"] == 3
            assert result["candidates"][0]["row"] == 1
            assert result["candidates"][0]["column_scope"] == "image"
            assert "数据结构" in result["candidates"][0]["text"]
    encoded = base64.b64encode((main.STATIC/"samples"/"target.jpg").read_bytes()).decode()
    denied = post(client,url,{"capture_scope":"partial","image_base64":encoded},"denied-photo")
    assert denied.status_code == 403
    result = post(client,url,{"capture_scope":"left_visible","image_base64":encoded,"upload_consent":True},"allowed-photo").json()
    assert result["state"] == "unique" and result["photo_expires_at"] is None
    assert result["candidates"][0]["column_scope"] == "shelf_row"
    verification = f"/sessions/{session_id}/verifications"
    assert post(client,verification,{"barcode":"DIW000002"},"wrong-attempt").json()["first"]["outcome"] == "wrong"
    correct = post(client,verification,{"barcode":"DIW000009"},"right-attempt").json()
    assert correct["first"]["holding_id"] == "H02" and correct["final"]["holding_id"] == "H09"
    assert post(client,verification,{"barcode":"DIW000001"},"third-attempt").json()["final"]["holding_id"] == "H09"


def test_concurrent_idempotency_and_workorder_receipt(client):
    with ThreadPoolExecutor(max_workers=3) as pool:
        sessions = list(pool.map(lambda _: new_session(client), range(3)))
    assert len({item["session_id"] for item in sessions}) == 1
    session_id = sessions[0]["session_id"]
    body = {"session_id":session_id,"reason":"unrecognized","note":"试点记录"}
    first = post(client,"/feedback",body,"feedback-same").json()
    assert post(client,"/feedback",body,"feedback-same").json() == first
    assert post(client,"/feedback",body,"feedback-other").json()["workorder_id"] == first["workorder_id"]
    assert post(client,"/feedback",{**body,"note":"修改"},"feedback-same").status_code == 409
    url = "/admin/workorders/" + first["workorder_id"]
    assert len(client.get("/admin/workorders",headers=LIBRARIAN).json()["items"]) == 1
    assert client.patch(url,json={"revision":1,"status":"assigned"},headers=LIBRARIAN).status_code == 200
    assert client.patch(url,json={"revision":1,"status":"closed"},headers=ADMIN).status_code == 409
    close = {"revision":2,"status":"closed","finding":"on_shelf","receipt":"已检查指定架面"}
    assert client.patch(url,json=close,headers=LIBRARIAN).json()["finding"] == "on_shelf"
    receipt = client.get("/workorders/"+first["workorder_id"],params={"session_id":session_id}).json()
    assert receipt["status"] == "closed" and receipt["receipt"] == "已检查指定架面"


def test_independent_trial_does_not_count_reader_self_report(client):
    session_id = new_session(client)["session_id"]
    report = {"task_id":"trial01","participant_code":"R01","method":"app","matched_pair_id":"P01",
              "target_edition_id":"DS-3","allowed_holding_ids":["H01","H09"],"presence":"visible","difficulty":"同名异版"}
    assert client.post("/trial/tasks", json=report, headers=VERIFIER).status_code == 200
    assert client.post("/trial/tasks", json=report, headers=VERIFIER).status_code == 409
    assert post(client,"/feedback",{"session_id":session_id,"reason":"found"},"reader-report").json()["status"]=="self_reported"
    trial = "/trial/tasks/trial01/events"
    data = {"session_id":session_id,"action":"take","barcode":"DIW000002","elapsed_s":12}
    assert post(client,trial,data,"take-wrong",VERIFIER).json()["first"]["outcome"] == "wrong"
    state = post(client,trial,{**data,"barcode":"DIW000001","elapsed_s":31},"take-right",VERIFIER).json()
    assert state["first"]["outcome"] == "wrong" and state["outcome"] == "success"
    assert client.get("/trial/export",headers=LIBRARIAN).status_code == 403
    exported = client.get("/trial/export",params={"format":"json"},headers=VERIFIER).json()["items"]
    assert exported[0]["first"]["holding_id"] == "H02" and exported[0]["final"]["holding_id"] == "H01"


def test_device_result_and_negative_trial_use_first_frame(client):
    session_id = new_session(client)["session_id"]
    recognitions = f"/sessions/{session_id}/recognitions"
    frame = {"capture_scope":"partial","observations":[{"text":"数据结构", "quad":[[.1,.15],[.2,.15],[.2,.85],[.1,.85]]}],
             "device_model":"emulated-android","device_ms":410,
             "device_display":{"state":"confirm","model_version":"android-local-v1","candidates":[
                 {"observation_index":0,"score":.91,"evidence":["书名片段"],"alternatives":["DS-2"]}]}}
    recorded = post(client,recognitions,frame,"first-frame").json()
    assert recorded["source"] == "android_local" and recorded["model_version"] == "android-local-v1"
    assert recorded["candidates"][0]["alternatives"][0]["volume"] == "第2版"
    changed = {**frame, "device_display":{**frame["device_display"],"state":"unique"}}
    assert post(client,recognitions,changed,"retake-frame").json()["state"] == "unique"
    task = {"task_id":"absent1","participant_code":"R09","method":"app","target_edition_id":"DS-3",
            "allowed_holding_ids":["H01","H09"],"presence":"absent","difficulty":"同名异版"}
    assert client.post("/trial/tasks",json=task,headers=VERIFIER).status_code == 200
    finish = {"session_id":session_id,"action":"finish","outcome":"negative","elapsed_s":15}
    outcome = post(client,"/trial/tasks/absent1/events",finish,"negative-end",VERIFIER).json()
    assert outcome["outcome"] == "negative" and not outcome["unique_hint"]
    assert len(outcome["recognitions"]) == 2
    visible = {**task,"task_id":"abandoned","presence":"visible"}
    assert client.post("/trial/tasks",json=visible,headers=VERIFIER).status_code == 200
    endpoint = "/trial/tasks/abandoned/events"
    assert post(client,endpoint,{"action":"start","elapsed_s":0},"early-start",VERIFIER).status_code == 200
    outcome = post(client,endpoint,{"action":"finish","outcome":"abandoned","elapsed_s":28},"early-finish",VERIFIER).json()
    assert outcome["outcome"] == "abandoned" and outcome["elapsed_s"] == 120 and outcome["first"] is None
