import pytest

from src.search.cache import BaseExtractionCache
from src.search.layers import numeric
from src.search.layers.numeric import compare_numerics, extract_numerics
from src.search.models import BaseAttributes, Verdict


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


@pytest.mark.parametrize(
    ("title", "key", "expected"),
    [
        ("Bottle 1,5 l", "volume_ml", 1500.0),
        ("Bag 2,5 kg", "weight_g", 2500.0),
        ("Can 0,33 l", "volume_ml", 330.0),
        ("Battery 1,5V", "voltage_v", 1.5),
        ("Jug 4,1 liter", "volume_ml", 4100.0),
        ("Sauce 2,27 Kg", "weight_g", 2270.0),
        ("Bag 1,000 g", "weight_g", 1000.0),
    ],
)
def test_decimal_comma_and_english_thousands_separator(title, key, expected):
    assert extract_numerics(title)[key] == pytest.approx(expected)


def test_decimal_comma_count_x_patterns():
    assert extract_numerics("Sauce 6x2,27kg") == {
        "count": 6.0,
        "weight_g": 2270.0,
    }
    assert extract_numerics("Drinks 24 x 0,33 l") == {
        "count": 24.0,
        "volume_ml": 330.0,
    }


def test_thousands_dot_is_country_and_unit_gated():
    assert extract_numerics("Bag 1.000 g", country="de")["weight_g"] == 1000.0
    assert extract_numerics("Bag 1.000 g", country="uk")["weight_g"] == 1.0
    assert extract_numerics("USB 3.0", country="de") == {}
    assert extract_numerics("Nikon 1.4", country="de") == {}


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Batterien 24er Pack", 24.0),
        ("Batterien 6er-Pack", 6.0),
        ("Packung mit 6 Batterien", 6.0),
        ("Batterijen 4 stuks", 4.0),
        ("Doos van 12 flessen", 12.0),
        ("Lot de 6 bouteilles", 6.0),
        ("Paquet de 4 piles", 4.0),
    ],
)
def test_extract_multilingual_pack_keywords(title, expected):
    assert extract_numerics(title)["count"] == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [("Fernseher 55 Zoll", 55.0), ("Moniteur 27 pouces", 27.0)],
)
def test_extract_multilingual_screen_keywords(title, expected):
    assert extract_numerics(title)["screen_inch"] == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Bier 4,9% vol", 4.9),
        ("Vin 13,5% vol", 13.5),
        ("Bier Alk. 5,0%", 5.0),
        ("Bier 5,0 vol.%", 5.0),
    ],
)
def test_extract_multilingual_abv_keywords(title, expected):
    assert extract_numerics(title)["abv_percent"] == expected


def test_unknown_foreign_unit_words_do_not_create_attributes():
    assert extract_numerics("500 Gramm") == {}
    assert extract_numerics("2 Kilogramm") == {}
    assert extract_numerics("stuks") == {}


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Bottle 500ml", 500.0),
        ("Bottle 500 ml", 500.0),
        ("Bottle 500Ml", 500.0),
        ("Bottle 500ML", 500.0),
        ("Bottle 15ml", 15.0),
    ],
)
def test_extract_ml_in_all_letter_cases(title, expected):
    assert extract_numerics(title)["volume_ml"] == expected


def test_conversion_fallback_tries_every_unit_symbol(monkeypatch):
    class Entity:
        name = "volume"

    class Unit:
        entity = Entity()
        name = "unmapped volume unit"
        symbols = ["cc", "ml"]

    class Quantity:
        value = 500.0
        unit = Unit()
        span = (0, 5)

    class Parser:
        @staticmethod
        def parse(_text):
            return [Quantity()]

    monkeypatch.setattr(numeric, "_get_parser", lambda: Parser())
    assert extract_numerics("500ml") == {"volume_ml": 500.0}


@pytest.mark.parametrize(
    ("title", "key", "expected"),
    [
        ("Sweets 157G", "weight_g", 157.0),
        ("Sweets 85 G", "weight_g", 85.0),
        ("Sweets 130G", "weight_g", 130.0),
        ("Drive 128Gb", "storage_gb", 128.0),
        ("Drive 100mb", "storage_gb", 0.1),
        ("Drive 100Tb", "storage_gb", 100000.0),
        ("Bolt 100MM", "length_mm", 100.0),
    ],
)
def test_normalizes_misresolved_unit_symbol_case(title, key, expected):
    assert extract_numerics(title)[key] == expected


def test_network_generation_and_lens_guards_do_not_create_weight():
    galaxy = extract_numerics("Samsung Galaxy A25 5G 128GB")
    assert galaxy["storage_gb"] == 128.0
    assert "weight_g" not in galaxy

    assert "weight_g" not in extract_numerics("Samsung A25 5G")
    assert "weight_g" not in extract_numerics("Nikon 1.4G lens")

    lens = extract_numerics("AF-S 50mm f/1.8G")
    assert lens["length_mm"] == 50.0
    assert "weight_g" not in lens


def test_part_number_guard_does_not_create_weight():
    out = extract_numerics("SanDisk 64GB Ultra USB 3.0 - SDCZ48-064G-")
    assert out["storage_gb"] == 64.0
    assert "weight_g" not in out


@pytest.mark.parametrize(
    ("title", "key", "expected"),
    [
        ("Bag 2.5KG", "weight_g", 2500.0),
        ("Bottle 75CL", "volume_ml", 750.0),
        ("Battery 1.5V", "voltage_v", 1.5),
        ("Drive 128GB", "storage_gb", 128.0),
        ("TV 65 INCH", "screen_inch", 65.0),
    ],
)
def test_existing_case_correct_unit_forms_are_unchanged(title, key, expected):
    assert extract_numerics(title)[key] == expected


@pytest.mark.parametrize(
    ("query", "candidate"),
    [
        ("Pilsner Urquell 500ml", "Pilsner Urquell 500Ml"),
        ("Wine Gums Juicies 130g", "Wine Gums Sweets Bag 130G"),
    ],
)
def test_cross_case_numeric_comparison_passes(query, candidate):
    assert compare_numerics(
        extract_numerics(query), extract_numerics(candidate)
    ) == Verdict.PASS


@pytest.mark.parametrize(
    ("query", "candidate"),
    [
        ("Oyster Sauce 2.27kg", "Oestersaus - 2,27 Kg"),
        ("Coca-Cola 1.5L", "Coca-Cola Zero 1,5 l fles"),
        ("Varta AA 1.5V", "Varta Batterien 1,5V AA"),
    ],
)
def test_cross_locale_numeric_comparison_passes(query, candidate):
    assert compare_numerics(
        extract_numerics(query), extract_numerics(candidate)
    ) == Verdict.PASS


def test_base_extraction_cache_is_partitioned_by_country(tmp_path):
    cache = BaseExtractionCache(str(tmp_path / "base.sqlite"))
    de_attrs = BaseAttributes(numerics={"weight_g": 1000.0})
    uk_attrs = BaseAttributes(numerics={"weight_g": 1.0})

    cache.set("Bag 1.000 g", de_attrs, country="DE")
    assert cache.get("Bag 1.000 g", country="de") == de_attrs
    assert cache.get("Bag 1.000 g", country="uk") is None

    cache.set("Bag 1.000 g", uk_attrs, country="uk")
    assert cache.get("Bag 1.000 g", country="de") == de_attrs
    assert cache.get("Bag 1.000 g", country="UK") == uk_attrs


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
