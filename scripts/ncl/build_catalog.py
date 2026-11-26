"""Build the WenShu Chinese book catalog from pinned NCL monthly snapshots."""

import argparse
import csv
import hashlib
import io
import json
from collections import Counter
from pathlib import Path
import re
import unicodedata
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[2] / "data" / "ncl"
BASE_URL = "https://isbn.ncl.edu.tw/NEW_ISBNNet/opendata/"
SNAPSHOTS = {
    "202510": "54bff5bfa2d9567f9f6ec8b436c0ea1a2d6d0dada279f957445155bfcac6a62d",
    "202511": "0cca4366d4a668edf220eb0d6e236d6ac4c2e7e4c532046a4e880e3e30a487b5",
    "202512": "04c70fed32d4fe29bd025b877c6b6357a871c4d2c4de696e45882ac9efcdf41e",
    "202601": "ee7375815b77616945078b75d4b50e142a35239501ecaf470e087d317d83c0e7",
    "202602": "7df013f809ded89bbbae4b568d9172691bba95fbb935aaa91969af1e6a069ca0",
    "202603": "9d52778337dac7f119ef718f62d38a89f83bfb5882ef3d556035e1de9a8c40d7",
}
LANGUAGES = {"繁體中文", "簡體中文"}
FIELDS = ["isbn13", "title", "author", "publisher", "split"]


def valid_isbn(number):
    if not re.fullmatch(r"97[89][0-9]{10}", number):
        return False
    return sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(number)) % 10 == 0


def title_key(title):
    text = unicodedata.normalize("NFKC", title).casefold()
    # A work and its subtitle/edition/volume variants should share a split.
    base = re.split(r"[:=]", text, maxsplit=1)[0]
    base = re.sub(r"[（(【\[].*?[）)】\]]", " ", base)
    base = re.sub(r"(第\s*)?[0-9零〇一二三四五六七八九十百上中下]+\s*[冊册卷集輯部話版]", " ", base)
    base = re.sub(r"\d+", "#", base)
    key = "".join(ch for ch in base if ch.isalnum() or ch == "#")
    return key if len(key) >= 2 else "".join(ch for ch in text if ch.isalnum()) or text


def grouped_splits(rows):
    keys = sorted({title_key(row["title"]) for row in rows})
    parent = {key: key for key in keys}

    def leader(key):
        while key != parent[key]:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    # Cluster equal normalized titles and longer stems that share a one-character
    # deletion signature (insertion, substitution, deletion or small local swap).
    signatures = {}
    for key in keys:
        if not 6 <= len(key) <= 96:
            continue
        for fingerprint in [key] + [key[:i] + key[i + 1:] for i in range(len(key))]:
            other = signatures.setdefault(fingerprint, key)
            if key != other:
                a, b = sorted((leader(key), leader(other)))
                parent[b] = a

    assignments = {}
    for key in keys:
        group = leader(key)
        bucket = int(hashlib.sha256(group.encode("utf-8")).hexdigest()[:12], 16) % 100
        assignments[key] = "train" if bucket < 80 else "validation" if bucket < 90 else "test"
    return assignments, len({leader(key) for key in keys})


def clean(value):
    return " ".join(value.split())


def read_source(month, download):
    filename = month + "_isbn.csv"
    path = ROOT / "source" / filename
    url = BASE_URL + filename
    if path.is_file():
        data = path.read_bytes()
    elif download:
        with urlopen(url, timeout=45) as resp:
            data = resp.read()
    else:
        raise FileNotFoundError(f"Missing {filename}. Rerun with --download.")
    sha = hashlib.sha256(data).hexdigest()
    if sha != SNAPSHOTS[month]:
        raise ValueError(f"Unexpected SHA-256 for {url}: {sha}")
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    source_text = data.decode("utf-8-sig").replace("\r\r\n", "\n")
    return list(csv.DictReader(io.StringIO(source_text, newline=""))), {
        "filename": filename, "url": url, "sha256": sha, "bytes": len(data),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Download any missing snapshots.")
    args = parser.parse_args()
    books = {}
    sources = []
    languages = Counter()
    exclusions = Counter()
    for month in SNAPSHOTS:
        source_rows, stats = read_source(month, args.download)
        stats["source_records"] = len(source_rows)
        start = len(books)
        for row in source_rows:
            if row["資料類型"] != "圖書":
                exclusions["non_book_medium"] += 1
                continue
            lang = row["作品語文"]
            if lang not in LANGUAGES:
                exclusions["other_language"] += 1
                continue
            isbn = clean(row["ISBN"]).replace("-", "")
            title = clean(row["申請書名"])
            if not valid_isbn(isbn) or not re.search(r"[\u3400-\u9fff]", title):
                exclusions["invalid_isbn_or_no_han_title"] += 1
                continue
            if isbn in books:
                exclusions["duplicate_isbn"] += 1
                continue
            books[isbn] = dict(isbn13=isbn, title=title, author=clean(row["作者"]),
                               publisher=clean(row["出版機構"]))
            languages[lang] += 1
        stats["retained_records"] = len(books) - start
        sources.append(stats)

    assignments, group_count = grouped_splits(books.values())
    for row in books.values():
        row["split"] = assignments[title_key(row["title"])]
    counts = Counter(row["split"] for row in books.values())
    if counts["train"] < 10000:
        raise ValueError(f"Training ISBN threshold unmet: {counts}")

    output = ROOT / "catalog.csv"
    with output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(books[isbn] for isbn in sorted(books))
    manifest = {
        "provider": "國家圖書館 國際標準書號中心",
        "dataset_page": "https://data.gov.tw/en/datasets/6730",
        "license": "Open Government Data License, version 1.0",
        "license_url": "https://data.gov.tw/license",
        "retrieved_on": "2026-11-26",
        "sources": sources, "excluded_rows": dict(exclusions),
        "catalog_rows": len(books), "catalog_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "retained_language_labels": dict(languages),
        "title_groups": group_count, "split_counts": dict(counts),
    }
    (ROOT / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    )
    print(json.dumps({"rows": len(books), "groups": group_count, "splits": counts}))


if __name__ == "__main__":
    main()
