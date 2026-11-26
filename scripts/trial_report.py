"""Compute acceptance metrics from the independent trial export."""

import argparse
import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np


def proportion(count, total):
    return {"count": count, "total": total, "rate": count / total if total else None}


def percentile(values, quantile):
    values = sorted(values)
    if not values:
        return None
    offset = quantile * (len(values) - 1)
    low, high = math.floor(offset), math.ceil(offset)
    return values[low] + (values[high] - values[low]) * (offset-low)


def time_stats(data):
    completed = [item for item in data if item.get("elapsed_s") is not None]
    values = [item["elapsed_s"] for item in completed]
    correct = [item["elapsed_s"] for item in completed if item["outcome"] == "success"]
    return {"records": len(data), "timed": len(values), "median_s": percentile(values,.50),
            "p90_s": percentile(values,.90), "timeouts": sum(item["outcome"] == "timeout" for item in data),
            "abandoned": sum(item["outcome"] == "abandoned" for item in data),
            "successful_times_s": correct}


def iou(a, b):
    x, y = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    overlap, _ = cv2.intersectConvexConvex(x, y)
    union = cv2.contourArea(x) + cv2.contourArea(y) - overlap
    return overlap / union if union > 0 else 0


def recognize_metrics(data):
    hits, top3, rejected, confirms, durations, per_device = 0, 0, 0, 0, [], defaultdict(list)
    for item in data:
        snapshots = item.get("recognitions", [])
        head = snapshots[0] if snapshots else {}
        targets = item.get("target_quads", [])
        candidates = head.get("candidates", [])
        matched = [any(iou(candidate["quad"], target) >= .5 for target in targets) for candidate in candidates]
        hits += bool(matched and matched[0])
        top3 += any(matched[:3])
        rejected += head.get("state") not in ("unique", "confirm")
        confirms += head.get("state") == "confirm"
        for snapshot in snapshots:
            if snapshot.get("duration_ms") is not None:
                durations.append(snapshot["duration_ms"])
            if snapshot.get("device_ms") is not None:
                name = snapshot.get("device_model") or item.get("device") or "unknown"
                per_device[name].append(snapshot["device_ms"])
    return {"annotated": sum(bool(item.get("target_quads")) for item in data),
            "top1_iou50": proportion(hits, len(data)), "top3_iou50": proportion(top3, len(data)),
            "rejected": proportion(rejected, len(data)), "confirm": proportion(confirms, len(data)),
            "server_event_latency_ms": {"count": len(durations), "p95": percentile(durations,.95)},
            "device_latency_ms": {name: {"count":len(values),"median":percentile(values,.5),
                                         "p95":percentile(values,.95)} for name, values in per_device.items()}}


def sus_scores(path):
    if not path:
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        data = list(csv.DictReader(stream))
    result, seen = [], set()
    for item in data:
        participant = item["participant_code"].strip()
        values = [int(item[f"q{i}"]) for i in range(1, 11)]
        if not participant or participant in seen or any(value < 1 or value > 5 for value in values):
            raise ValueError("SUS participant codes must be unique and responses must be 1–5")
        seen.add(participant)
        result.append({"participant_code":participant,
                       "score":2.5 * sum(value-1 if i%2 == 0 else 5-value for i,value in enumerate(values))})
    return result


def metrics(data, scores):
    participants = defaultdict(list)
    pairs = defaultdict(lambda: defaultdict(list))
    app, manual, absent = [], [], []
    for item in data:
        participants[item["participant_code"]].append(item)
        if item["method"] == "app" and item["presence"] == "absent":
            absent.append(item)
        elif item["method"] == "app":
            app.append(item)
        elif item["method"] == "manual":
            manual.append(item)
        if item["presence"] == "visible" and item["matched_pair_id"] and item.get("elapsed_s") is not None:
            pairs[(item["participant_code"],item["matched_pair_id"])][item["method"]].append(item["elapsed_s"])
    deltas = [methods["app"][0]-methods["manual"][0] for methods in pairs.values()
              if len(methods["app"]) == len(methods["manual"]) == 1]
    first = [item for item in app if (item.get("first") or {}).get("outcome") == "correct"]
    final = [item for item in app if item["outcome"] == "success"]
    mistakes = [item for item in absent if item.get("unique_hint") is True]
    complete = sum(item["outcome"] == "negative" for item in absent)
    return {
        "participants": len(participants),
        "first_correct": proportion(len(first), len(app)),
        "final_correct": proportion(len(final), len(app)),
        "wrong_first_then_correct": sum((item.get("first") or {}).get("outcome") == "wrong" for item in final),
        "absent_false_unique": proportion(len(mistakes), len(absent)),
        "absent_evaluated": complete,
        "app_timing": time_stats(app), "manual_timing": time_stats(manual),
        "paired_app_minus_manual_s": {"pairs":len(deltas), "median":percentile(deltas,.5), "values":deltas},
        "recognition": recognize_metrics(app),
        "difficulty_counts": dict(Counter(item["difficulty"] for item in app)),
        "step_completions": {step: {"tasks":sum(any(event.get("milestone") == step for event in item.get("events", [])) for item in app+absent),
                                   "readers":len({item["participant_code"] for item in app+absent
                                                  if any(event.get("milestone") == step for event in item.get("events", []))})}
                             for step in ("search", "arrival", "capture", "feedback")},
        "sus": {"n":len(scores),"mean":statistics.mean(item["score"] for item in scores) if scores else None,
                "scores":scores},
        "failure_examples": [{"task_id":item["task_id"],"outcome":item["outcome"],
                              "first":(item.get("first") or {}).get("outcome"),
                              "unique_hint":item.get("unique_hint")}
                             for item in app+absent if item in mistakes or (item in app and item not in first)],
        "by_participant": {person: [{"task_id":item["task_id"],"outcome":item["outcome"],"method":item["method"],
                                     "presence":item["presence"],"elapsed_s":item.get("elapsed_s")}
                                    for item in records] for person,records in sorted(participants.items())},
        "pilot_gate": {
            "counts_ready": len(app) == 30 and len(manual) == 30 and len(absent) == complete == 20
                            and len(participants) >= 10 and all(item["outcome"] != "in_progress" for item in data),
            "first_26_of_30": len(app) == 30 and len(first) >= 26,
            "absent_at_most_1_of_20": len(absent) == complete == 20 and len(mistakes) <= 1,
            "final_27_of_30": len(app) == 30 and len(final) >= 27,
            "sus": len(scores) >= 10 and statistics.mean(item["score"] for item in scores) >= 70,
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("export", type=Path, help="JSON export from the verifier workspace")
    parser.add_argument("--sus", type=Path, help="CSV with participant_code and q1…q10")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = metrics(json.loads(args.export.read_text(encoding="utf-8-sig"))["items"], sus_scores(args.sus))
    result = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    else:
        print(result)


if __name__ == "__main__":
    main()
