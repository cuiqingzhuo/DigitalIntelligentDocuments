"""Build WenShu's reproducible supervised title retrieval data from the bundled CSV.GZ.

    python scripts/prepare_gutenberg.py
    python scripts/prepare_gutenberg.py --verify

Uses only Python 3.9+ standard library. Each row has one actual catalog target.
The query is derived from the title, not observed phone OCR. No images are read.
"""
import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path


DIRECTORY = Path(__file__).resolve().parents[1] / "data" / "gutenberg"
SNAPSHOT = "pg_catalog.csv.gz"
LIMITS = {"train": 12000, "validation": 1500, "test": 1500}
FIELDS = ["ebook_id", "title", "authors", "languages", "query_text", "query_kind", "source_url"]


def sha256(binary):
    return hashlib.sha256(binary).hexdigest()


def stable_order(identifier, salt):
    return sha256(f"wenshu-1:{salt}:{identifier}".encode("utf-8"))


def key(title):
    normalized = unicodedata.normalize("NFKC", title).casefold()
    return "".join(character for character in normalized if character.isalnum())


def query_from_title(title, identifier):
    # These deterministic variants provide a weak label for matching a text query to a book.
    method = int(stable_order(identifier, "query")[:8], 16) % 3
    tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", title), re.UNICODE)
    if method == 0:
        return " ".join(tokens).casefold(), "casefold_tokenized"
    if method == 1:
        return "".join(tokens).casefold(), "compact_title"
    # First part only for long titles. Some short titles are intentionally complete.
    return " ".join(tokens[:6])[:36].casefold(), "title_prefix"


def read_catalog(directory):
    chosen = {}
    rows_read = 0
    with gzip.open(directory / SNAPSHOT, "rt", encoding="utf-8-sig", newline="") as source:
        catalog = csv.DictReader(source)
        required = {"Text#", "Type", "Title", "Language", "Authors"}
        if not required.issubset(catalog.fieldnames or []):
            raise ValueError("Unrecognized Project Gutenberg catalog header")
        for row in catalog:
            rows_read += 1
            identifier = row["Text#"].strip()
            title = " ".join(row["Title"].split())
            normalized = key(title)
            if row["Type"] != "Text" or not identifier.isdecimal() or len(normalized) < 2:
                continue
            languages = ",".join(word for word in re.split(r"[,;\s]+", row["Language"].lower()) if word)
            entry = (int(identifier), title, " ".join(row["Authors"].split()), languages)
            previous = chosen.get(normalized)
            if previous is None or entry[0] < previous[0]:
                chosen[normalized] = entry
    return list(chosen.values()), rows_read


def partition(entries):
    groups = {name: [] for name in LIMITS}
    if len(entries) < sum(LIMITS.values()):
        raise ValueError("Source catalog did not provide enough titles")
    chinese = [entry for entry in entries if "zh" in entry[3].split(",")]
    other = [entry for entry in entries if "zh" not in entry[3].split(",")]
    # Retain all available Chinese titles unless they exceed the entire sample budget.
    total = sum(LIMITS.values())
    chinese.sort(key=lambda entry: stable_order(entry[0], "sample"))
    other.sort(key=lambda entry: stable_order(entry[0], "sample"))
    chinese = chinese[:total]
    other = other[:total - len(chinese)]
    chinese.sort(key=lambda entry: stable_order(entry[0], "split"))
    other.sort(key=lambda entry: stable_order(entry[0], "split"))
    cumulative_limit = 0
    previous_zh, previous_other = 0, 0
    for name, count in LIMITS.items():
        cumulative_limit += count
        end_zh = round(len(chinese) * cumulative_limit / total)
        end_other = cumulative_limit - end_zh
        groups[name] = sorted(chinese[previous_zh:end_zh] + other[previous_other:end_other])
        previous_zh, previous_other = end_zh, end_other
    return groups


