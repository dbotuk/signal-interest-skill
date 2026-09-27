# Request contract

`scripts/analyze.py` accepts flags or a JSON config. Flags override JSON values. This v1.1 contract belongs to the bundled Wikimedia Pageviews source and has no runtime source selector; every request uses that implementation. Future adapters should use versioned, source-specific configuration rather than reinterpret these fields. The following is a complete reusable request:

```json
{
  "topic": "astronomy",
  "question": "Which language has the most stable attention, and were there unusual spikes?",
  "analysis_modules": ["auto"],
  "source_language": "en",
  "languages": ["uk", "pl"],
  "titles": {},
  "start": "2024-01-01",
  "end": "2025-12-31",
  "metric": "share",
  "weights": {
    "growth": 0.5,
    "volume": 0.2,
    "reliability": 0.3
  },
  "formats": ["csv", "svg", "html", "pdf"],
  "bootstrap_samples": 600,
  "seed": 1729,
  "allow_search_fallback": false
}
```

Run it from the skill directory:

```bash
python scripts/analyze.py --config references/request.example.json --output-dir run-output
```

## Fields

- `topic` is the human label and source-wiki search phrase.
- `question` is an optional research question of at most 1,000 characters. In `auto` mode it routes relevant optional diagnostics; without a specialized intent, the reproducible core remains sufficient.
- `analysis_modules` defaults to `["auto"]`. Explicit values can be any combination of `seasonality`, `changepoints`, `anomalies`, and `stability`. Do not combine `auto` with explicit modules. Explicit selection still cannot bypass a module's minimum data requirements.
- `source_language` identifies the concept and supplies interlanguage links.
- `languages` contains 1-12 lowercase Wikipedia language codes.
- `titles` maps a language code to an exact local article title. Use this to resolve missing or disputed mappings.
- `start` and `end` are inclusive UTC dates on or after 2015-07-01. Complete months are preferred.
- `metric` is `raw` or `share`. Share means article views per million total views in the same language project.
- `weights` are non-negative. They are normalized automatically and only affect relative ranking, not measurements.
- `formats` can include `csv`, `svg`, `html`, and `pdf`. JSON request, resolution, analysis, and manifest files are always written.
- `bootstrap_samples` is 0-10000. Values below 100 suppress the 95% interval and are suitable only for quick debugging; the default is 600.
- `seed` makes intervals reproducible.
- `allow_search_fallback` permits target-language search when no interlanguage link exists. Its results are low-confidence and require manual verification.
- `user_agent` can override the bundled descriptive identifier. Use `Tool/version (contact)` form for deployed use.

PDF output uses pinned FiraGO TrueType fonts and a conservative simple-layout allowlist. It accepts spaces, numbers, non-joining punctuation, and non-composing symbols plus precomposed Latin, Greek, Cyrillic, horizontal Han/Hiragana/Katakana (including ideographic iteration and closing marks), precomposed Hangul syllables, Bopomofo, and fullwidth Latin letters. It rejects Unicode marks and controls, Mongolian joining punctuation U+1807/U+180A, regional-indicator flags, emoji modifiers, RTL or Arabic-number bidi classes, decomposed Hangul Jamo, and every other letter script before creating a PDF. For rejected runs, remove `pdf` from `formats` (or pass `--formats csv,svg,html`) so the Unicode JSON, CSV, SVG, and HTML outputs can be produced. Horizontal CJK requires appropriate TrueType files through `INTEREST_SIGNALS_FONT` and `INTEREST_SIGNALS_BOLD_FONT`.

Operational fields `output_dir`, `refresh`, and `resolve_only` may also appear in a config, but keeping them on the command line makes a request easier to reuse.

Use `--question "..."` with the default planner, or override it deliberately:

```bash
python scripts/analyze.py \
  --topic "astronomy" \
  --languages uk,pl \
  --question "Was there a structural break after the campaign?" \
  --analysis-modules changepoints,anomalies \
  --output-dir run-output
```

## Iterative comparisons

- Change criteria: copy `request.json`, edit `weights` or `metric`, and rerun to the same directory. Cached responses are reused.
- Change the article mapping: add or replace an entry in `titles`, then rerun and recheck `resolution.json`.
- Compare scenarios without overwriting: use a new output directory for each config.
- Force fresh public data: add `--refresh`.
- Keep an audit trail: retain `request.json`, `resolution.json`, `analysis.json`, and `manifest.json` together.
