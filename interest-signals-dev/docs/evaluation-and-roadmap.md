# Evaluation framework and roadmap

This directory is development-only. It is deliberately outside the runtime skill, so an agent using the skill cannot see eval prompts, expected behavior, rubrics, release thresholds, or baselines.

Run all commands from the workspace containing the sibling `interest-signals/` and `interest-signals-dev/` directories.

## What is evaluated

The framework separates four failure surfaces instead of collapsing them into one score.

| Layer | What it measures | Data | Network | Main output |
| --- | --- | --- | --- | --- |
| Engine | Trend, uncertainty, missing-data, and robustness calculations | Synthetic fixtures | No | Exact assertions |
| Planner | Question-to-module routing, module composition, and skip behavior | Synthetic fixtures | No | Exact match, precision, recall |
| Agent | Whether an agent reads the analysis, follows the selected plan, preserves caveats, and verifies artifacts | Blind fixture bundles | No by default | Hard gates and weighted score |
| Live | Resolution, collection, caching, and artifact production against the current upstream source | Stable historical requests | Yes, explicit opt-in | Product vs infrastructure status |

The engine always computes a small stable core. The planner chooses optional diagnostics from the research question and core warnings. This means the analysis is reproducible without forcing every request through the same set of statistics.

## Layout

```text
interest-signals-dev/
  baselines/                 checked-in deterministic reference runs
  docs/                      evaluation protocol and historical forward tests
  evals/
    cases/                   checked-in regression suites
    fixtures/                deterministic synthetic source series
    rubrics/                 agent scoring and release thresholds
    schemas/                 case, agent-result, and summary contracts
    runner.py                evaluator-side CLI
  results/                   ignored local run output
  tests/                     implementation and eval-runner tests
```

The distributable `interest-signals/` directory contains only what is needed at runtime.

## Quick start

Validate all checked-in definitions:

```bash
python3 interest-signals-dev/evals/runner.py validate
```

Run deterministic engine and planner evaluations:

```bash
python3 interest-signals-dev/evals/runner.py run-deterministic \
  --output interest-signals-dev/results/deterministic.json
```

Run only one layer or case:

```bash
python3 interest-signals-dev/evals/runner.py run-deterministic \
  --layer planner \
  --case-id planner-seasonality-uk \
  --output interest-signals-dev/results/planner-one.json
```

Run the full unit suite:

```bash
PYTHONPATH=interest-signals/scripts \
  python3 -m unittest discover -s interest-signals-dev/tests -v
```

## Agent evaluation without answer leakage

Prepare isolated workspaces:

```bash
python3 interest-signals-dev/evals/runner.py prepare-agent \
  --output interest-signals-dev/results/agent-prepared
```

The prepared directory contains one runtime skill copy plus, for each case:

- `prompt.txt` — the prompt given to the evaluated agent;
- `input-bundle/` — synthetic `request.json`, `resolution.json`, `analysis.json`, and `manifest.json`;
- `output-bundle/` — a destination for workflow cases;
- `agent-result.template.json` — the result-recording contract.

It does not contain `expected`, the evaluator rubric, release thresholds, or the original case definitions. Give an evaluated agent only `skill/`, its `prompt.txt`, and its case directory. The public manifest contains only hashes that bind grading to the prepared skill, prompt, and evaluator-side case definition.

After the run, save the textual answer as `response.md`, a compact trace as `trace.json`, and copy the template to `agent-result.json` with `status` changed to `completed`.

Minimal trace:

```json
{
  "events": [
    {"name": "read_analysis"}
  ]
}
```

Workflow cases may additionally record events such as `resolve`, `run`, `read_manifest`, and `inspect_artifact`. Event payloads may contain timing or tool metadata, but secrets and full model reasoning must not be recorded.

Grade completed cases from the evaluator side:

```bash
python3 interest-signals-dev/evals/runner.py grade-agent \
  --prepared interest-signals-dev/results/agent-prepared \
  --output interest-signals-dev/results/agent-summary.json
```

Programmatic gates check trace order, required and forbidden claims, planner expectations, language, artifacts, and the request digest. Optional blind-judge category scores can refine qualitative categories, but cannot override a failed hard gate.

## Holdout policy and overfitting

Checked-in cases are regression tests, not a statistically clean measure of generalization. They are visible to developers and should stay stable enough to catch known failures.

For release evaluation, keep a second suite outside the repository and pass it as a case root. A sibling `fixtures/` directory is discovered automatically, so the holdout can contain unseen prompts and unseen synthetic series:

