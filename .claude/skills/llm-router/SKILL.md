---
name: llm-router
description: Add name-only LLM switching to a module that calls an LLM. Use when the user wants to "switch LLM by name", "make the model swappable", "add LLM routing", "route model to base_url/key", or is manually editing base_url/API-key-env alongside a model name to change providers. Takes a target module path and optionally a config file + section key.
---

# LLM Router

Install a keyword-routing layer so that switching LLM vendor/model becomes a
**single line edit** (the model name) instead of touching base_url, API key
env var, and Python source together.

Args form: `<module path> [<config file>] [<section key>]`
Example: `src/search maintain/search_config.yaml llm`

## Step 0 — Resolve arguments

If config file or section key are omitted, discover them by grepping the
target module for `base_url`, `os.getenv(.*KEY)`, `ChatOpenAI`, `OpenAI(`,
`model=`. If still ambiguous after that, ask the user rather than guessing.

## Step 1 — Locate every LLM construction site

Grep the target module for the patterns above. There may be more than one
site (e.g. multiple layers/functions each building a client). List every
site found to the user before editing any of them.

## Step 2 — Decide where `llm_router_config.yaml` goes

Priority order, first match wins:
1. An existing human-maintained config folder inside the module (e.g.
   `maintain/`, `config/`).
2. Module root, next to the module's existing yaml loader.
3. A shared location (e.g. `src/common/`) if two or more modules already
   call LLMs and would benefit from one shared router file.

**You must announce the chosen path and why, to the user, at the end of the
run** — this decision is made autonomously but always needs a report-back.

## Step 3 — Write `llm_router_config.yaml`

Human-maintained routing table. Keys are **recognition keywords, not full
model names** — so e.g. `deepseek-v4-flash` and `deepseek-v4-pro` both route
through one `deepseek` entry without needing every model id listed.

```yaml
# Human-maintained. Add an entry when you introduce a new LLM vendor.
# Key = recognition keyword matched (case-insensitive substring) against the
# model name in <section>.model. Longest matching keyword wins.
providers:
  qwen:
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    key_name: QWEN_KEY        # name of the variable in .env
  deepseek:
    base_url: https://api.deepseek.com/v1
    key_name: DEEPSEEK_KEY
```

Never put API key *values* here — only `key_name` pointing at a variable in
`.env`.

## Step 4 — Add the resolver

Place a small resolver function next to the module's existing yaml loader.
Reuse that loader's existing style/pattern (e.g. `@lru_cache` + a module-
relative `_CONFIG_PATH`) — do not invent a second loading convention inside
the same module.

```python
def resolve_llm_route(model: str) -> tuple[str, str]:
    """Return (base_url, api_key) for a model name via keyword routing."""
```

Behavior:
- Case-insensitive substring match of each keyword against `model`; longest
  matching keyword wins on ties.
- No keyword matches → `RuntimeError` naming the model, the router file
  path, and the available keywords.
- Keyword matches but `os.getenv(key_name)` is empty/unset → `RuntimeError`
  naming the missing env var and the router file path.
- Call `load_dotenv()` once, at resolver level.
- Cache the parsed yaml the same way the sibling config loader does.

## Step 5 — Rewire the call site

The `<section>` in the module's own behavioral config keeps only knobs that
aren't routing — model name, temperature, timeout, etc. `base_url` and any
hardcoded `provider` key are deleted from it:

```yaml
llm:
  model: qwen-flash      # <- the only line to change when switching LLMs
  temperature: 0.1
  timeout_s: 60
```

The construction site becomes:

```python
base_url, api_key = resolve_llm_route(config.get("llm", "model"))
```

**Preserve existing test seams.** If tests patch a specific function name
(e.g. `_get_llm`), that function must keep its name and signature — only its
internals change.

## Step 6 — Update docs

- Module `README.md` — "how to switch LLM" instructions and the config-file
  table.
- Module `CLAUDE.md` (or `AGENTS.md`) — config-knob table: drop `base_url`
  from the section's row, add the router file with its path.
- Root `README.md` / `CLAUDE.md` — "Config files" table gets an
  `llm_router_config.yaml` row.
- `.env.sample` — ensure every `key_name` referenced in the router file has
  a corresponding line.
- If `CLAUDE.md` and `AGENTS.md` are kept in sync by a pre-commit hook in
  this repo, edit only one side and let the hook mirror it — never hand-edit
  both into potentially divergent states.

## Step 7 — Report back

Tell the user:
- The router file's path and why that location was chosen (Step 2).
- The one line to edit to switch models.
- How to add a new vendor (one new entry in the router file + one line in
  `.env.sample`, no code change).

## Rules

- Never put API key values in the router yaml — only `key_name`.
- Never hard-code a vendor name in Python after the refactor is applied.
- The router file is human-maintained: say so in a header comment inside it,
  and list it in the module's "Files to maintain" documentation.
- If the target module has no yaml loader at all, create one mirroring the
  nearest existing example in the repo (module-relative path, cached load,
  simple `get()` accessor) rather than inventing a new convention.
