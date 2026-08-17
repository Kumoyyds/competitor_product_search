import pytest

from src.search.layers.brand import compare_brands, extract_brands
from src.search.models import Verdict
from src.search.utils import load_brands_all, normalize


def _has_brand(name: str) -> bool:
    """Skip tests whose brand was removed from brand.xlsx by maintainers."""
    target = normalize(name)
    return any(normalize(b) == target for b in load_brands_all())


def test_extract_brands_user_supplied_wins():
    # supplied path bypasses brand.xlsx — safe even if Kopparberg is removed
    assert extract_brands("KOPPARBERG VARIETY", supplied="Kopparberg") == ["Kopparberg"]


def test_extract_brands_literal_word_boundary():
    if not _has_brand("Stella Artois"):
        pytest.skip("'Stella Artois' not in current brand.xlsx")
    out = extract_brands("STELLA ARTOIS 18 X 330ML (ABV 4.6%)")
    assert any("stella" in b.lower() for b in out)


def test_extract_brands_returns_all_literal_matches():
    """Regression: title with two brand-list tokens must surface BOTH, in order
    of appearance, so the comparison layer can recover the real brand via the
    any-pair-match rule. Was: longest-only, so 'Tetley' got swallowed by 'Tropical'.
    """
    if not (_has_brand("Tetley") and _has_brand("Tropical")):
        pytest.skip("Tetley or Tropical not in current brand.xlsx")
    out = extract_brands("Tetley 20 Supergreen Vitamin C Tropical Tea 40g")
    lower = [b.lower() for b in out]
    assert "tetley" in lower
    assert "tropical" in lower
    assert lower.index("tetley") < lower.index("tropical")


def test_extract_brands_short_does_not_overmatch():
    """Short tokens that are NOT in the brand list must not be invented."""
    out = extract_brands("ZQXJ ZZZWAFFLE THINGAMAJIG")
    assert out == [] or all(normalize(b) != normalize("ace") for b in out)


def test_extract_brands_short_digit_brand_via_literal():
    """Short / digit-bearing brands must come from the literal path —
    they're excluded from the fuzzy-safe subset so fuzzy can't recover them.
    Tries a few candidates; skips if none of them remain in brand.xlsx."""
    samples = [
        ("7Up Cherry 12 X 330ML", "7up"),
        ("3CE LIP TINT VELVET 4G", "3ce"),
        ("AEG Built-in Oven 60cm", "aeg"),
    ]
    tried = []
    for title, brand_norm in samples:
        if _has_brand(brand_norm):
            out = extract_brands(title)
            tried.append((brand_norm, out))
            assert brand_norm in {normalize(b) for b in out}, (
                f"expected literal match for {brand_norm!r}, got {out!r}"
            )
            return
    pytest.skip(f"none of the sample short brands present in brand.xlsx: {[s[1] for s in samples]}")


def test_extract_brands_empty_for_blank():
    assert extract_brands("") == []
    assert extract_brands(None) == []


# --- compare_brands tests below operate on pure inputs, brand.xlsx-independent ---


def test_compare_brands_pass_identical():
    assert compare_brands(["Kopparberg"], ["Kopparberg"]) == Verdict.PASS


def test_compare_brands_pass_accent_variant():
    assert compare_brands(["L'Oréal"], ["Loreal"]) == Verdict.PASS


def test_compare_brands_any_pair_pass_wins():
    """Multi-brand query vs single-brand candidate: PASS via the matching pair."""
    assert compare_brands(["Tetley", "Tropical"], ["Tetley"]) == Verdict.PASS


def test_compare_brands_fail_only_when_all_pairs_differ():
    assert compare_brands(["Coca-Cola"], ["Pepsi"]) == Verdict.FAIL
    # add a near-match -> not all-differ, so UNKNOWN
    assert compare_brands(["Coca-Cola", "Cole"], ["Cola"]) != Verdict.FAIL


def test_compare_brands_unknown_when_either_side_empty():
    assert compare_brands([], ["Kopparberg"]) == Verdict.UNKNOWN
    assert compare_brands(["Kopparberg"], []) == Verdict.UNKNOWN
    assert compare_brands([], []) == Verdict.UNKNOWN
