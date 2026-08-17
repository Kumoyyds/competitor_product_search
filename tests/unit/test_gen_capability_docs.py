from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import scripts.gen_capability_docs as capability_docs
from scripts.gen_capability_docs import (
    CountryColumn,
    build_blocks,
    collapse_country_rows,
    collect_countries,
    collect_websites,
    inject_generated_blocks,
    render_countries_table,
    render_websites_table,
)


ROOT = Path(__file__).resolve().parents[2]


def test_country_collector_discovers_new_provider_column(tmp_path):
    (tmp_path / "alpha.py").write_text(
        '_COUNTRY_TO_REGION: dict[str, str] = {"uk": "uk-en", "de": "de-de"}\n',
        encoding="utf-8",
    )
    (tmp_path / "third_engine.py").write_text(
        '_COUNTRY_TO_MARKET = {"uk": "GB", "de": "DE"}\n',
        encoding="utf-8",
    )

    columns = collect_countries(tmp_path)

    assert [(column.module, column.parameter) for column in columns] == [
        ("alpha", "region"),
        ("third_engine", "market"),
    ]
    assert columns[1].values["de"] == "DE"


def test_aliases_collapse_and_missing_values_render_as_dash(monkeypatch):
    columns = [
        CountryColumn("duckduckgo", "region", {"uk": "uk-en", "gb": "uk-en", "kr": "kr-ko"}),
        CountryColumn("serper", "gl", {"uk": "gb", "gb": "gb"}),
    ]
    warnings = []
    monkeypatch.setattr(capability_docs, "warn", warnings.append)

    rows = collapse_country_rows(columns, {"uk": "United Kingdom", "gb": "United Kingdom"})
    table = render_countries_table(columns, rows)

    assert rows[0].codes == ("uk", "gb")
    assert "`uk` / `gb`" in table
    assert "| `kr` | — | `kr-ko` | — |" in table
    assert warnings == ["country code 'kr' is missing from COUNTRY_NAMES"]


def test_injection_is_idempotent():
    source = (
        "before <!-- BEGIN GENERATED: countries-inline -->old"
        "<!-- END GENERATED: countries-inline --> after"
    )
    blocks = {"countries-inline": "`uk`, `de`"}

    once = inject_generated_blocks(source, blocks)
    twice = inject_generated_blocks(once, blocks)

    assert once == twice
    assert "before <!-- BEGIN" in once
    assert "<!-- END GENERATED: countries-inline --> after" in once


def test_websites_come_only_from_domain_map(tmp_path):
    config_path = tmp_path / "search_config.yaml"
    config_path.write_text(
        "domain_map:\n  tesco: tesco.com\n  amazon.nl: amazon.nl\n",
        encoding="utf-8",
    )

    rows = collect_websites(config_path)

    assert rows == [("tesco", "tesco.com"), ("amazon.nl", "amazon.nl")]
    table = render_websites_table(rows)
    assert table.splitlines()[0] == "| `website` | Host kept by `domain_filter` |"
    assert "Retailer keyword" not in table


def test_real_readmes_are_fresh():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/gen_capability_docs.py"),
            "--check",
            "--root",
            str(ROOT),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_all_registered_blocks_can_be_built_from_real_sources():
    assert set(build_blocks(ROOT)) == {
        "countries-inline",
        "countries-table",
        "websites-inline",
        "websites-table",
        "llm-inline",
        "llm-table",
    }
