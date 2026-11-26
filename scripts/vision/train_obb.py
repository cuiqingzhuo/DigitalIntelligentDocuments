"""Fine-tune and test a YOLO-OBB detector using the author's original data splits."""

import argparse
import csv
import hashlib
import json
import shutil
from pathlib import Path

from ultralytics import YOLO
from ultralytics.utils.torch_utils import strip_optimizer

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=ROOT / "bookspine_yolo/data.yaml")
    parser.add_argument("--initial", default="yolo11n-obb.pt")
    parser.add_argument("--checkpoint", type=Path, help="Skip fitting and evaluate an existing checkpoint.")
    parser.add_argument("--training-log", type=Path, help="Record completed epochs from an existing training run.")
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--device", default="0")
    parser.add_argument("--output", type=Path, default=ROOT / "models/bookspine-pilot.pt")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/vision-obb.json")
    args = parser.parse_args()
    if args.checkpoint is None:
        model = YOLO(args.initial)
        model.train(data=str(args.data.resolve()), epochs=args.epochs, imgsz=args.imgsz, batch=args.batch,
                    device=args.device, workers=0, amp=False, close_mosaic=3, plots=False,
                    project=str(ROOT / "vision-runs"), name="pilot-obb")
        checkpoint = Path(model.trainer.best)
        args.training_log = checkpoint.parent.parent / "results.csv"
    else:
        checkpoint = args.checkpoint
    model = YOLO(str(checkpoint))
    scores = model.val(data=str(args.data.resolve()), split="test", batch=args.batch, imgsz=args.imgsz,
                       device=args.device, workers=0, plots=False,
                       project=str(ROOT / "vision-runs"), name="pilot-test")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if checkpoint.resolve() != args.output.resolve():
        shutil.copyfile(checkpoint, args.output)
        strip_optimizer(args.output)
    with args.output.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    epochs = []
    if args.training_log:
        with args.training_log.open(encoding="utf-8", newline="") as stream:
            epochs = list(csv.DictReader(stream))
    data = {"split": "test", "images": 301, "instances": 4757, "device": args.device,
            "image_size": args.imgsz, "mAP50": float(scores.box.map50), "mAP50_95": float(scores.box.map),
            "speed_ms": scores.speed, "checkpoint_bytes": args.output.stat().st_size,
            "checkpoint": args.output.name, "checkpoint_sha256": checksum,
            "completed_epochs": len(epochs) if epochs else None}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(data)


if __name__ == "__main__":
    main()
