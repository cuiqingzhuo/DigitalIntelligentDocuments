"""Index the attributed BookSpineDataset v1.0 release or export YOLO-OBB data."""

import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "visual"
ARCHIVE_NAME = "BookSpineDataset_v1.0.zip"
SHA256 = "951907289688081254794c87984d433053f09b149f3744ab6b3aae3d6c4bde4e"
SOURCE = "https://github.com/wyn2282145606-crypto/BookSpineDataset/releases/tag/v1.0"
PREFIX = "spine_datasets/"
SPLITS = ("test", "val", "train")


def to_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def inspect_box(line):
    fields = line.split()
    if len(fields) != 9 or fields[0] != "0":
        raise ValueError(f"Unexpected annotation format: {line}")
    coords = [float(item) for item in fields[1:]]
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in coords):
        return None
    corners = list(zip(coords[::2], coords[1::2]))
    turns = []
    for i, (x, y) in enumerate(corners):
        x2, y2 = corners[(i + 1) % 4]
        x3, y3 = corners[(i + 2) % 4]
        turns.append((x2 - x) * (y3 - y2) - (y2 - y) * (x3 - x2))
    if min(turns) <= 0 <= max(turns):
        return None
    return coords


def build(archive_path, output, export):
    with archive_path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != SHA256:
            raise ValueError("Archive SHA-256 differs from the author's v1.0 release")
    output.mkdir(exist_ok=True, parents=True)
    if export is not None and export.exists():
        raise ValueError("Choose an unused export directory")
    stats = {}
    skipped_boxes = []
    duplicate_images = []
    image_hashes = {}
    seen_ids = set()

    index_path = output / "instances.jsonl"
    index_temporary = index_path.with_suffix(".tmp")
    with ZipFile(archive_path) as archive, index_temporary.open(
        "w", encoding="utf-8", newline="\n"
    ) as manifest:
        license_text = archive.read(PREFIX + "LICENSE.txt")
        (output / "SOURCE-LICENSE.txt").write_bytes(license_text)
        for split in SPLITS:
            counts = {"source_images": 0, "source_boxes": 0, "images": 0, "boxes": 0}
            stats[split] = counts
            members = archive.read(PREFIX + split + ".txt.txt").decode("utf-8-sig").splitlines()
            if export is not None:
                image_dir = export / "images" / split
                label_dir = export / "labels" / split
                image_dir.mkdir(exist_ok=True, parents=True)
                label_dir.mkdir(exist_ok=True, parents=True)
            for member in members:
                filename = PurePosixPath(member).name
                image_id = PurePosixPath(filename).stem
                if member != f"{PREFIX}images/{filename}" or image_id in seen_ids:
                    raise ValueError(f"Unexpected image ID or split overlap: {member}")
                seen_ids.add(image_id)
                image = archive.read(member)
                if not image.startswith(b"\xff\xd8") or image.rfind(b"\xff\xd9") < 2:
                    raise ValueError(f"Incomplete JPEG: {member}")
                rows = archive.read(f"{PREFIX}labels/{image_id}.txt").decode("utf-8-sig").splitlines()
                counts["source_images"] += 1
                counts["source_boxes"] += len(rows)
                digest = hashlib.sha256(image).digest()
                if digest in image_hashes:
                    duplicate_images.append([member, image_hashes[digest]])
                    continue
                image_hashes[digest] = member
                valid_rows = []
                for line_num, row in enumerate(rows, start=1):
                    coords = inspect_box(row)
                    if coords is None:
                        skipped_boxes.append([split, image_id, line_num])
                        continue
                    valid_rows.append(row)
                    manifest.write(to_json({
                        "split": split, "image_member": member, "source_line": line_num,
                        "class": "book_spine", "quad_xy_normalized": coords,
                    }) + "\n")
                counts["images"] += 1
                counts["boxes"] += len(valid_rows)
                if export is not None:
                    (image_dir / filename).write_bytes(image)
                    (label_dir / (image_id + ".txt")).write_text(
                        "\n".join(valid_rows) + "\n", encoding="utf-8"
                    )
        text_root = PREFIX + "SpineText-40/"
        text_data = json.loads(archive.read(text_root + "instance_text_annotations.json.json"))
        ocr_rows = 0
        image_text_rows = 0
        for name, entries in text_data.items():
            archive.read(text_root + "images/" + name)
            for entry in entries:
                if "book_id" in entry:
                    ocr_rows += bool(entry["gt_text"].strip())
                else:
                    image_text_rows += bool(entry["full_text"].strip())
        if export is not None:
            (export / "source-license.txt").write_bytes(license_text)
            (export / "data.yaml").write_text(
                f"path: {to_json(export.resolve().as_posix())}\n"
                "train: images/train\nval: images/val\ntest: images/test\nnames:\n  0: book_spine\n",
                encoding="utf-8"
            )
            (export / "attribution.txt").write_text(
                f"BookSpineDataset v1.0. Copyright 2026 Yanan Wu and Zhigang Lian.\n{SOURCE}\n"
                "Original images, original splits and labels with invalid boxes and duplicate JPEGs removed.\n",
                encoding="utf-8"
            )
    if stats["train"]["boxes"] < 10000:
        raise ValueError("Training set contains fewer than 10,000 usable objects")
    index_temporary.replace(index_path)
    report = {
        "source": SOURCE, "authors": ["Yanan Wu", "Zhigang Lian"], "version": "v1.0",
        "source_sha256": SHA256, "archive_bytes": archive_path.stat().st_size, "splits": stats,
        "excluded_boxes": skipped_boxes, "excluded_duplicate_images": duplicate_images,
        "source_split_overlap_by_id": 0,
        "spinetext40": {
            "images": len(text_data),
            "spine_transcriptions": ocr_rows,
            "whole_image_transcriptions": image_text_rows,
        },
        "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
    }
    (output / "stats.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DATA / ARCHIVE_NAME)
    parser.add_argument("--index-dir", type=Path, default=DATA)
    parser.add_argument("--export", type=Path, help="Also write a new YOLO-OBB directory")
    args = parser.parse_args()
    build(args.archive, args.index_dir, args.export)
