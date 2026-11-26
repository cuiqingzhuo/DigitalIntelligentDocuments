"""Report whole-photo OCR character error on the auxiliary SpineText-40 set."""

import argparse
import json
import time
import unicodedata
from pathlib import Path
from zipfile import ZipFile

import cv2
import numpy as np

from app.vision import ocr_engine

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "spine_datasets/SpineText-40/"


def normalized(text):
    return "".join(character for character in unicodedata.normalize("NFKC", text) if not character.isspace())


def edit_distance(a, b):
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b)+1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(current[-1]+1, previous[j]+1, previous[j-1]+(left != right)))
        previous = current
    return previous[-1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, default=ROOT / "data/visual/BookSpineDataset_v1.0.zip")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/ocr-spinetext40.json")
    args = parser.parse_args()
    cv2.setNumThreads(2)
    runner = ocr_engine()
    records = []
    with ZipFile(args.archive) as archive:
        annotations = json.loads(archive.read(PREFIX + "instance_text_annotations.json.json"))
        for name, data in sorted(annotations.items()):
            reference = normalized(next(item["full_text"] for item in data if "full_text" in item))
            image = cv2.imdecode(np.frombuffer(archive.read(PREFIX + "images/" + name), np.uint8), cv2.IMREAD_COLOR)
            scale = min(1, 1600 / max(image.shape[:2]))
            if scale < 1:
                image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            start = time.perf_counter()
            prediction, _ = runner(image)
            actual = normalized("".join(text for _, text, confidence in (prediction or []) if float(confidence) >= .48))
            records.append({"image": name, "reference_chars": len(reference), "predicted_chars": len(actual),
                            "edits": edit_distance(reference, actual), "ms": round((time.perf_counter()-start)*1000)})
    total = sum(item["reference_chars"] for item in records)
    edits = sum(item["edits"] for item in records)
    report = {"dataset":"SpineText-40 auxiliary", "scope":"full-photo OCR, whitespace ignored after NFKC",
              "images":len(records), "reference_chars":total, "edits":edits, "cer": edits / total,
              "records":records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print({key:value for key,value in report.items() if key != "records"})


if __name__ == "__main__":
    main()