def unique_queries(groups):
    entries = [entry for partition_entries in groups.values() for entry in partition_entries]
    title_keys = {key(entry[1]) for entry in entries}
    queries = {entry[0]: query_from_title(entry[1], entry[0]) for entry in entries}
    counts = Counter(key(query) for query, _ in queries.values())
    for identifier, title, _, _ in entries:
        query_key = key(queries[identifier][0])
        if counts[query_key] > 1 or (query_key in title_keys and query_key != key(title)):
            # A prefix may describe more than one catalogue entry. Use the full title label.
            queries[identifier] = title, "full_title_disambiguated"
    if len({key(query) for query, _ in queries.values()}) != len(entries):
        raise ValueError("Ambiguous queries in the selected catalog")
    return queries


def as_csv(entries, queries):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(FIELDS)
    for identifier, title, authors, languages in entries:
        query, kind = queries[identifier]
        if not query:
            raise ValueError(f"Empty training query for eBook {identifier}")
        writer.writerow([identifier, title, authors, languages, query, kind,
                         f"https://www.gutenberg.org/ebooks/{identifier}"])
    return stream.getvalue().encode("utf-8-sig")


def build(directory):
    entries, source_rows = read_catalog(directory)
    groups = partition(entries)
    queries = unique_queries(groups)
    files = {}
    splits = {}
    used_ids, used_titles = set(), set()
    for name, partition_entries in groups.items():
        ids = {entry[0] for entry in partition_entries}
        titles = {key(entry[1]) for entry in partition_entries}
        if (len(ids) != LIMITS[name] or len(titles) != LIMITS[name] or
                used_ids.intersection(ids) or used_titles.intersection(titles)):
            raise ValueError(f"Invalid row count or overlapping labels in {name}")
        used_ids.update(ids)
        used_titles.update(titles)
        filename = f"{name}.csv"
        files[filename] = as_csv(partition_entries, queries)
        language_counts = Counter(language for entry in partition_entries for language in entry[3].split(",") if language)
        splits[name] = {"file": filename, "rows": len(ids), "language_tags": dict(sorted(language_counts.items())),
                        "sha256": sha256(files[filename])}
    manifest = {
        "dataset": "WenShu text-to-catalog matching v1",
        "source": "Project Gutenberg machine-readable catalog (metadata only)",
        "catalog_page": "https://www.gutenberg.org/ebooks/offline_catalogs.html",
        "policy": "https://www.gutenberg.org/policy/robot_access.html",
        "metadata_rights": "The catalog data are granted to the public domain.",
        "official_feed": "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz",
        "downloaded_from": "https://gutenberg.pglaf.org/cache/epub/feeds/pg_catalog.csv.gz",
        "snapshot_acquired_date_asia_shanghai": "2026-11-26",
        "raw_file": SNAPSHOT, "raw_sha256": sha256((directory / SNAPSHOT).read_bytes()),
        "raw_rows": source_rows, "eligible_unique_title_keys": len(entries),
        "unique_catalog_ids_in_all_splits": len(used_ids),
        "unique_title_keys_in_all_splits": len(used_titles),
        "unique_query_keys_in_all_splits": len(queries), "splits": splits,
        "label": "ebook_id identifies the source eBook metadata record, not a physical copy or print edition.",
        "query": "query_text is derived from the catalog title; it is a simulated retrieval query, never observed OCR.",
        "partition": "Keep Type=Text; global NFKC casefold alphanumeric title de-duplication, lowest numeric ID; prioritize zh, hash sample the rest; allocate within zh and other strata. Related titles and translations may still be present.",
    }
    files["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return files, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Rebuild in memory and compare every distributed file.")
    args = parser.parse_args()
    files, manifest = build(DIRECTORY)
    for name, data in files.items():
        output = DIRECTORY / name
        if args.verify:
            if not output.is_file() or output.read_bytes() != data:
                parser.exit(1, f"Verification failed for {name}\n")
        else:
            temporary = output.with_suffix(output.suffix + ".tmp")
            temporary.write_bytes(data)
            temporary.replace(output)
    print("Verified" if args.verify else "Created", {key: split["rows"] for key, split in manifest["splits"].items()})


if __name__ == "__main__":
    main()
