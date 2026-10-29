import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core import errors
from models.violation_word import ViolationWord
from services import title_save_service, word_service


def test_removal_matches_spaces_and_inserted_hyphens() -> None:
    cleaned, removed = word_service.clean_title(
        "New N-i-k-e Air free---shipping shoes",
        [("nike", "remove"), ("free shipping", "remove")],
    )

    assert cleaned == "New Air shoes"
    assert removed == ["free shipping", "nike"]


@pytest.mark.parametrize("separator", ["\u200b", "\u00ad"])
def test_invisible_separators_do_not_bypass_word_matching(separator: str) -> None:
    title = f"free{separator}shipping shoes"
    words = [("free shipping", "remove")]

    assert word_service.matched_words(title, words) == words
    assert word_service.clean_title(title, words) == ("shoes", ["free shipping"])


def test_ascii_word_boundaries_still_protect_unrelated_words() -> None:
    cleaned, removed = word_service.clean_title(
        "freedom shoes", [("free", "remove")],
    )

    assert cleaned == "freedom shoes"
    assert removed == []


def test_legacy_review_words_are_removed_instead_of_only_recorded() -> None:
    legacy_word = ("adidas originals", "review")

    assert word_service.matched_words("Adidas---Originals", [legacy_word]) == [
        ("adidas originals", "remove")
    ]
    assert word_service.clean_title("Adidas---Originals shoes", [legacy_word]) == (
        "shoes",
        ["adidas originals"],
    )


def test_seed_migrates_legacy_review_words_to_removal(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'words.db'}")
    ViolationWord.__table__.create(engine)
    with Session(engine) as db:
        db.add(
            ViolationWord(
                word="legacy term",
                normalized="legacy term",
                action="review",
                source="manual",
                enabled=True,
            )
        )
        db.commit()

        word_service.seed(db)
        db.commit()

        row = db.query(ViolationWord).filter_by(normalized="legacy term").one()
        assert row.action == "remove"
        assert word_service._counts(db)["review"] == 0
        assert ("legacy term", "remove") in word_service.matcher(db)
    engine.dispose()


def test_clear_removes_legacy_review_rows_too(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'words.db'}")
    ViolationWord.__table__.create(engine)
    with Session(engine) as db:
        db.add_all(
            [
                ViolationWord(word="old", normalized="old", action="review"),
                ViolationWord(word="current", normalized="current", action="remove"),
            ]
        )
        db.commit()

        result = word_service.clear(db, confirm=True)

        assert result["removed"] == 2
        assert db.query(ViolationWord).count() == 0
    engine.dispose()


def test_review_category_is_rejected_for_new_words() -> None:
    with pytest.raises(errors.AppError):
        word_service.add(None, "new term", action="review")
    with pytest.raises(errors.AppError):
        word_service.import_words(None, ["new term"], action="review")


def test_export_replace_round_trip_preserves_metadata_and_flattens_legacy_review(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'words-backup.db'}")
    ViolationWord.__table__.create(engine)
    with Session(engine) as db:
        db.add_all([
            ViolationWord(word="manual term", normalized="manual term", action="remove",
                          source="manual", enabled=False, created_at=123.5),
            ViolationWord(word="legacy term", normalized="legacy term", action="review",
                          source="import", enabled=True, created_at=456.25),
        ])
        db.commit()

        backup = word_service.export_words(db)
        db.query(ViolationWord).delete()
        db.commit()
        result = word_service.import_words(db, backup, mode="replace")
        db.commit()

        rows = {row.word: row for row in db.query(ViolationWord).all()}
        assert result["added"] == 2
        assert rows["manual term"].action == "remove"
        assert rows["manual term"].source == "manual"
        assert rows["manual term"].enabled is False
        assert rows["manual term"].created_at == 123.5
        assert rows["legacy term"].action == "remove"
        assert rows["legacy term"].source == "import"
        assert rows["legacy term"].enabled is True
        assert rows["legacy term"].created_at == 456.25
    engine.dispose()


