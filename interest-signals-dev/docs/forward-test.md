# Independent cheap-model forward test

This is a development-only historical record stored outside the distributable skill.

This record predates the rename from `wikipedia-interest` to `interest-signals` and the v1.1 adaptive analysis planner. Historical temporary paths and quoted prompts are preserved verbatim so the evaluation provenance is not rewritten after the fact; this record therefore validates the v1.0 baseline workflow, not adaptive module selection.

## Test record

| Field | Recorded value |
| --- | --- |
| Date | 2026-09-27 |
| Evaluator | `gpt-5.6-luna`, low reasoning effort |
| Isolation | Fresh fork with no worked examples or expected answer; outputs were written only under `/private/tmp/wikipedia-interest-forward-test-20260927` |
| User scenario | A product team considering an astronomy course asks whether Wikipedia attention is rising in Ukrainian over the prior two complete calendar years and requests a one-page report |
| Resolved concept | English `Astronomy` to Ukrainian `Астрономія`, both Wikidata `Q333` |
| Analysis request | `2024-01-01` through `2025-12-31`, normalized share metric, 600 bootstrap samples |
| Saved request digest | `bfaa439f511cc8a822355246ef550de41608484cc190a3d27b10785998f55bc6` |

The complete initial delegation was not retained after orchestration-context compaction, so it would be misleading to reconstruct it as an exact quotation. The preserved fragments are:

> Independently forward-test ... Do not edit ... Work only in `/private/tmp/wikipedia-interest-forward-test-20260927` ... Realistic user request: "We are considering adding an astronomy course ...". Use the skill ... Return...

The ellipses above mark text missing from the retained record, not omissions from a claimed verbatim quotation. What is known from the execution trace is that the evaluator received the skill path and realistic request, but no example output, expected title, intended conclusion, suspected bug, or proposed fix.

## First run, correction, and rerun

The evaluator correctly used the skill pipeline, resolved the Ukrainian article through the interlanguage link, reviewed the evidence and caveats, produced the complete artifact bundle, and checked the PDF. It also exposed a wording defect: a one-language decline was described comparatively as:

> uk has the least negative estimated annualized share trend at -43.1%.

That phrasing was inappropriate when there was only one series. The finding generator was narrowed so a single series reports its own direction, and `test_single_series_finding_is_not_comparative` was added as a regression test.

The full verbatim rerun message was also not retained. Its recorded instruction was to rerun the same isolated scenario after the wording fix and verify direct, non-comparative language plus the one-page PDF. The rerun passed and emitted:

> uk has an estimated annualized share trend of -43.1%, indicating decreasing Wikipedia attention over this window.

`pdfinfo` reported one unencrypted A4 landscape page (64,582 bytes). A rendered preview was inspected for clipping and readability, including the Cyrillic title `Астрономія`; Unicode rendered correctly in that environment. The remaining minor presentation issue is that prose uses the language code `uk` instead of a human-friendly label such as “Ukrainian.” It does not change the calculation or conclusion.

## Final-package independent audit

A second fresh `gpt-5.6-luna` evaluator at low reasoning effort received the completed skill and this exact scenario, with no expected answer:

> Compare whether Wikipedia attention to heat pumps is growing in German and French over the last 18 complete months, using normalized attention, and produce a one-page PDF for a market-research briefing.

It correctly selected `2025-03-01` through `2026-08-31` as the latest 18 complete months on 2026-09-27 and attempted resolution first. The host's DNS sandbox blocked the Wikimedia request before any output could be produced, so this attempt is recorded as an infrastructure failure rather than a behavioral pass. The exact failed command was:

```bash
python scripts/analyze.py \
  --topic "heat pumps" \
  --source-language en \
  --languages de,fr \
  --start 2025-03-01 \
  --end 2026-08-31 \
  --metric share \
  --output-dir /private/tmp/wikipedia-interest-final-forward-test-20260927 \
  --resolve-only
```

As a read-only fallback, the same evaluator audited the packaged live astronomy example. It confirmed that all eight declared artifacts exist, the canonical request digest agrees across the request, analysis, and manifest, all three resolved articles share Wikidata `Q333`, both compared series use the same 24 complete observed months, the findings match the reported estimates and intervals, and the PDF is one readable A4 landscape page with intact Cyrillic. It found no material correctness issue; its only minor usability note was the use of language codes (`pl`, `uk`) rather than human-friendly names.

## Development and verification provenance

- The primary Codex agent implemented the reusable skill, deterministic Python pipeline, fixtures, tests, and documentation.
- Independent agents researched the Wikimedia API constraints, checked Agent Skills packaging, reviewed the workspace architecture, and audited the implementation. Their findings were checked against the code and official API behavior before changes were accepted.
- The inexpensive evaluator above performed the end-to-end behavioral test without the intended answer. Its observed failure led to a small code correction and a regression test, followed by a fresh rerun of the same scenario.
- Deterministic checks include the unit suite, CLI help, skill-package validation, static linting, manifest inspection, and PDF page-count/layout inspection. Live Wikimedia output is treated as a smoke test rather than a replacement for fixture tests because upstream data can change.
- The generated conclusion remained measurement-only: the negative Wikipedia attention trend is not evidence of market size, purchase intent, conversion, or course demand.

Re-run this forward test when title resolution, metrics, findings, artifact generation, or the request schema changes materially. Preserve the exact future prompts and command transcript alongside the record so later runs do not inherit this test's prompt-retention gap.