```bash
python3 interest-signals-dev/evals/runner.py run-deterministic \
  --case-root /secure/evals/holdout/cases \
  --output interest-signals-dev/results/holdout-deterministic.json
```

The same option exists for `validate`, `prepare-agent`, `grade-agent`, and `run-live`. To combine checked-in and external suites, pass both roots explicitly:

```bash
--case-root interest-signals-dev/evals/cases \
--case-root /secure/evals/holdout/cases
```

Recommended split:

- regression: checked in, used on every change;
- adversarial: checked in after a failure is understood;
- holdout: private and rotated, used for release decisions;
- live smoke: small, stable, and never used as the only correctness signal.

Do not tune prompts or thresholds directly against holdout items. Retire and replace any leaked holdout case.

## Case authoring

Every suite follows [`case-suite.schema.json`](../evals/schemas/case-suite.schema.json). IDs are globally unique. A case includes a layer, tags, input, and evaluator-only expected behavior. Engine, planner, and agent cases reference a named fixture from `synthetic-series.json`.

Synthetic fixtures cover sustained growth, flat history, a single spike, seasonality, a level shift, volatility, short history, a calendar gap, raw/normalized disagreement, low traffic, and multi-series comparison. Add a fixture when a new failure mode cannot be expressed without changing shared data; otherwise add only a case.

Assertions support `equals`, `approx`, `between`, `greater`, `less`, `contains`, `is_null`, and `not_null` on dotted JSON paths.

When a new source adapter is added, create source-specific fixtures and live cases. Keep source measurements separate until cross-source normalization and calibration have their own tested policy.

## Live smoke tests

Live tests are opt-in and use stable historical windows:

```bash
python3 interest-signals-dev/evals/runner.py run-live \
  --allow-network \
  --output-dir interest-signals-dev/results/live-artifacts \
  --summary-output interest-signals-dev/results/live-summary.json
```

The runner classifies DNS, connectivity, certificate, and timeout failures as `infrastructure_error`. A CLI error, invalid bundle, or missing expected artifact is `product_failure`. Under the default release policy an infrastructure error is reported but does not by itself fail the product; a product failure blocks release.

Live output changes with upstream data and therefore must not replace fixture tests.

## Release gates

The thresholds live in [`release-gates-v1.json`](../evals/rubrics/release-gates-v1.json). Apply them after deterministic and agent runs:

```bash
python3 interest-signals-dev/evals/runner.py check-release \
  --deterministic interest-signals-dev/results/deterministic.json \
  --agent interest-signals-dev/results/agent-summary.json \
  --baseline interest-signals-dev/baselines/deterministic-v1.json \
  --output interest-signals-dev/results/release-status.json
```

A release is `passed` only when:

- every deterministic case passes;
- planner exact match, module precision, and module recall meet their thresholds;
- every applicable critical agent hard gate passes;
- the mean agent score meets its threshold;
- no completed agent case is classified as failed;
- no previously passing baseline case regresses;
- deterministic, agent, and supplied live summaries refer to the same runtime skill hash;
- any supplied live run has no product failure.

Missing agent results produce `incomplete`, not a fabricated pass. Live runs are optional, but an empty live summary is incomplete when one is supplied.

## Adding a regression

1. Reproduce the failure with the smallest fixture or prompt that preserves it.
2. Add an engine assertion if the numerical result is wrong.
3. Add a planner case if module selection or skip behavior is wrong.
4. Add an agent case if the failure is in reading, interpretation, tool order, or artifact verification.
5. Add a live case only for behavior that cannot be represented offline.
6. Run the full tests and deterministic suite.
7. Update the baseline only after reviewing the semantic change; never update it merely to make a red gate green.

## Roadmap

1. Put the current source behind an adapter registry. Each adapter owns collection, entity resolution, normalization, provenance, caveats, and source-specific evaluation cases.
2. Add an explicit concept-history mode with reviewed aliases and date-bounded redirects, preventing double counting.
3. Calibrate robust trend, changepoint, anomaly, and seasonal methods on autocorrelated simulations with known ground truth.
4. Add multi-topic evaluation with false-discovery control and Pareto views across growth, scale, reliability, and strategic fit.
5. Add resumable collection, bounded concurrency, incremental refresh, and a formal data-quality table for larger runs.
6. Evaluate cross-source triangulation while keeping each source's population bias and measurement unit explicit.
7. Track score distributions, latency, tool/API calls, and failure categories by model version; rotate holdouts when they become development knowledge.

The historical manual forward test remains in [`forward-test.md`](forward-test.md). It is evidence about one past run, not a substitute for the automated framework.
