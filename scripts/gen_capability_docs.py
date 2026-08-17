#!/usr/bin/env python3
"""Generate README capability lists from provider code and maintained YAML.

The generated regions are deliberately narrow: prose outside the marker pairs
remains hand-written, while values that otherwise drift are replaced from their
real sources of truth.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only in dependency-poor hooks
    yaml = None


TAG = "gen-capability-docs"
MARKER_RE = re.compile(
    r"<!-- BEGIN GENERATED: (?P<id>[a-z0-9-]+) -->"
    r"(?P<body>.*?)"
    r"<!-- END GENERATED: (?P=id) -->",
    re.DOTALL,
)
KNOWN_BLOCKS = {
    "countries-inline",
    "countries-table",
    "websites-inline",
    "websites-table",
    "llm-inline",
    "llm-table",
}


def fail(message: str) -> None:
    print(f"[{TAG}] {message}", file=sys.stderr)
    raise SystemExit(1)


def warn(message: str) -> None:
    print(f"[{TAG}] warning: {message}", file=sys.stderr)


def repo_root(arg_root: Optional[str]) -> Path:
    if arg_root:
        return Path(arg_root).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail("not inside a git repository")
    return Path(result.stdout.strip()).resolve()


def run_git(root: Path, *args: str) -> int:
    return subprocess.run(["git", *args], cwd=root).returncode


def stage(root: Path, paths: Sequence[Path]) -> None:
    relative = [str(path.relative_to(root)) for path in paths]
    if run_git(root, "add", "--", *relative) != 0:
        fail(f"git add failed for {relative}")


def _literal_assignment(path: Path, name: str) -> Any:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        fail(f"cannot parse {path}: {exc}")

    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        if isinstance(target, ast.Name) and target.id == name and value is not None:
            try:
                return ast.literal_eval(value)
            except (ValueError, TypeError, SyntaxError) as exc:
                fail(f"{path}: {name} must be a literal mapping: {exc}")
    fail(f"{path}: missing module-level assignment {name}")


def _country_assignments(path: Path) -> list[tuple[str, dict[str, str]]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        fail(f"cannot parse {path}: {exc}")

    found: list[tuple[str, dict[str, str]]] = []
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        if not (
            isinstance(target, ast.Name)
            and target.id.startswith("_COUNTRY_TO_")
            and value is not None
        ):
            continue
        try:
            mapping = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError) as exc:
            fail(f"{path}: {target.id} must be a literal mapping: {exc}")
        if not isinstance(mapping, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in mapping.items()
        ):
            fail(f"{path}: {target.id} must map strings to strings")
        found.append((target.id, mapping))
    return found


def _provider_name(module_stem: str) -> str:
    special = {"duckduckgo": "DuckDuckGo"}
    return special.get(module_stem, module_stem.replace("_", " ").title())


@dataclass(frozen=True)
class CountryColumn:
    module: str
    parameter: str
    values: Mapping[str, str]

    @property
    def heading(self) -> str:
        return f"{_provider_name(self.module)} `{self.parameter}`"


@dataclass(frozen=True)
class CountryRow:
    codes: tuple[str, ...]
    name: str
    values: tuple[str | None, ...]


def collect_countries(providers_dir: Path) -> list[CountryColumn]:
    columns: list[CountryColumn] = []
    for path in sorted(providers_dir.glob("*.py")):
        for variable, mapping in _country_assignments(path):
            parameter = variable.removeprefix("_COUNTRY_TO_").lower()
            columns.append(CountryColumn(path.stem, parameter, mapping))
    if not columns:
        fail(f"no module-level _COUNTRY_TO_* mappings found in {providers_dir}")
    return columns


def collect_country_names(path: Path) -> dict[str, str]:
    names = _literal_assignment(path, "COUNTRY_NAMES")
    if not isinstance(names, dict) or not all(
        isinstance(code, str) and isinstance(name, str)
        for code, name in names.items()
    ):
        fail(f"{path}: COUNTRY_NAMES must map strings to strings")
    return names


def collapse_country_rows(
    columns: Sequence[CountryColumn], names: Mapping[str, str]
) -> list[CountryRow]:
    ordered_codes: list[str] = []
    for column in columns:
        for code in column.values:
            if code not in ordered_codes:
                ordered_codes.append(code)

    grouped: dict[tuple[tuple[str | None, ...], str], list[str]] = {}
    missing_names: list[str] = []
    for code in ordered_codes:
        values = tuple(column.values.get(code) for column in columns)
        name = names.get(code)
        if name is None:
            missing_names.append(code)
            # The code is part of the key so unrelated unnamed countries never
            # collapse merely because their provider mappings happen to match.
            grouping_name = f"__missing__:{code}"
        else:
            grouping_name = name
        grouped.setdefault((values, grouping_name), []).append(code)

    for code in missing_names:
        warn(f"country code {code!r} is missing from COUNTRY_NAMES")

    return [
        CountryRow(tuple(codes), names.get(codes[0], "—"), values)
        for (values, _grouping_name), codes in grouped.items()
    ]


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is not installed")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        fail(f"cannot read {path}: {exc}")
    if not isinstance(data, dict):
        fail(f"{path}: expected a top-level mapping")
    return data


def collect_websites(config_path: Path) -> list[tuple[str, str | None]]:
    config = _load_yaml(config_path)
    domains = config.get("domain_map") or {}
    if not isinstance(domains, dict):
        fail(f"{config_path}: domain_map must be a mapping")
    return [
        (
            str(code),
            None if domains.get(code) is None else str(domains[code]),
        )
        for code in domains
    ]


@dataclass(frozen=True)
class LlmCapabilities:
    vendors: tuple[tuple[str, str, str], ...]
    active_model: str
    active_vendor: str | None


def collect_llm_vendors(router_path: Path, search_config_path: Path) -> LlmCapabilities:
    router = _load_yaml(router_path)
    config = _load_yaml(search_config_path)
    raw_providers = router.get("providers") or {}
    if not isinstance(raw_providers, dict):
        fail(f"{router_path}: providers must be a mapping")

    vendors: list[tuple[str, str, str]] = []
    for keyword, raw in raw_providers.items():
        if not isinstance(raw, dict):
            fail(f"{router_path}: provider {keyword!r} must be a mapping")
        base_url = raw.get("base_url")
        key_name = raw.get("key_name")
        if not isinstance(base_url, str) or not isinstance(key_name, str):
            fail(f"{router_path}: provider {keyword!r} needs base_url and key_name")
        vendors.append((str(keyword), base_url, key_name))

    active_model = (config.get("llm") or {}).get("model")
    if not isinstance(active_model, str):
        fail(f"{search_config_path}: llm.model must be a string")
    matches = [keyword for keyword, _url, _key in vendors if keyword.lower() in active_model.lower()]
    active_vendor = max(matches, key=len) if matches else None
    if active_vendor is None:
        warn(f"active llm.model {active_model!r} matches no configured vendor keyword")
    return LlmCapabilities(tuple(vendors), active_model, active_vendor)


def _code_list(codes: Sequence[str], *, inline_alias: bool = False) -> str:
    head, *aliases = codes
    rendered = f"`{head}`"
    if aliases:
        separator = " (= " if inline_alias else " / "
        rendered += separator + " / ".join(f"`{code}`" for code in aliases)
        if inline_alias:
            rendered += ")"
    return rendered


def _cell(value: str) -> str:
    return value.replace("|", "\\|")


def render_countries_inline(rows: Sequence[CountryRow]) -> str:
    return ", ".join(_code_list(row.codes, inline_alias=True) for row in rows)


def render_countries_table(
    columns: Sequence[CountryColumn], rows: Sequence[CountryRow]
) -> str:
    lines = [
        "| `country` | Country | " + " | ".join(column.heading for column in columns) + " |",
        "|---|---|" + "---|" * len(columns),
    ]
    for row in rows:
        values = [f"`{_cell(value)}`" if value is not None else "—" for value in row.values]
        lines.append(
            "| " + _code_list(row.codes) + f" | {_cell(row.name)} | " + " | ".join(values) + " |"
        )
    return "\n".join(lines)


def render_websites_inline(rows: Sequence[tuple[str, str | None]]) -> str:
    return ", ".join(f"`{code}`" for code, _domain in rows)


def _domain_cell(domain: str | None) -> str:
    if domain is None:
        return "—"
    if domain.endswith("."):
        return f"`{_cell(domain)}` (registrable prefix; any TLD)"
    return f"`{_cell(domain)}` (plus subdomains)"


def render_websites_table(rows: Sequence[tuple[str, str | None]]) -> str:
    lines = [
        "| `website` | Host kept by `domain_filter` |",
        "|---|---|",
    ]
    for code, domain in rows:
        lines.append(f"| `{_cell(code)}` | {_domain_cell(domain)} |")
    return "\n".join(lines)


def render_llm_inline(capabilities: LlmCapabilities) -> str:
    vendors = ", ".join(f"`{keyword}`" for keyword, _url, _key in capabilities.vendors)
    route = f" via `{capabilities.active_vendor}`" if capabilities.active_vendor else " (unrouted)"
    return f"{vendors}; active model: `{capabilities.active_model}`{route}"


def render_llm_table(capabilities: LlmCapabilities) -> str:
    route = (
        f"routed through `{capabilities.active_vendor}`"
        if capabilities.active_vendor
        else "not matched to a configured vendor"
    )
    lines = [
        f"Active model: `{capabilities.active_model}` ({route}).",
        "",
        "| Routing keyword | Base URL | Required `.env` key |",
        "|---|---|---|",
    ]
    for keyword, base_url, key_name in capabilities.vendors:
        lines.append(f"| `{_cell(keyword)}` | `{_cell(base_url)}` | `{_cell(key_name)}` |")
    return "\n".join(lines)


def build_blocks(root: Path) -> dict[str, str]:
    providers_dir = root / "src/search/providers"
    columns = collect_countries(providers_dir)
    names = collect_country_names(providers_dir / "countries.py")
    country_rows = collapse_country_rows(columns, names)
    search_config = root / "src/search/maintain/search_config.yaml"
    websites = collect_websites(search_config)
    llms = collect_llm_vendors(
        root / "src/search/maintain/llm_router_config.yaml",
        search_config,
    )
    return {
        "countries-inline": render_countries_inline(country_rows),
        "countries-table": "\n" + render_countries_table(columns, country_rows) + "\n",
        "websites-inline": render_websites_inline(websites),
        "websites-table": "\n" + render_websites_table(websites) + "\n",
        "llm-inline": render_llm_inline(llms),
        "llm-table": "\n" + render_llm_table(llms) + "\n",
    }


def inject_generated_blocks(text: str, blocks: Mapping[str, str], source: str = "document") -> str:
    matches = list(MARKER_RE.finditer(text))
    unknown = sorted({match.group("id") for match in matches} - set(blocks))
    if unknown:
        fail(f"{source}: unknown generated block id(s): {', '.join(unknown)}")
    duplicates = sorted(
        block_id
        for block_id in {match.group("id") for match in matches}
        if sum(match.group("id") == block_id for match in matches) > 1
    )
    if duplicates:
        fail(f"{source}: duplicate generated block id(s): {', '.join(duplicates)}")

    def replace(match: re.Match[str]) -> str:
        block_id = match.group("id")
        return (
            f"<!-- BEGIN GENERATED: {block_id} -->"
            f"{blocks[block_id]}"
            f"<!-- END GENERATED: {block_id} -->"
        )

    return MARKER_RE.sub(replace, text)


def update_docs(root: Path, blocks: Mapping[str, str], check: bool, pre_commit: bool) -> int:
    targets = [root / "README.md", root / "src/search/README.md"]
    changed: list[Path] = []
    seen_ids: set[str] = set()
    replacements: dict[Path, str] = {}
    for path in targets:
        if not path.exists():
            fail(f"missing documentation target {path}")
        original = path.read_text(encoding="utf-8")
        seen_ids.update(match.group("id") for match in MARKER_RE.finditer(original))
        rendered = inject_generated_blocks(original, blocks, str(path.relative_to(root)))
        if rendered != original:
            changed.append(path)
            replacements[path] = rendered

    missing = sorted(KNOWN_BLOCKS - seen_ids)
    if missing:
        warn(f"generated block id(s) absent from both READMEs: {', '.join(missing)}")

    if check:
        if changed:
            for path in changed:
                print(f"[{TAG}] stale: {path.relative_to(root)}", file=sys.stderr)
            return 1
        print(f"[{TAG}] OK: generated README regions are current")
        return 0

    for path in changed:
        path.write_text(replacements[path], encoding="utf-8")
        print(f"[{TAG}] rewrote {path.relative_to(root)}")
    if pre_commit and changed:
        stage(root, changed)
        print(f"[{TAG}] staged {len(changed)} generated documentation file(s)")
    if not changed:
        print(f"[{TAG}] no documentation changes")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="git repo root (default: git rev-parse)")
    parser.add_argument(
        "--pre-commit",
        action="store_true",
        help="rewrite and stage changed README files",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when generated README regions are stale; write nothing",
    )
    args = parser.parse_args()
    if args.pre_commit and args.check:
        fail("--pre-commit and --check are mutually exclusive")
    if yaml is None:
        warn("PyYAML is not installed; skipping capability documentation generation")
        raise SystemExit(0)

    root = repo_root(args.root)
    raise SystemExit(update_docs(root, build_blocks(root), args.check, args.pre_commit))


if __name__ == "__main__":
    main()
