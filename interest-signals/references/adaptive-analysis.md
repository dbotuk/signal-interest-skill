# Adaptive analysis planner

The planner keeps a fixed, auditable core and adds only diagnostics justified by the research question or an existing core warning. It does not let the model invent calculations during a run.

## Planning contract

Every completed `analysis.json` contains:

- `analysis_plan.core_analyses`: calculations run for every series;
- `analysis_plan.selected_modules`: optional modules that ran for at least one series, with the selection reason;
- `analysis_plan.skipped_modules`: requested or routed modules that could not run on any series;
- `analysis_plan.not_selected_modules`: modules that were irrelevant to this question;
- `series[].adaptive_analysis`: per-series results or a structured skip reason.

With `analysis_modules: ["auto"]`, the planner matches English and Ukrainian intent terms for seasonality, changepoints, anomalies, and stability. A peak-sensitive core trend also triggers the anomaly scan. If nothing indicates a specialized diagnostic, the planner records `baseline_only` rather than filling the report with extra statistics.

Use explicit modules when the user names a method or the decision protocol requires it. `auto` and explicit module names are mutually exclusive so the saved request remains unambiguous.

## Interpretation boundaries

- Treat every optional result as descriptive and source-specific.
- Do not claim seasonality from less than two full cycles.
- Do not call a changepoint causal or statistically confirmed; the current scan selects the best descriptive split.
- Investigate anomaly candidates against events, source quality, redirects, and title history.
- Do not turn historical stability into a forecast.
- Report skipped modules when they were requested; never synthesize missing results.

The human-facing report shows at most the first three findings to protect the one-page layout. Full module output, methods, caveats, and skip reasons remain in `analysis.json`.

## Adding a module

Add a deterministic runner to `planner.py` that returns `computed` or `skipped`, states its method and caveat, and never silently fills unavailable observations. Define intent terms only when natural-language routing is reliable. Add synthetic positive, negative, and insufficient-data tests, document the minimum data requirement here, and ensure that the module does not alter core metrics or ranking unless a separately validated policy explicitly requires it.
