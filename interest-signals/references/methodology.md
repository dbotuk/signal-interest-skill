# Methodology and interpretation

This reference documents the bundled Wikimedia Pageviews source. Future sources must define their own collection contract, normalization, evidence policy, caveats, and evaluation fixtures rather than inheriting these rules automatically.

## Data path

The pipeline uses two public Wikimedia systems:

1. The per-language [MediaWiki Action API](https://www.mediawiki.org/wiki/API:Main_page) searches the seed wiki, canonicalizes redirects, rejects disambiguation pages, records the Wikidata QID, reads the earliest available revision timestamp, and obtains exact interlanguage links.
2. The [Wikimedia Pageviews API](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html) supplies daily per-article and project totals with identical `all-access/user` filters.

Every request sends a descriptive User-Agent, runs sequentially, honors `Retry-After`, retries bounded 429/5xx failures, and is cached by full URL. Start and end dates are inclusive UTC dates. Requests are split into year-sized chunks, then daily data is aggregated locally to avoid partial boundary buckets from the monthly endpoint.

## Title resolution

The source search is only accepted when its title is plausibly close to the query. Exact interlanguage links are preferred over separately translated searches. A missing sitelink means "no verified equivalent article," not zero interest. The pipeline stops unless the caller supplies an explicit title or deliberately enables a low-confidence search fallback.

An explicit target title is not trusted merely because it exists. When both pages have Wikidata IDs, the pipeline compares them: a match is high confidence, a mismatch is low confidence and emits a warning, and an unavailable ID is medium confidence and requires manual verification. This is a warning rather than a hard failure because rare legitimate mappings can use distinct Wikidata items.

Pageviews are keyed by requested title, not stable page ID. Redirect traffic is credited separately, and moves can split a historical series. Version 1 therefore uses a deterministic `page` scope: the current canonical title only. It records redirects and warns rather than silently summing today's redirect aliases across history.

## Metrics

For each complete calendar month the pipeline reports:

- `views`: human-classified views of the canonical article;
- `project_views`: human-classified views of the corresponding language project;
- `share_per_million`: `views / project_views * 1,000,000`.

Raw views are a scale signal. Share per million is a relative-attention signal that reduces bias from differently sized Wikipedia editions. Neither corrects for population, internet access, demographics, media exposure, or product-market fit. Report both; use `share` as the primary trend metric for cross-language comparisons unless the user's criterion says otherwise.

## Trend and sensitivity

The annualized trend is the ordinary least-squares slope of `log1p(monthly_value)` against original calendar-month positions, multiplied by 12 and transformed back to a percentage. Gaps are not compressed. Cross-language runs use the intersection of complete, eligible calendar months so every ranked series covers the same dates. This is a compact descriptive trend, not a causal effect.

The 95% interval is a deterministic residual moving-block bootstrap with three-month blocks. It preserves a small amount of short-range dependence but does not model every seasonal or structural process. Fewer than 100 resamples suppresses the interval and contributes no trend-signal evidence. Also report:

- trailing 12 months versus the preceding 12 months when 24 contiguous months exist;
- recent three-month momentum versus the previous three months when six contiguous months exist;
- the largest month's share of the period;
- the trend after removing the largest month.

Flag event sensitivity when removing the peak reverses the trend or changes it materially. Investigate news, campaigns, school calendars, page moves, or data incidents before acting.

## Evidence quality

Evidence quality is a transparent heuristic from 0 to 100, not a probability that a conclusion is true. It combines:

- sample length (25%);
- whether the bootstrap interval separates from zero, or signal relative to interval width (30%);
- absolute traffic base (20%);
- resistance to a single peak month (15%);
- title-resolution confidence (10%).

Labels are high at 75+, medium at 50-74.9, and low below 50. A high score can still accompany a commercially irrelevant topic; a low score means more or different evidence is needed.

## Adaptive analysis

The reproducible core always calculates trend, uncertainty, momentum, peak sensitivity, and evidence quality. Optional modules are selected from the research question or an explicit override, then gated by their data requirements. The planner records selected, skipped, and unselected modules in `analysis_plan`; each series stores results in `adaptive_analysis`.

- Seasonality requires at least 24 contiguous eligible months and two observations for each calendar month. It estimates month-of-year effects after removing a log-linear trend.
- Changepoints require at least 12 contiguous eligible months and six months on both sides of a candidate split. The best two-segment fit is descriptive and has no multiple-testing or causal interpretation.
- Anomalies require at least 12 eligible months. They are candidate outliers in detrended log space at an absolute robust z-score of 3.5 or more.
- Stability requires at least six eligible months. It describes residual volatility and typical month-to-month movement within the observed window; it is not a forecast.

`auto` does not mean "run everything." It routes explicit question intent and may add the anomaly module when the core analysis already finds peak-sensitive results. If neither condition applies, the plan remains baseline-only. See [adaptive-analysis.md](adaptive-analysis.md) for routing and extension rules.

## Screening score

For two or more candidates, growth uses fixed anchors: -25% annualized maps to 0, flat maps to 0.5, and +25% maps to 1, with values clipped outside that range. Absolute monthly article volume uses a five-order logarithmic scale, so 100 views is about 0.4, 1,000 is 0.6, 10,000 is 0.8, and 100,000 is 1. Evidence quality is divided by 100. The configured weights produce a 0-100 screening score and rank. The fixed anchors prevent the least-bad declining candidate from receiving a perfect score, but the result remains a heuristic rather than a launch threshold. No score is calculated for a single series.

## Missing and provisional data

Pageviews begins on 2015-07-01. Zero-view timestamps can be omitted, so the pipeline creates a complete daily index and records omitted days as `implicit_zero_days`. It records pre-creation months separately when the earliest revision is known and excludes the partial creation month from inference. Project-data gaps and provisional periods are also excluded rather than drawn or analyzed as zero attention. A whole-query 404 remains an error because it can mean zero views or data not yet loaded. Explicit ranges touching the newest two days are marked provisional; the default range ends on the last complete month.

Consult Wikimedia's [analytics troubleshooting guide](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/troubleshooting.html) and [data issues register](https://wikitech.wikimedia.org/wiki/Data_Platform/Data_Lake/Data_Issues) when results look anomalous.

## Report integrity

Human-facing charts break lines across unavailable periods instead of converting gaps, provisional periods, or pre-creation months to zero. Reports disclose warnings and any languages omitted from the one-page view; JSON and CSV retain all series. PDF generation validates the chosen TrueType font against every visible character and fails with setup guidance when glyph coverage is insufficient.

The PDF canvas path uses a closed simple-layout allowlist. It permits ordinary spaces and Unicode numbers, non-joining punctuation, and non-composing symbols. Letter characters must have verified Unicode name prefixes: Latin, Greek, Cyrillic, CJK unified or compatibility ideographs, the ideographic iteration and closing marks, Hiragana, Katakana (including the prolonged sound mark), precomposed Hangul syllables, Bopomofo, or fullwidth Latin. It rejects every Unicode mark (`M*`) and control/format/surrogate/private/unassigned character (`C*`), the joining punctuation U+1807/U+180A, regional-indicator flag characters, emoji modifier characters, right-to-left and Arabic-number bidi classes, non-space separators, decomposed Hangul Jamo, and letters from all other scripts. Font glyph coverage is checked separately after this preflight. ReportLab 4.4.9 has an experimental shaping path, but its bidi component is not available from the public Python package index; this package does not silently substitute an unverified text-layout stack. Exclude `pdf` from `formats` and use HTML/SVG plus JSON/CSV for rejected scripts. Horizontal CJK can use the simple path when an explicitly configured TrueType font covers every visible character.

## Decision language

Use "attention," "signal," "candidate," and "validate next." Avoid "demand," "market size," or "users want" unless other evidence supports those claims. A useful recommendation names the measured pattern, uncertainty or fragility, the business assumption it does not test, and the next validation step.
