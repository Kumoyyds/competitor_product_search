# Fix unit-symbol resolution in numeric extraction (`ml` drop + case sensitivity)

## Context

`numeric` is a hard kill-gate before the LLM ([base_match.py:47-51](src/search/layers/base_match.py#L47-L51)),
and today it is silently inert for a large share of real titles. Investigating run
`1b82f20e73ee4c4e8a7b8e388a0aed72` (task 17, `Pilsner Urquell 500ml` matched against
`Pilsner Urquell 500Ml`, `numeric: unknown`) surfaced two independent defects.

Measured over all 1455 candidate titles in that run: **404 titles contain a unit-looking
token, and 90 of them (22%) yield `{}` from `extract_numerics`.** Verdict counts for the whole
run were `pass 20 / fail 14 / unknown 211 / (never reached) 1210` — the layer is mostly
abstaining when it should be deciding.

### Defect A — every `ml` volume is dropped, in *all* letter cases

quantulum3 resolves `500ml` to the canonical unit name **`cubic centimetre`** (mL ≡ cc in its
ontology), which is not a key in `unit_conversions.volume_ml`. The symbol fallback at
[numeric.py:283-291](src/search/layers/numeric.py#L283-L291) then tries only `symbols[0]`,
which is `cc` — also absent. The symbol list is `['cc', 'ccm', 'mL', 'ml']`, so the working
key `ml` sits at index 3 and is never tried.

```
'500ml' -> {}    'mL' -> {}    'Ml' -> {}    'ML' -> {}
quantulum: (500.0, 'cubic centimetre', 'volume', symbols=['cc','ccm','mL','ml'])
```

This is **not** a case problem — lowercase fails too. It accounts for ~39 of the 90 misses
(`500ml` x17, `15ml` x12, `250ml`, `480ml`, `175ml`, `570 Ml`, …).

### Defect B — genuine case sensitivity, but only for four symbols

quantulum3's symbol table is case-sensitive and inconsistent. A sweep of every symbol in
`unit_conversions` shows `KG`/`Kg`/`CL`/`OZ`/`LB`/`MG`/`V`/`W`/`GB`/`TB`/`CM`/`INCH` all work,
while four resolve to a **different entity**:

| surface | quantulum resolves to | result |
|---|---|---|
| `130G` | `gauss` / *magnetic field* | `{}` — entity unmapped |
| `128Gb`, `100gB` | `giga base pair` / *length* | `{}` — conversion fails |
| `100mb` | `millibarn` / *length* | `{}` (but `MB` works) |
| `100MM` | `unk` / *dimensionless* | `{}` |

`G` dominates: ~35 of the 90 misses (`157G` x6, `500G`, `454G`, `900G`, `85 G`, `130G`, …).
These are mis-recognitions, not misses — the value is real but lands in an entity we do not map.

### Why a blanket casefold is unsafe

`5G` / `4G` are network generations, and the run contains **22 such titles**
(`Samsung Galaxy A25 5G 128GB`, `realme C75 4G Smartphone`, …). Rewriting `G`→`g`
unconditionally yields `weight_g: 5`, a **wrong value** that turns into a hard `FAIL` —
the exact failure class the sibling plan `0813_numeric_extract_multi_lang.md` warns about.
`Nikon 1.4G` (lens aperture designation) is the same trap.

Two independent signals can separate the cases, and **both were measured on the corpus to be
individually perfect** (each: 28 network-generation tokens identified, 0 missed, 0 gram values
misread). They are combined as a union rather than picking one, because their failure modes are
disjoint:

| signal | catches | blind spot the other covers |
|---|---|---|
| value ∈ `{2,3,4,5}` (closed domain fact — no other generation exists) | `Samsung A25 5G` with no device vocabulary | `Nikon 1.4G` — 1.4 is not in the set |
| device context keywords (`phone`, `SIM`, `LTE`, `Galaxy`, `lens`, `AF-S`, …) | `Nikon 1.4G lens`, `AF-S 50mm f/1.8G` | a bare title carrying no device word |

The union is the correct bias for this layer specifically: `UNKNOWN` is free (the candidate is
passed to the LLM) while `FAIL` is destructive (the candidate is killed), so abstaining beats
guessing. The one accepted residual cost is a genuine 5-gram product written `5G` with no
device vocabulary (`Saffron 5G jar`) staying `unknown` — which is exactly today's behavior, so
nothing regresses.

### Intended outcome

Both defects fixed, with guards that are validated against the recorded corpus rather than
assumed. Simulated over the 245 candidates that actually reached the numeric layer:

```
unknown -> pass    17      (incl. 500ml/500Ml, 157G, 454G, 350G, 185G, 85 G)
unknown -> fail     9      (all genuinely different: 300g vs 250G, 500ML vs 2Ltr, 250ml vs 1 Litre)
pass    -> fail     0
fail    -> pass     0
```

Zero regressions; 26 of 245 abstentions become real decisions.

## Changes

### 1. `src/search/layers/numeric.py` — try every unit symbol, not just the first

In the quantulum loop ([numeric.py:281-293](src/search/layers/numeric.py#L281-L293)), replace the
`symbols[0]`-only fallback with a loop over all symbols, stopping at the first one that
converts. Keep the existing `try/except` tolerance for units without a `symbols` attribute.

```python
v = _convert(q.value, unit_name, attr)
if v is None:
    try:
        symbols = getattr(q.unit, "symbols", None) or []
    except Exception:
        symbols = []
    for symbol in symbols:
        v = _convert(q.value, symbol, attr)
        if v is not None:
            break
if v is None:
    continue
```

Measured in isolation over the corpus: **+47 `volume_ml` extractions, 0 changed or lost values.**

### 2. `src/search/layers/numeric.py` — unit-case normalization pre-pass

Add `_normalize_unit_case(text) -> str` alongside the existing
[`_normalize_separators`](src/search/layers/numeric.py#L109-L127), and call it inside
`extract_numerics` immediately after `_normalize_separators` (line 200) so both the regex
pre-passes and quantulum see canonical case. Build the pattern at module load from config
(change 3), matching the existing `_PACK_RE` / `_ABV_RE` convention of compiling once.

Shape:

```python
_UNIT_CASE_RE = re.compile(
    rf"(?<![\w.\-/])(\d+(?:\.\d+)?)(\s*)({_CASE_SYMBOL_ALTERNATION})(?![\w])"
)
```

Three guards, each backed by a measured false positive. Evaluate the device-context regex
**once per title**, not per match, so per-call cost stays negligible.

| guard | blocks | evidence in corpus |
|---|---|---|
| `(?<![\w.\-/])` before the number | part numbers, paths, versions | `SDCZ48-064G-`, `USB 3.0` |
| skip `G` when the number is in `network_generation_values` | network generation | 22 × `4G` / `5G` titles |
| skip `G` when a device keyword appears anywhere in the title | phones and camera lenses | `Nikon 1.4G lens`, `AF-S 50mm f/1.8G` |

The last two are ORed — either firing blocks the rewrite.

**Do not add a "number has a decimal point" guard.** It was considered and rejected: corpus gain
is `+35 weight_g` with or without it, so it has no supporting evidence, and the device-context
keyword already covers the `Nikon 1.4G` case it was invented for.

**Keep `4g` / `5g` out of the device keyword list.** With `\b` boundaries they match the tail of
`1.4G` and `2.5G`, silently reintroducing the rejected decimal guard under a different name.
Network generations are the value blacklist's job; the keyword list only answers "is this a
device listing".

Only the number-adjacent symbol is rewritten; the rest of the title is untouched.

### 3. `src/search/maintain/search_config.yaml` — declare the override table

Add under `numeric.locale` (after `abv_keywords`, [search_config.yaml:92](src/search/maintain/search_config.yaml#L92)),
matching the convention that adding a market or a keyword is a config edit:

```yaml
    # quantulum3's symbol table is case-sensitive and resolves these forms to the
    # wrong entity (G -> gauss, Gb -> giga base pair, mb -> millibarn, MM -> unk).
    # Rewritten to the canonical symbol before parsing. Keys are case-folded.
    unit_case_overrides:
      g: g
      gb: GB
      mb: MB
      mm: mm
      tb: TB
    # Numbers that mean a mobile network generation, not grams ("Galaxy A25 5G").
    network_generation_values: [2, 3, 4, 5]
    # A title containing any of these is a device listing, so a trailing "G" is a
    # network generation or a lens designation, never grams. Never add "4g"/"5g"
    # here — with word boundaries they also match the tail of "1.4G" / "2.5G".
    device_context_keywords:
      - phone
      - smartphone
      - mobile
      - sim
      - lte
      - android
      - galaxy
      - iphone
      - смартфон
      - lens
      - camera
      - af-s
      - nikkor
      - dslr
```

`tb` is included so `100Tb` resolves as terabyte explicitly rather than through today's
accidental `terabit` → `tb` symbol collision.

Also add the canonical name to `unit_conversions.volume_ml`
([search_config.yaml:106](src/search/maintain/search_config.yaml#L106)) so the primary path
works without depending on the change-1 fallback:

```yaml
      cubic centimetre: 1
```

### 4. `src/search/cache.py` — bump the extractor version

Extraction behavior changes, so bump `EXTRACTOR_VERSION` from `"3"` to `"4"`
([cache.py:13](src/search/cache.py#L13)) to make stale rows in `.cache/base_extraction.sqlite`
unreachable. This is the documented invariant in `src/search/CLAUDE.md`.

### 5. `tests/unit/search/test_numeric.py` — lock in both fixes

Follow the existing `pytest.mark.parametrize` style used by
[test_decimal_comma_and_english_thousands_separator](tests/unit/search/test_numeric.py#L92-L105).

Defect A (case-independent `ml`):
- `500ml`, `500 ml`, `500Ml`, `500ML` → `volume_ml: 500`; `15ml` → `15`

Defect B (case normalization):
- `157G`, `85 G`, `130G` → `weight_g` 157 / 85 / 130
- `128Gb` → `storage_gb: 128`; `100MM` → `length_mm: 100`

Guards (the tests that matter most — they prevent wrong values). Cover each guard *in isolation*
so the union does not mask a broken half:
- `Samsung Galaxy A25 5G 128GB` → `storage_gb: 128` and **no `weight_g`** (both guards fire)
- `Samsung A25 5G` → no `weight_g` (**value blacklist alone** — no device keyword present)
- `Nikon 1.4G lens`, `AF-S 50mm f/1.8G` → no `weight_g` (**context alone** — 1.4 / 1.8 are not
  in the blacklist). `f/1.8G` must still yield `length_mm: 50` from the `50mm`.
- `SanDisk 64GB Ultra USB 3.0 - SDCZ48-064G-` → no `weight_g` from the part number
- Existing case-correct forms unchanged: `2.5KG`, `75CL`, `1.5V`, `128GB`, `65 INCH`

Cross-case comparison, asserting the reported bugs are gone:
- `compare_numerics(E('Pilsner Urquell 500ml'), E('Pilsner Urquell 500Ml'))` → `PASS`
- `compare_numerics(E('… Wine Gums Juicies 130g'), E('… Sweets Bag 130G'))` → `PASS`

## Risks

The change converts abstentions into decisions at a hard kill-gate, so it makes the layer
*more* consequential. The simulation shows 9 new `FAIL`s on this corpus, all correct
(`300g` vs `250G`, `500ML` vs `2Ltr`, `250ml` vs `1 Litre`) — but any new `FAIL` on the first
real run should be spot-checked against its candidate title before the change is trusted.

Change 1 (iterating all symbols) is the less predictable of the two: a symbol could collide
with a different unit's key in the same conversion table. It measured 0 regressions here, and
the primary path is tried first, so a collision only surfaces where extraction produces
nothing today.

Change 2 is a surface heuristic, and worth naming as such: it infers meaning from the shape of
the text rather than from a real unit ontology. The `(?<![\w.\-/])` prefix guard generalizes
(a number glued to a preceding token is part of an identifier, not a measurement), and the
network-generation set is a closed domain fact rather than a fit to this corpus. But the device
keyword list is genuinely open-ended and will need entries as new categories appear — it belongs
in config for exactly that reason. Change 1 has none of this character: it is a plain lookup bug
and accounts for 47 of the 82 recovered attributes.

## Out of scope (found during investigation, worth separate tickets)

- **Candidate titles are being concatenated.** Task 51's stored `title` is seven titles glued
  together (`…130G - TescoMaynards Bassetts Wine Gums Sweets 130g - Tesco Groceries…`). This is
  why task 51 reads `numeric: pass` — a lowercase `130g` happened to appear later in the blob.
  Extraction quality is capped until this is fixed.
- **Per-unit vs total weight false FAIL.** `Costa Coffee … Sachets 6x17g` vs
  `Costa Coffee … 6 Sachets 102g` currently `FAIL`s, but 6 × 17 = 102 — the same product.

## Verification

1. `python -m pytest tests/unit/search/ -v` — offline, no API cost.
2. Replay the recorded corpus: read the 1455 candidate titles from `search_db.sqlite`
   (`candidates`, `run_id='1b82f20e73ee4c4e8a7b8e388a0aed72'`), run `extract_numerics` with
   each task's country, and diff old vs new. Expect ~+82 attributes gained
   (~47 `volume_ml`, ~35 `weight_g`) and **zero changed or lost existing values**.
3. Replay verdicts: recompute `compare_numerics(query, candidate)` for the 245 candidates that
   reached the layer. Expect `unknown→pass 17`, `unknown→fail 9`, and `pass→fail 0`,
   `fail→pass 0`. Any non-zero flip in the last two rows means a regression.
4. `python scripts/validate_search.py --sample 20 --budget 0` for the numeric pre-pass dump.
5. Live spot check: re-run tasks 17 and 51 and confirm `numeric: pass`.
