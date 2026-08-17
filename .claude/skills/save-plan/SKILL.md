---
name: save-plan
description: >
  Save a plan — the current plan-mode plan, or one just discussed/approved in
  conversation — as a persistent markdown file inside a directory the user
  names. Use whenever the user asks to save, persist, write out, or archive a
  plan into a repo location, e.g. "save this plan to docs/plans/", "save it as
  a plan in src/scraping/scripts/plans/", "write the plan out to X", "keep a
  copy of this plan in Y". Also use when they give a filename template with
  today's date and leave the rest to you ("name it yourself"). Not for writing
  a brand-new plan from scratch — only for persisting one that already exists
  in the conversation (plan-mode plan file or an agreed-upon plan) into a
  chosen directory.
---

# Save plan to a directory

Turn an in-conversation plan into a permanent file at a location the user
names, formatted to fit in with whatever's already there. The two things that
make this more than a file copy: figuring out *where the plan content comes
from*, and matching the *target directory's existing conventions* rather than
imposing a fixed template — repos are inconsistent about this (date-prefixed
filenames, plain descriptive names, English vs. the local language, section
headings) and a plan doc that doesn't match its neighbors sticks out.

## 1. Find the source content

- If a plan-mode plan file exists for this conversation (its path is in the
  plan-mode system reminders, e.g. `~/.claude/plans/<slug>.md`), that's the
  canonical content — read it.
- If plan mode was never entered, or the plan evolved after exiting it,
  reconstruct the plan from what was actually discussed/approved — don't
  just replay a stale plan-mode file if the conversation has since moved on.

## 2. Resolve the target directory

The user usually names it directly. Create it if it doesn't exist yet — but
if a similarly-named directory already exists elsewhere in the repo, check
you're not about to fork the convention by creating a near-duplicate.

## 3. Learn the convention before naming the file

List what's already in the target directory, and read at least one existing
file in full (not just its filename) if any exist. Look for:

- **Filename shape**: date-prefixed (`MMDD_description.md`,
  `YYYY-MM-DD-description.md`) vs. plain descriptive, snake_case vs.
  kebab-case, whether a `plan_`/"Plan:" marker is used.
- **Document shape**: section headings used (e.g. `## Context` / `## Approach`
  / `## Verification`), heading depth, language (repos are sometimes
  mixed-language across docs — don't force a translation just because you
  default to one language).

If the directory is empty or new, don't invent a convention from nothing —
check sibling doc directories in the same repo first, and only fall back to a
plain `<short-description>.md` if nothing else in the repo suggests a pattern.

## 4. Pick the filename

If the user gave an explicit naming hint (e.g. today's date plus a
placeholder word, or an f-string-shaped template), honor the concrete parts
literally (date, position, extension) and use judgment on any placeholder —
replace it with a short, specific description of what the plan is actually
about, matching the case style found in step 3, not the placeholder word
itself. Check the name isn't already taken in that directory; if it is,
sharpen the description rather than overwriting.

## 5. Match content to the target's style, don't restructure needlessly

Most plan-mode plans already carry a Context → Approach → Critical files →
Verification shape, which is a reasonable default if the target directory has
no strong opinion of its own. If existing docs in the target directory use
different section names or a different language, prefer matching them over
your own default — consistency with neighbors matters more than a
"canonical" template.

## 6. Write and confirm

Write the file (it's new, even when the content is copied from an existing
plan-mode file — don't try to edit/move the original). Confirm with one line
and a relative-path link to the new file; don't re-paste the plan content
back into the conversation.

## This is a documentation step, not a go-ahead

Saving the plan as a file is not the same as approval to implement it. Only
start on the plan's actual steps if the user separately asks for that —
saving it is often just a "keep a record" or "let the team see this" action
on its own.
