"""The explicit-path quality verifier supports both legacy and migrated snapshots."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "verify_product_data_quality.py"
SPEC = importlib.util.spec_from_file_location("product_data_quality_verifier", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


def _snapshot(path: Path, *, provenance: bool) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE products (title TEXT, site TEXT, payload TEXT, deleted_at TEXT);"
            "INSERT INTO products VALUES ('safe title', '', '{}', NULL);"
            "CREATE TABLE violation_words (word TEXT, action TEXT, source TEXT, enabled INTEGER"
            + (", site_scope TEXT, source_url TEXT, rule_version TEXT" if provenance else "")
            + ");"
        )
        if provenance:
            db.execute(
                "INSERT INTO violation_words VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("restricted", "remove", "manual", 1, "MY", "https://example.org/rules", "v1"),
            )
        else:
            db.execute(
                "INSERT INTO violation_words VALUES (?, ?, ?, ?)",
                ("restricted", "remove", "seed", 1),
            )


def test_quality_verifier_handles_legacy_snapshot_without_provenance_columns(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    _snapshot(path, provenance=False)

    result = VERIFIER.summarize(path)

    assert result["violation_word_provenance_schema_available"] == 0
    assert result["enabled_words_with_site_scope"] == 0
    assert result["enabled_words_with_source_url"] == 0
    assert result["enabled_words_with_rule_version"] == 0


def test_quality_verifier_counts_recorded_provenance_without_claiming_review(tmp_path) -> None:
    path = tmp_path / "migrated.db"
    _snapshot(path, provenance=True)

    result = VERIFIER.summarize(path)

    assert result["violation_word_provenance_schema_available"] == 1
    assert result["enabled_words_with_site_scope"] == 1
    assert result["enabled_words_with_source_url"] == 1
    assert result["enabled_words_with_rule_version"] == 1
