---
name: interest-signals
description: Plan and run question-aware interest-signal analysis over time and across comparable segments, combining a reproducible core with data-gated seasonality, changepoint, anomaly, and stability diagnostics. Use for early topic, content, or localization research; the bundled first source compares Wikipedia language editions through public Wikimedia Pageviews, not market size or purchase intent.
metadata:
  version: "1.1.0"
  runtime: "Python 3.11+, outbound HTTPS to Wikimedia APIs for the current adapter, and ReportLab for PDF output"
---

# Interest Signals

Use the bundled pipeline for data collection and calculations. Do not rewrite analysis code ad hoc.

## Supported source

The current implementation uses Wikimedia Pageviews plus MediaWiki/Wikidata resolution. Treat Wikipedia as the first evidence source, not the identity of this skill. Do not imply support for another source until its collection, normalization, provenance, caveats, and evaluation are implemented.

## Workflow

1. Capture the research question, topic, comparison dimensions, time window, and decision criterion. Confirm the active source: this version supports only Wikimedia Pageviews, so do not imply that another source can be collected by the bundled pipeline. For the current source, capture the target Wikipedia language codes. Unless the user says otherwise:
   - use the latest 24 complete calendar months;
   - use English as the concept-resolution language;
   - use `share` for cross-language comparisons and `raw` for a single language;
   - weight screening as growth 0.50, absolute volume 0.20, and evidence quality 0.30.
   - use `analysis_modules: ["auto"]`, which retains the reproducible core and selects optional diagnostics only when the question or core diagnostics justify them.
2. Run from this skill directory. Install the pinned PDF dependencies only if they are unavailable:

   ```bash
   python -m pip install -r requirements.lock.txt
   ```

   The PDF renderer uses a conservative simple-layout allowlist. It accepts ordinary spaces, numbers, non-joining punctuation, and non-composing symbols plus precomposed letters from Latin, Greek, Cyrillic, horizontal Han/Hiragana/Katakana (including ideographic iteration and closing marks), precomposed Hangul syllables, Bopomofo, and fullwidth Latin forms. It rejects all Unicode marks and controls, Mongolian joining punctuation U+1807/U+180A, regional-indicator flags, emoji modifiers, right-to-left text, decomposed Hangul Jamo, and letters from every other script even when a font contains the glyphs. For rejected text, run with `--formats csv,svg,html`; the machine-readable and browser-rendered outputs retain the original Unicode. CJK also requires compatible fonts via `INTEREST_SIGNALS_FONT` and `INTEREST_SIGNALS_BOLD_FONT`.

3. Resolve titles before downloading traffic. Inspect `resolution.json`; verify that articles describe the same concept and review the Wikidata IDs, semantic-match status, methods, candidate titles, redirects, and warnings. An explicit target with a mismatched or unavailable Wikidata ID still requires human verification.

   ```bash
   python scripts/analyze.py \
     --topic "intermittent fasting" \
     --question "Which language has the most stable attention, and were there unusual spikes?" \
     --source-language en \
     --languages cs,de \
     --output-dir run-output \
     --resolve-only
   ```

   If a language has no interlanguage link, do not call that zero interest. Find and verify an equivalent article, then pass `--title LANG=ARTICLE`; omit the language if none exists. Use `--allow-search-fallback` only for candidate discovery and verify every low-confidence result.
4. Run the full analysis with the verified mappings. Add `--title` arguments as needed. The same output directory reuses cached public API responses.

   ```bash
   python scripts/analyze.py \
     --topic "intermittent fasting" \
     --source-language en \
     --languages cs,de \
     --title cs="Přerušovaný půst" \
     --title de="Intermittierendes Fasten" \
     --start 2024-01-01 \
     --end 2025-12-31 \
     --metric share \
     --output-dir run-output
   ```

5. Read `analysis.json` before reporting. Inspect `analysis_plan` first: confirm which optional modules were selected, why they were selected, and whether any were skipped for insufficient data. Then check each series' title resolution, core metrics, adaptive results, evidence score, warnings, and monthly data status. Treat the screening score as prioritization support, not a decision rule or launch threshold.
6. Present measured facts separately from interpretation. Answer in the user's language and explain why a candidate merits further validation. State the active source and its limitations; for Wikimedia, Wikipedia attention is not unique audience, market size, conversion, or willingness to pay.
7. Share the requested artifacts. The run bundle always contains `request.json`, `resolution.json`, `analysis.json`, and `manifest.json`, plus the requested CSV, SVG, HTML, and supported PDF outputs. Confirm any PDF is one page and visually readable before delivery.

## Follow-ups

For changed dates, weights, metrics, or title assumptions, edit a copy of `request.json` and rerun with `--config COPY.json --output-dir SAME_DIR`. The cache avoids redundant requests. Use a new output directory when the user wants side-by-side scenarios.

When exact mappings or assumptions change, rerun the pipeline; do not edit computed values manually. Cite article URLs and API source URLs recorded in the JSON provenance.

When adding a source, keep its raw observations, normalization, evidence policy, provenance, and caveats source-specific. Do not combine or rank unlike sources until an explicit calibration policy has been validated.

## References

- Read [references/methodology.md](references/methodology.md) before interpreting evidence, changing formulas, or handling missing data and redirects.
- Read [references/adaptive-analysis.md](references/adaptive-analysis.md) when choosing, overriding, interpreting, or extending optional analysis modules.
- Read [references/request-format.md](references/request-format.md) for the JSON request contract and reusable command patterns.
