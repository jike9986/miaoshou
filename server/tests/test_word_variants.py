from services import word_service


def test_removal_matches_spaces_and_inserted_hyphens() -> None:
    cleaned, removed = word_service.clean_title(
        "New N-i-k-e Air free---shipping shoes",
        [("nike", "remove"), ("free shipping", "remove")],
    )

    assert cleaned == "New Air shoes"
    assert removed == ["free shipping", "nike"]


def test_ascii_word_boundaries_still_protect_unrelated_words() -> None:
    cleaned, removed = word_service.clean_title(
        "freedom shoes", [("free", "remove")],
    )

    assert cleaned == "freedom shoes"
    assert removed == []


def test_review_hits_use_the_same_variant_matching_rule() -> None:
    hits = word_service.matched_words(
        "Adidas---Originals", [("adidas originals", "review")],
    )

    assert hits == [("adidas originals", "review")]


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
