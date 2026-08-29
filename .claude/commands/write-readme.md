# Write README

Target: $ARGUMENTS

## Step 0: Determine target and mode

If `$ARGUMENTS` specifies a path (e.g. `src/search`), use that.
If `$ARGUMENTS` is empty, infer from current working directory:
- Inside a module subfolder → Module mode
- At project root → Root mode
- Unclear → ask the user

If `$ARGUMENTS` contains a hint after ` - ` (e.g. `src/search - added cache layer`),
focus analysis on that change and update only affected sections.

---

## Mode A: Module README → save to `<module_dir>/README.md`

Scan all files in the module. Identify: entry point, inputs, outputs, which files
call which others, and any files needing regular human maintenance (config, lookup
tables, data files with expiry).

Sections to write:
1. **What this does** — 2–3 sentences, include business context
2. **How it works** — 3–6 plain-language steps
3. **Workflow** — ASCII diagram of end-to-end flow
4. **Input** — table: what / type+format / source
5. **Output** — table: what / type+format / destination
6. **Script map** — ASCII diagram of file call/import relationships
7. **Files to maintain** — table: file / contents / update frequency (omit if none)

---

## Mode B: Root README → save to `README.md`

Scan `src/` subdirectories and the orchestrator/main/router file.
Understand what each module does and how they connect.

Sections to write:
1. **What this is** — 2–4 sentences, problem + status
2. **How it works** — high-level plain-language flow
3. **Architecture** — ASCII diagram of modules and data flow
4. **Modules** — table: module path / what it does
5. **Input & Output** — one line each
6. **Setup & Run** — commands from `pyproject.toml` / `uv.lock` / Makefile
7. **Files to maintain** — same as above (omit if none)

---

## ASCII diagram format

Use only: `[Name]` for components, `-->` for flow, `|` and `v` for vertical.
No Mermaid, no unicode boxes. Max 15 lines.

Example:
```
[scraper] --> [parser] --> [db_writer]
                 |
                 v
            [error_log]
```

---

## Rules

- If README exists: update section by section, preserve hand-written content
- If README doesn't exist: create from scratch
- Never invent details — write `(TODO: confirm)` if unclear
- Keep it short, one screen is better than scrolling
- After saving, tell the user the file path
