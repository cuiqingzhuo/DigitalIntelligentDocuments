"""Build supervised title-retrieval triplets from the frozen NCL ISBN catalog."""

import argparse
import csv
import hashlib
import io
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "ncl"
FIELDS = ("split", "query_title", "positive_isbn13", "positive_title",
          "negative_isbn13", "negative_title", "negative_selection")


def normalized(title):
    return "".join(c for c in unicodedata.normalize("NFKC", title).casefold()
                   if c.isalnum())


def bigrams(title):
    return {title[i:i + 2] for i in range(len(title) - 1)}


def make_query(title, seed, known):
    # Keep digits, volume numbers and short titles intact.
    if len(title) >= 6:
        eligible = [i for i in range(1, len(title) - 1)
                    if "\u4e00" <= title[i] <= "\u9fff" and title[i] not in "一二三四五六七八九十上下中卷冊部版集"]
        if eligible:
            i = eligible[seed % len(eligible)]
            shortened = title[:i] + title[i + 1:]
            if shortened not in known:
                return shortened
    return title


def examples(records):
    known = Counter(normalized(row["title"]) for row in records)
    for split in ("train", "validation", "test"):
        books = sorted((row for row in records if row["split"] == split),
                       key=lambda row: row["isbn13"])
        titles = [normalized(row["title"]) for row in books]
        neighbors = defaultdict(list)
        for i, title in enumerate(titles):
            for gram in bigrams(title):
                neighbors[gram].append(i)
        query_rows = []
        for i, title in enumerate(titles):
            if len(title) < 2 or known[title] != 1:
                continue
            seed = int(books[i]["isbn13"])
            query = make_query(title, seed, known)
            query_rows.append((i, query))
        query_counts = Counter(query for _, query in query_rows)
        for i, query in query_rows:
            if query_counts[query] > 1:
                continue
            grams = bigrams(query)
            overlaps = Counter()
            for gram in sorted(grams):
                pool = neighbors.get(gram, ())
                if len(pool) <= 120:
                    overlaps.update(pool)
            order = sorted(overlaps, key=lambda j: (-overlaps[j], abs(len(query) - len(titles[j])), j))
            def distinguishable(j):
                candidate = titles[j]
                return bool(candidate and candidate != titles[i] and
                            query not in candidate and candidate not in query and
                            titles[i] not in candidate and candidate not in titles[i])
            negative = next((j for j in order if distinguishable(j)), None)
            method = "shared_bigram"
            if negative is None:
                method = "fallback"
                offset = int(books[i]["isbn13"]) % len(books)
                negative = next((j for step in range(len(books))
                                 if distinguishable(j := (offset + step) % len(books))), None)
            if negative is not None:
                yield (split, query, books[i]["isbn13"], books[i]["title"],
                       books[negative]["isbn13"], books[negative]["title"], method)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="rebuild in memory and compare with the delivered CSV")
    args = parser.parse_args()
    with (DATA / "catalog.csv").open(encoding="utf-8-sig", newline="") as source:
        records = list(csv.DictReader(source))
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(FIELDS)
    counts = Counter()
    for row in examples(records):
        writer.writerow(row)
        counts[row[0]] += 1
    payload = out.getvalue().encode("utf-8")
    path = DATA / "matching_triplets.csv"
    if args.check:
        if path.read_bytes() != payload:
            raise SystemExit("Delivered CSV differs from the rebuilt examples.")
    else:
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)
    print(dict(counts), "sha256", hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    main()
