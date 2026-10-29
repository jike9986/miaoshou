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
