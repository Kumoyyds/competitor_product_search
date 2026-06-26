from src.search.layers.numeric import compare_numerics, extract_numerics
from src.search.models import Verdict


def test_extract_count_x_volume():
    out = extract_numerics("MAGIC ROCK SAUCERY 4 X 330ML (ABV 3.9%)")
    assert out.get("count") == 4.0
    assert out.get("volume_ml") == 330.0
    assert abs(out.get("abv_percent", 0.0) - 3.9) < 1e-6


def test_extract_abv_zero():
    out = extract_numerics("SAN MIGUEL 0.0 12 X 330ML (ABV 0%)")
    assert out.get("count") == 12.0
    assert out.get("abv_percent") == 0.0


def test_extract_kg_normalizes_to_g():
    out = extract_numerics("FLOUR BAG 1.5 KG")
    assert out.get("weight_g") is not None
    assert abs(out["weight_g"] - 1500.0) < 1.0


def test_compare_discrete_conflict():
    a = {"storage_gb": 128.0}
    b = {"storage_gb": 256.0}
    assert compare_numerics(a, b) == Verdict.FAIL


def test_compare_continuous_within_tolerance():
    # 500g vs 0.5kg
    a = {"weight_g": 500.0}
    b = {"weight_g": 500.0}
    assert compare_numerics(a, b) == Verdict.PASS


def test_compare_continuous_out_of_tolerance():
    a = {"weight_g": 500.0}
    b = {"weight_g": 650.0}
    assert compare_numerics(a, b) == Verdict.FAIL


def test_compare_no_shared_keys_is_unknown():
    a = {"volume_ml": 330.0}
    b = {"weight_g": 500.0}
    assert compare_numerics(a, b) == Verdict.UNKNOWN


def test_compare_one_side_empty_is_unknown():
    assert compare_numerics({}, {"volume_ml": 330.0}) == Verdict.UNKNOWN
    assert compare_numerics({"volume_ml": 330.0}, {}) == Verdict.UNKNOWN
