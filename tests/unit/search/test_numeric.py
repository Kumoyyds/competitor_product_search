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


def test_extract_storage_units():
    assert extract_numerics("SanDisk Ultra 64GB")["storage_gb"] == 64.0
    assert extract_numerics("SanDisk Ultra 64 GB")["storage_gb"] == 64.0
    assert extract_numerics("Samsung SSD 1TB")["storage_gb"] == 1000.0


def test_extract_ram_and_storage_from_same_title():
    out = extract_numerics("realme C75 8GB RAM + 128GB ROM")
    assert out["ram_gb"] == 8.0
    assert out["storage_gb"] == 128.0


def test_slash_capacity_keeps_larger_value_as_storage():
    out = extract_numerics('Samsung 6.5" Dual SIM 6GB/128GB')
    assert out["storage_gb"] == 128.0
    assert "ram_gb" not in out


def test_memory_card_is_storage_not_ram():
    out = extract_numerics("Nextorage 256GB Memory Card")
    assert out["storage_gb"] == 256.0
    assert "ram_gb" not in out


def test_extract_power_voltage_and_charge():
    out = extract_numerics("Duracell 30W charger, 3V, 6000mAh")
    assert out["power_w"] == 30.0
    assert out["voltage_v"] == 3.0
    assert out["charge_mah"] == 6000.0


def test_extract_screen_inches():
    assert extract_numerics('Samsung 55" TV')["screen_inch"] == 55.0
    assert extract_numerics("Dell 23.8 inch monitor")["screen_inch"] == 23.8
    assert extract_numerics("Dell display 23.8 in")["screen_inch"] == 23.8
    assert extract_numerics("Dell 23.8-in monitor")["screen_inch"] == 23.8


def test_in_preposition_is_not_screen_inches():
    assert "screen_inch" not in extract_numerics("Doll #3 in Barbiecore Outfit")
    assert "screen_inch" not in extract_numerics("Ninja Foodi 10 in 1 Air Fryer")


def test_extract_pack_variants():
    assert extract_numerics("Batteries 4 Pack")["count"] == 4.0
    assert extract_numerics("Batteries 4-pack")["count"] == 4.0
    assert extract_numerics("Batteries Pack of 4")["count"] == 4.0


def test_data_transfer_rate_is_not_storage():
    out = extract_numerics("Max Read Speed 300MB/s")
    assert "storage_gb" not in out


def test_model_numbers_are_not_attributes():
    assert extract_numerics("USB 3.0") == {}
    assert extract_numerics("iPhone 16") == {}


def test_imperial_mass_and_volume_units():
    out = extract_numerics("Bottle 12 fluid ounce, shipping weight 2 lb")
    assert abs(out["volume_ml"] - 354.882) < 0.01
    assert abs(out["weight_g"] - 907.184) < 0.01


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
