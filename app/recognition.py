import json
import math
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import Field, model_validator

from .catalog import normalize
from .routing import Strict

ROOT = Path(__file__).resolve().parents[1]
RANKER = ROOT / "models" / "title-ranker.json"
BASELINE = {"version": "baseline-v1", "unique_threshold": 0.86, "margin": 0.14,
            "candidate_threshold": 0.5, "matcher": "baseline"}


class Observation(Strict):
    quad: list[list[float]] = Field(min_length=4, max_length=4)
    text: str = Field(max_length=400)
    direction: str = Field(default="horizontal", pattern=r"^(horizontal|vertical)$")

    @model_validator(mode="after")
    def valid_quad(self):
        if any(len(point) != 2 or any(not 0 <= v <= 1 for v in point) for point in self.quad):
            raise ValueError("书脊框必须是四个归一化坐标点")
        edges = [self.quad[(i+1)%4] for i in range(4)]
        turns = [(b[0]-a[0])*(c[1]-b[1]) - (b[1]-a[1])*(c[0]-b[0])
                 for a, b, c in zip(self.quad, edges, edges[1:] + edges[:1])]
        if min(turns) <= 0 <= max(turns):
            raise ValueError("书脊框须按顺序形成凸四边形")
        return self


class DeviceCandidate(Strict):
    observation_index: int = Field(ge=0, le=79)
    score: float = Field(ge=0, le=1)
    evidence: list[str] = Field(max_length=8)
    alternatives: list[str] = Field(default_factory=list, max_length=2)


class DeviceDisplay(Strict):
    state: str = Field(pattern=r"^(unique|confirm|retake|unrecognized)$")
    model_version: str = Field(pattern=r"^android-local-v1$")
    candidates: list[DeviceCandidate] = Field(max_length=3)


def recorded_device_result(display, observations, catalog, target, copy, capture, quality):
    expected = {"unique": (1, 1), "confirm": (1, 3), "retake": (0, 0), "unrecognized": (0, 0)}
    minimum, maximum = expected[display.state]
    if (not minimum <= len(display.candidates) <= maximum or bool(quality) != (display.state == "retake")
            or (copy and display.state == "unique")):
        raise ValueError("本机判定与画质、候选数量或指定复本不一致")
    by_id = {book["edition_id"]: book for book in catalog}
    positions_by_identity = {id(item): (row, column) for row, column, item in positions(observations)}
    allowed_evidence = {"书名片段", "ISBN", "索书号", "版次／卷册", "出版社", "作者"}
    candidates, used = [], set()
    for item in display.candidates:
        index = item.observation_index
        if index >= len(observations) or index in used or not set(item.evidence) <= allowed_evidence:
            raise ValueError("本机候选与书脊观察记录不一致")
        current = observations[index]
        if not current["text"]:
            raise ValueError("本机候选缺少可见文字")
        used.add(index)
        row, column = positions_by_identity[id(current)]
        alternatives = [{key: by_id[key_id][key] for key in ("edition_id", "title", "volume", "publisher", "year")}
                        for key_id in item.alternatives if key_id in by_id and key_id != target]
        candidates.append({**current, "candidate_edition_id": target, "row": row, "column": column,
                           "column_scope": "shelf_row" if capture == "left_visible" else "image",
                           "score": item.score, "evidence": item.evidence, "alternatives": alternatives})
    return {"state": display.state, "candidates": candidates, "model_version": display.model_version,
            "capture_scope": capture, "quality": quality}


def grams(value):
    return Counter(zip(value, value[1:]))


def title_features(query, title):
    if not query or not title:
        return (0.,) * 5
    if query == title:
        return (1.,) * 5
    left, right = grams(query), grams(title)
    overlap = sum((left & right).values())
    length = min(len(query), len(title)) / max(len(query), len(title))
    sequence = SequenceMatcher(None, query, title, autojunk=False).ratio()
    contains = float(query in title or title in query)
    return (sequence, 2 * overlap / max(1, sum(left.values()) + sum(right.values())),
            contains, length, float(query[:2] == title[:2]))


def rank_weights():
    return json.loads(RANKER.read_text(encoding="utf-8")) if RANKER.is_file() else None


def title_score(query, title, weights=None):
    features = title_features(query, title)
    if query == title and query:
        return 1.
    if weights:
        logit = weights["intercept"] + sum(a*b for a, b in zip(features, weights["weights"]))
        return 1 / (1 + math.exp(-max(-40, min(40, logit))))
    sequence, overlap, contains, length, _ = features
    return max(.72 * sequence + .28 * overlap, .68 + .27 * length if contains else 0)


def center(quad):
    return (sum(x for x, _ in quad) / 4, sum(y for _, y in quad) / 4)


