"""Read-only summary verifier for a local Miaoshou product database snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any

REPORTED = {
    "products": 355,
    "products_site_empty": 355,
    "payload_site_my": 259,
    "payload_site_missing": 96,
    "products_missing_weight_and_dimensions": 96,
    "products_dimensions_30x20x10": 259,
    "titles_empty_or_over_255": 0,
    "titles_25_to_254": 353,
    "titles_under_25": 1,
    "titles_255": 1,
    "active_skus": 11598,
    "active_skus_positive_price_origin_weight": 11598,
    "active_skus_price_differs_from_origin": 11598,
    "enabled_violation_words": 24,
    "enabled_seed_remove_words": 24,
    "products_with_violation_word_hits": 0,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def positive(value: Any) -> bool:
    parsed = number(value)
    return parsed is not None and parsed > 0


def word_pattern(word: str) -> re.Pattern[str]:
    normalized = unicodedata.normalize("NFKC", word).casefold()
    chars = [char for char in normalized if char not in " \t\r\n-‐‑‒–—"]
    separator = r"[\s\-‐‑‒–—\u00ad\u200b-\u200f\u2060\ufeff]*"
    pattern = separator.join(re.escape(char) for char in chars) or re.escape(normalized)
    if normalized and normalized[0].isascii() and normalized[0].isalnum():
        pattern = r"(?<![A-Za-z0-9_])" + pattern
    if normalized and normalized[-1].isascii() and normalized[-1].isalnum():
        pattern += r"(?![A-Za-z0-9_])"
    return re.compile(pattern, re.IGNORECASE)


def load_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def summarize(path: Path) -> dict[str, int]:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise sqlite3.DatabaseError(f"SQLite quick_check failed: {check}")

        product_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(products)")
        }
        if not {"title", "site", "payload", "deleted_at"}.issubset(product_columns):
            raise sqlite3.DatabaseError("products table is missing required columns")
        selection = ["title", "site", "payload"]
        has_automation_result = "automation_result" in product_columns
        if has_automation_result:
            selection.append("automation_result")
        query = "SELECT " + ", ".join(selection) + " FROM products WHERE deleted_at IS NULL"
        rows = connection.execute(query).fetchall()

        word_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(violation_words)")
        }
        if not {"word", "action", "source", "enabled"}.issubset(word_columns):
            raise sqlite3.DatabaseError("violation_words table is missing required columns")
        provenance_columns = {"site_scope", "source_url", "rule_version"}
        has_provenance = provenance_columns.issubset(word_columns)
        word_selection = ["word", "action", "source"]
        if has_provenance:
            word_selection.extend(sorted(provenance_columns))
        words = connection.execute(
            "SELECT " + ", ".join(word_selection)
            + " FROM violation_words WHERE enabled = 1"
        ).fetchall()
    finally:
        connection.close()

    summaries = {
        "products": 0,
        "products_site_empty": 0,
        "payload_site_my": 0,
        "payload_site_missing": 0,
        "products_missing_weight": 0,
        "products_missing_dimensions": 0,
        "products_missing_weight_and_dimensions": 0,
        "products_dimensions_30x20x10": 0,
        "titles_empty_or_over_255": 0,
        "titles_25_to_254": 0,
        "titles_under_25": 0,
        "titles_255": 0,
        "active_skus": 0,
        "active_skus_positive_price_origin_weight": 0,
        "active_skus_price_differs_from_origin": 0,
        "enabled_violation_words": len(words),
        "violation_word_provenance_schema_available": int(has_provenance),
        "enabled_words_with_site_scope": sum(
            bool(row["site_scope"]) for row in words
        ) if has_provenance else 0,
        "enabled_words_with_source_url": sum(
            bool(row["source_url"]) for row in words
        ) if has_provenance else 0,
        "enabled_words_with_rule_version": sum(
            bool(row["rule_version"]) for row in words
        ) if has_provenance else 0,
        "enabled_seed_remove_words": sum(
            row["source"] == "seed" and row["action"] == "remove" for row in words
        ),
        "products_with_violation_word_hits": 0,
    }
    patterns = [word_pattern(str(row["word"] or "")) for row in words if row["word"]]

    for row in rows:
        if has_automation_result and bool(row["automation_result"]):
            continue
        summaries["products"] += 1
        title = str(row["title"] or "")
        payload = load_payload(row["payload"])
        payload_site = str(payload.get("site") or "").strip().upper()
        if not str(row["site"] or "").strip():
            summaries["products_site_empty"] += 1
        if payload_site == "MY":
            summaries["payload_site_my"] += 1
        if not payload_site:
            summaries["payload_site_missing"] += 1
        if any(pattern.search(title) for pattern in patterns):
            summaries["products_with_violation_word_hits"] += 1

        weight = positive(payload.get("weight"))
        dimensions = [
            positive(payload.get(key))
            for key in ("packageLength", "packageWidth", "packageHeight")
        ]
        missing_dimensions = not all(dimensions)
        summaries["products_missing_weight"] += int(not weight)
        summaries["products_missing_dimensions"] += int(missing_dimensions)
        summaries["products_missing_weight_and_dimensions"] += int(
            not weight and missing_dimensions
        )
        raw_dimensions = [number(payload.get(key)) for key in
                          ("packageLength", "packageWidth", "packageHeight")]
        summaries["products_dimensions_30x20x10"] += int(raw_dimensions == [30, 20, 10])

        title_length = len(title)
        summaries["titles_empty_or_over_255"] += int(not title or title_length > 255)
        summaries["titles_25_to_254"] += int(25 <= title_length <= 254)
        summaries["titles_under_25"] += int(0 < title_length < 25)
        summaries["titles_255"] += int(title_length == 255)

        sku_map = payload.get("skuMap")
        if not isinstance(sku_map, dict):
            continue
        for sku in sku_map.values():
            if not isinstance(sku, dict) or str(sku.get("isDelete", "0")) == "1":
                continue
            summaries["active_skus"] += 1
            if all(positive(sku.get(key)) for key in ("price", "originPrice", "weight")):
                summaries["active_skus_positive_price_origin_weight"] += 1
            price = number(sku.get("price"))
            origin_price = number(sku.get("originPrice"))
            if price is not None and origin_price is not None and price != origin_price:
                summaries["active_skus_price_differs_from_origin"] += 1
    return summaries


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only summary check; no product rows or credentials are printed."
    )
    parser.add_argument("--db", required=True, type=Path, help="SQLite app.db snapshot path")
    args = parser.parse_args()
    path = args.db.expanduser().resolve()
    if not path.is_file():
        print("error: database file does not exist", file=sys.stderr)
        return 2
    try:
        summaries = summarize(path)
    except (sqlite3.Error, OSError) as exc:
        print(f"error: read-only database check failed ({type(exc).__name__})", file=sys.stderr)
        return 2

    print(f"db_sha256={sha256(path)}")
    wal_path = Path(str(path) + "-wal")
    print(f"wal_sha256={sha256(wal_path) if wal_path.is_file() else 'absent'}")
    for key, value in summaries.items():
        print(f"{key}={value}")
    matches = all(summaries.get(key) == value for key, value in REPORTED.items())
    print(f"reported_summary_matches={'yes' if matches else 'no'}")
    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
