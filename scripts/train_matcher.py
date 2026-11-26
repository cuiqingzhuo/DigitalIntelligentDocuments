"""Fit only on NCL train triplets and evaluate full-catalog retrieval by split."""

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

from app.catalog import normalize
from app.recognition import grams, title_features, title_score

ROOT = Path(__file__).resolve().parents[1]


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fit(pairs):
    from sklearn.linear_model import LogisticRegression
    features, labels = [], []
    for item in pairs:
        query = normalize(item["query_title"])
        for positive in (True, False):
            title = normalize(item["positive_title" if positive else "negative_title"])
            features.append(title_features(query, title))
            labels.append(int(positive))
    estimator = LogisticRegression(class_weight="balanced", max_iter=300).fit(features, labels)
    return {"intercept": float(estimator.intercept_[0]), "weights": estimator.coef_[0].tolist(),
            "training_pairs": len(pairs), "source": "NCL train", "version": "title-ranker-v1"}


def evaluate(queries, catalog, weights, collect=False):
    lookup = defaultdict(set)
    for identifier, title in catalog.items():
        for gram in grams(title):
            lookup[gram].add(identifier)
        lookup[title].add(identifier)
    stats = {name: {"top1": 0, "top3": 0} for name in ("baseline", "trained")}
    recall, positive, negative = 0, [], []
    for key, query in queries:
        candidates = set().union(*(lookup[gram] for gram in grams(query)), lookup[query])
        if not candidates and len(query) == 1:
            candidates = {identifier for identifier, title in catalog.items() if query in title}
        recall += key in candidates
        for mode, model in (("baseline", None), ("trained", weights)):
            scored = sorted(((title_score(query, catalog[ident], model) * .91, ident)
                             for ident in candidates), key=lambda v: (-v[0], v[1]))[:4]
            stats[mode]["top1"] += bool(scored and scored[0][1] == key)
            stats[mode]["top3"] += any(ident == key for _, ident in scored[:3])
            if collect and mode == "trained":
                target_score = next((score for score, ident in scored if ident == key), 0.)
                rivals = [score for score, ident in scored if ident != key]
                if target_score:
                    positive.append((target_score, target_score-max(rivals, default=0.)))
                if rivals:
                    negative.append((rivals[0], rivals[0]-(rivals[1] if len(rivals)>1 else 0.)))
    return {"queries": len(queries), "catalog": len(catalog), "recalled": recall,
            **stats}, positive, negative


def calibrate(positive, negative):
    values = []
    for threshold in [value / 100 for value in range(75, 93)]:
        for margin in [value / 100 for value in range(8, 31, 2)]:
            good = sum(score >= threshold and separation >= margin for score, separation in positive)
            bad = sum(score >= threshold and separation >= margin for score, separation in negative)
            if bad <= len(negative) * .05:
                values.append((good, -bad, threshold, margin))
    good, bad, threshold, margin = max(values)
    return {"unique_threshold": threshold, "margin": margin, "candidate_threshold": .50,
            "positive_hints": good, "hard_negative_hints": -bad, "positive_pool": len(positive),
            "hard_negative_pool": len(negative), "scope": "NCL validation title-text proxy"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", type=Path, default=ROOT / "models/title-ranker.json")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/title-retrieval.json")
    args = parser.parse_args()
    pairs = csv_rows(ROOT / "data/ncl/matching_triplets.csv")
    catalog = csv_rows(ROOT / "data/ncl/catalog.csv")
    weights = fit([item for item in pairs if item["split"] == "train"])
    write_json(args.weights, weights)
    report = {"training_pairs": weights["training_pairs"], "ncl": {}, "gutenberg": {}}
    for split in ("validation", "test"):
        pool = {item["isbn13"]: normalize(item["title"]) for item in catalog if item["split"] == split}
        queries = [(item["positive_isbn13"], normalize(item["query_title"])) for item in pairs if item["split"] == split]
        scores, positive, negative = evaluate(queries, pool, weights, split == "validation")
        report["ncl"][split] = scores
        if split == "validation":
            report["thresholds"] = calibrate(positive, negative)
        print("NCL", split, scores, flush=True)
        data = csv_rows(ROOT / f"data/gutenberg/{split}.csv")
        pool = {item["ebook_id"]: normalize(item["title"]) for item in data}
        queries = [(item["ebook_id"], normalize(item["query_text"])) for item in data]
        scores, _, _ = evaluate(queries, pool, weights)
        report["gutenberg"][split] = scores
        print("Gutenberg", split, scores, flush=True)
    write_json(args.report, report)


if __name__ == "__main__":
    main()