def positions(observations):
    bands = []
    for item in sorted(observations, key=lambda value: center(value["quad"])[1]):
        quad = item["quad"]
        _, y = center(quad)
        height = max(p[1] for p in quad) - min(p[1] for p in quad)
        band = next((band for band in bands if abs(band[0] - y) < .43 * min(height, band[1])), None)
        if band:
            band[2].append(item)
        else:
            bands.append((y, height, [item]))
    return [(i, j, item) for i, band in enumerate(bands, 1)
            for j, item in enumerate(sorted(band[2], key=lambda value: center(value["quad"])[0]), 1)]


def recognize(observations, catalog, holdings, target_edition, target_holding, capture, quality, model):
    weights = (model.get("rank_weights") or rank_weights()) if model["matcher"] == "trained" else None
    by_edition = defaultdict(list)
    for copy in holdings:
        by_edition[copy["edition_id"]].append(copy)
    titles = {edition["edition_id"]: normalize(edition["title"]) for edition in catalog}
    index = defaultdict(set)
    by_id = {edition["edition_id"]: edition for edition in catalog}
    for edition_id, title in titles.items():
        for gram in grams(title):
            index[gram].add(edition_id)
    candidates = []
    for row, column, observation in positions(observations):
        raw = observation["text"]
        fragments = [normalize(item) for item in re.split(r"[\n|；]", raw) if normalize(item)]
        if not fragments:
            continue
        queries = fragments + [left + right for left, right in zip(fragments, fragments[1:])]
        eligible = {target_edition}
        for fragment in queries:
            eligible.update(edition_id for gram in grams(fragment) for edition_id in index[gram])
        # Exact visible ISBN or call number can recall an edition with no readable title.
        for edition in catalog:
            edition_id = edition["edition_id"]
            terms = [edition.get("isbn")] + [copy["call_number"] for copy in by_edition[edition_id]]
            if any(value and len(normalize(value)) >= 3 and any(normalize(value) in f for f in fragments) for value in terms):
                eligible.add(edition_id)
        scored = []
        for edition_id in eligible:
            edition = by_id.get(edition_id)
            if edition is None:
                continue
            title = titles[edition_id]
            evidence = []
            tokens = [item for item in queries if len(item) >= min(2, len(title))]
            title_match = max((title_score(item, title, weights) for item in tokens), default=0.)
            score = title_match * .91
            if title_match >= .55:
                evidence.append("书名片段")
            for key, label in (("publisher", "出版社"), ("volume", "版次／卷册"), ("author", "作者")):
                value = normalize(edition.get(key) or "")
                if value and any(value in text and text != title for text in fragments):
                    score += (.18 if key == "volume" else .04) if title_match >= .55 else 0.
                    evidence.append(label)
            isbn = edition.get("isbn")
            if isbn and any(isbn in text for text in fragments):
                score = max(score, .995)
                evidence.append("ISBN")
            call_numbers = [normalize(copy["call_number"]) for copy in by_edition[edition_id]]
            if any(len(number) >= 3 and any(number == item or item == title + number for item in fragments)
                   for number in call_numbers):
                score = max(score, .94)
                evidence.append("索书号")
            scored.append((min(score, 1.), edition_id, evidence))
        scored.sort(key=lambda item: (-item[0], item[1]))
        target = next((item for item in scored if item[1] == target_edition), None)
        if target is None or target[0] < model["candidate_threshold"]:
            continue
        alternatives = [item for item in scored if item[1] != target_edition and item[0] >= model["candidate_threshold"]]
        explicit = {"ISBN", "索书号", "版次／卷册"}
        conflicts = set().union(*(explicit.intersection(item[2]) for item in alternatives))
        if conflicts - set(target[2]):
            continue
        target_explicit = explicit.intersection(target[2])
        competitor = max((item[0] - (.14 if target_explicit and not explicit.intersection(item[2]) else 0.)
                          for item in alternatives), default=0.)
        if competitor - target[0] >= model["margin"]:
            continue
        candidates.append({
            "quad": observation["quad"], "direction": observation["direction"], "row": row,
            "column": column, "column_scope": "shelf_row" if capture == "left_visible" else "image",
            "candidate_edition_id": target_edition, "score": round(target[0], 5),
            "text": raw, "evidence": target[2], "separation": target[0] - competitor,
            "alternatives": [{"edition_id": item[1], "title": by_id[item[1]]["title"],
                              "volume": by_id[item[1]]["volume"], "publisher": by_id[item[1]]["publisher"],
                              "year": by_id[item[1]]["year"], "score": round(item[0], 5)}
                             for item in alternatives[:2]],
        })
    candidates.sort(key=lambda item: (-item["score"], item["row"], item["column"]))
    state = "retake" if quality else "unrecognized"
    if candidates and not quality:
        first = candidates[0]
        runner = candidates[1]["score"] if len(candidates) > 1 else 0.
        unique = (not target_holding and first["score"] >= model["unique_threshold"]
                  and first["separation"] >= model["margin"] and first["score"] - runner >= model["margin"])
        state = "unique" if unique else "confirm"
        candidates = candidates[:1 if unique else 3]
    else:
        candidates = []
    return {"state": state, "candidates": candidates, "model_version": model["version"],
            "capture_scope": capture, "quality": quality}