def test_word_rule_provenance_round_trips_and_validates(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'words-provenance.db'}")
    ViolationWord.__table__.create(engine)
    with Session(engine) as db:
        added = word_service.add(
            db,
            "restricted claim",
            site_scope="my",
            source_url="https://example.org/policy",
            rule_version="2026-10",
        )
        db.commit()
        assert added["word"]["siteScope"] == "MY"
        assert added["word"]["sourceUrl"] == "https://example.org/policy"
        assert added["word"]["ruleVersion"] == "2026-10"

        backup = word_service.export_words(db)
        db.query(ViolationWord).delete()
        db.commit()
        word_service.import_words(db, backup, mode="replace")
        db.commit()
        restored = word_service.list_words(db, page=1, page_size=10)["items"][0]
        assert restored["siteScope"] == "MY"
        assert restored["sourceUrl"] == "https://example.org/policy"
        assert restored["ruleVersion"] == "2026-10"

        with pytest.raises(errors.AppError):
            word_service.add(db, "bad site", site_scope="XX")
        with pytest.raises(errors.AppError):
            word_service.add(db, "bad url", source_url="https://")
    engine.dispose()


def test_text_import_applies_provenance_and_legacy_backup_defaults_to_empty(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'words-provenance-import.db'}")
    ViolationWord.__table__.create(engine)
    with Session(engine) as db:
        word_service.import_words(
            db,
            ["manual rule"],
            site_scope="sg",
            source_url="https://example.org/rules",
            rule_version="v2",
        )
        row = db.query(ViolationWord).filter_by(word="manual rule").one()
        assert (row.site_scope, row.source_url, row.rule_version) == (
            "SG", "https://example.org/rules", "v2"
        )

        legacy = {"format": "aohuang.violation-words", "version": 1, "items": [{
            "word": "legacy backup", "action": "remove", "source": "manual",
            "enabled": True, "createdAt": 1,
        }]}
        word_service.import_words(db, legacy, mode="append")
        old = db.query(ViolationWord).filter_by(word="legacy backup").one()
        assert old.site_scope is None and old.source_url is None and old.rule_version is None
    engine.dispose()


def test_invalid_backup_does_not_clear_existing_words(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'invalid-words-backup.db'}")
    ViolationWord.__table__.create(engine)
    with Session(engine) as db:
        db.add(ViolationWord(word="keep me", normalized="keep me", action="remove"))
        db.commit()

        with pytest.raises(errors.AppError):
            word_service.import_words(
                db,
                {"format": "aohuang.violation-words", "version": 1,
                 "items": [{"word": "duplicate", "action": "remove", "source": "manual",
                            "enabled": True, "createdAt": 1},
                           {"word": "DUPLICATE", "action": "review", "source": "manual",
                            "enabled": True, "createdAt": 2}]},
                mode="replace",
            )
        assert db.query(ViolationWord).filter_by(normalized="keep me").one()
    engine.dispose()


def test_nfkc_full_width_title_matches_half_width_word() -> None:
    words = [("nike 2024", "remove")]
    ordinary_title = "Nike-2024 running shoes"
    full_width_title = "Ｎ-ＩＫＥ－２０２４ running shoes"

    assert word_service.matched_words(ordinary_title, words) == word_service.matched_words(
        full_width_title, words
    ) == [("nike 2024", "remove")]
    assert word_service.clean_title(ordinary_title, words) == word_service.clean_title(
        full_width_title, words
    ) == ("running shoes", ["nike 2024"])


def test_nfkc_full_width_word_matches_half_width_title() -> None:
    words = [("ＮＩＫＥ－２０２４", "remove")]
    title = "Nike 2024 running shoes"

    assert word_service.matched_words(title, words) == [("ＮＩＫＥ－２０２４", "remove")]
    assert word_service.clean_title(title, words) == ("running shoes", ["ＮＩＫＥ－２０２４"])


@pytest.mark.parametrize("variant", ["free shipping", "free-shipping", "free---shipping"])
def test_direct_title_save_rejects_separator_variants(variant: str, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "services.account_service", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "services.title_service",
        SimpleNamespace(review_hits=lambda title, pairs: []),
    )

    with pytest.raises(errors.AppError) as excinfo:
        title_save_service.save_title(
            None,
            account_id=1,
            detail_id=1,
            shop_id=1,
            title=f"New handmade {variant} shoe product for daily wear",
            confirm=True,
            rules={"free shipping": "remove"},
        )

    assert "待清理词" in excinfo.value.message
    assert excinfo.value.details["removedWords"] == ["free shipping"]
