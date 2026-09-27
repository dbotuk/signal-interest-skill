#!/usr/bin/env python3
"""Deterministic, agent, and live evaluation runner for the sibling skill."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable


EVAL_ROOT = Path(__file__).resolve().parent
DEV_ROOT = EVAL_ROOT.parent
WORKSPACE_ROOT = DEV_ROOT.parent
SKILL_ROOT = WORKSPACE_ROOT / "interest-signals"
SCRIPTS_ROOT = SKILL_ROOT / "scripts"
DEFAULT_CASE_ROOT = EVAL_ROOT / "cases"
FIXTURE_PATH = EVAL_ROOT / "fixtures" / "synthetic-series.json"
RUBRIC_ROOT = EVAL_ROOT / "rubrics"
RESULTS_ROOT = DEV_ROOT / "results"

sys.path.insert(0, str(SCRIPTS_ROOT))

from interest_signals.metrics import (  # noqa: E402
    analyze_metric,
    analyze_series,
    build_findings,
    rank_series,
)
from interest_signals.planner import apply_analysis_plan  # noqa: E402


RANKING_WEIGHTS = {"growth": 0.5, "volume": 0.2, "reliability": 0.3}
EXCLUDED_STATUSES = {
    "not_created",
    "creation_partial",
    "project_data_gap",
    "provisional",
}
NETWORK_ERROR_TERMS = (
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided",
    "network is unreachable",
    "connection timed out",
    "connection reset",
    "connection refused",
    "certificate verify failed",
    "dns",
    "timed out",
)


class EvalConfigError(ValueError):
    """Raised when an evaluation definition or result is invalid."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def run_id(label: str) -> str:
    return datetime.now(UTC).strftime(f"%Y%m%dT%H%M%SZ-{label}")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise EvalConfigError(f"Cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise EvalConfigError(f"Invalid JSON in {path}: {error}") from error


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def canonical_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def skill_sha256() -> str:
    if not SKILL_ROOT.is_dir():
        raise EvalConfigError(f"Runtime skill not found at {SKILL_ROOT}")
    digest = hashlib.sha256()
    ignored_names = {".DS_Store", "__pycache__"}
    for path in sorted(
        item
        for item in SKILL_ROOT.rglob("*")
        if item.is_file()
        and not any(part in ignored_names for part in item.parts)
        and item.suffix not in {".pyc", ".pyo"}
    ):
        relative = path.relative_to(SKILL_ROOT).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _json_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix == ".json" else []
    if root.is_dir():
        return sorted(root.rglob("*.json"))
    raise EvalConfigError(f"Case root does not exist: {root}")


def _fixture_catalog_paths(case_roots: Iterable[Path]) -> list[Path]:
    paths = [FIXTURE_PATH]
    candidate_dirs: set[Path] = set()
    for root in case_roots:
        anchor = root if root.is_dir() else root.parent
        candidate_dirs.add(anchor / "fixtures")
        candidate_dirs.add(anchor.parent / "fixtures")
    for directory in sorted(candidate_dirs):
        if directory.is_dir():
            paths.extend(sorted(directory.rglob("*.json")))
    return list(dict.fromkeys(path.resolve() for path in paths))


def load_fixtures(
    case_roots: Iterable[Path] | None = None,
) -> dict[str, dict[str, Any]]:
    roots = list(case_roots or [DEFAULT_CASE_ROOT])
    fixtures: dict[str, dict[str, Any]] = {}
    sources: dict[str, Path] = {}
    for path in _fixture_catalog_paths(roots):
        payload = load_json(path)
        if payload.get("schema_version") != "1.0" or not isinstance(
            payload.get("fixtures"), dict
        ):
            raise EvalConfigError(f"Invalid fixture catalog: {path}")
        for fixture_id, definition in payload["fixtures"].items():
            if fixture_id in fixtures and fixtures[fixture_id] != definition:
                raise EvalConfigError(
                    f"Fixture {fixture_id!r} is defined differently in "
                    f"{sources[fixture_id]} and {path}"
                )
            fixtures[fixture_id] = definition
            sources[fixture_id] = path
    return fixtures


def load_cases(case_roots: Iterable[Path] | None = None) -> list[dict[str, Any]]:
    roots = list(case_roots or [DEFAULT_CASE_ROOT])
    fixtures = load_fixtures(roots)
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        for path in _json_files(root):
            payload = load_json(path)
            if "layer" not in payload and isinstance(payload.get("fixtures"), dict):
                continue
            if payload.get("schema_version") != "1.0":
                raise EvalConfigError(f"{path}: schema_version must be 1.0")
            layer = payload.get("layer")
            if layer not in {"engine", "planner", "agent", "live"}:
                raise EvalConfigError(f"{path}: invalid layer {layer!r}")
            suite_cases = payload.get("cases")
            if not isinstance(suite_cases, list) or not suite_cases:
                raise EvalConfigError(f"{path}: cases must be a non-empty array")
            for raw in suite_cases:
                if not isinstance(raw, dict):
                    raise EvalConfigError(f"{path}: each case must be an object")
                missing = {"id", "title", "tags", "input", "expected"} - set(raw)
                if missing:
                    raise EvalConfigError(
                        f"{path}: case missing {', '.join(sorted(missing))}"
                    )
                case_id = raw["id"]
                if not isinstance(case_id, str) or not re.fullmatch(
                    r"[a-z0-9][a-z0-9-]+", case_id
                ):
                    raise EvalConfigError(f"{path}: invalid case id {case_id!r}")
                if case_id in seen:
                    raise EvalConfigError(f"Duplicate case id: {case_id}")
                seen.add(case_id)
                fixture = raw.get("fixture")
                if fixture is not None and fixture not in fixtures:
                    raise EvalConfigError(
                        f"{path}: case {case_id} references unknown fixture {fixture!r}"
                    )
                rubric = raw.get("rubric")
                if rubric and not (RUBRIC_ROOT / f"{rubric}.json").is_file():
                    raise EvalConfigError(
                        f"{path}: case {case_id} references unknown rubric {rubric!r}"
                    )
                if layer in {"engine", "planner", "agent"} and not fixture:
                    raise EvalConfigError(f"{path}: {case_id} requires a fixture")
                if layer == "agent" and not raw.get("prompt"):
                    raise EvalConfigError(f"{path}: {case_id} requires a prompt")
                if layer == "live" and not raw["input"].get("steps"):
                    raise EvalConfigError(f"{path}: {case_id} requires live steps")
                case = dict(raw)
                case["layer"] = layer
                case["definition_file"] = str(path.resolve())
                if fixture is not None:
                    case["_fixture_data"] = fixtures[fixture]
                cases.append(case)
    return cases


def _month_add(start: str, offset: int) -> str:
    year, month = (int(part) for part in start.split("-"))
    absolute = year * 12 + month - 1 + offset
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def _generate_values(spec: dict[str, Any], months: int, start: str) -> list[float]:
    kind = spec.get("type")
    if kind == "constant":
        return [float(spec["value"])] * months
    if kind == "exponential":
        base = float(spec["base"])
        rate = float(spec["monthly_rate"])
        return [base * ((1 + rate) ** index) for index in range(months)]
    if kind == "spike":
        values = [float(spec["base"])] * months
        values[int(spec["index"])] = float(spec["value"])
        return values
    if kind == "seasonal":
        base = float(spec["base"])
        rate = float(spec.get("monthly_rate", 0.0))
        effects = {
            str(key).zfill(2): float(value) for key, value in spec["effects"].items()
        }
        return [
            base
            * ((1 + rate) ** index)
            * effects.get(_month_add(start, index)[5:7], 1.0)
            for index in range(months)
        ]
    if kind == "level_shift":
        split = int(spec["split"])
        return [
            float(spec["before"] if index < split else spec["after"])
            for index in range(months)
        ]
    if kind == "values":
        values = [float(value) for value in spec["values"]]
        if len(values) != months:
            raise EvalConfigError(
                f"values generator has {len(values)} values for {months} months"
            )
        return values
    raise EvalConfigError(f"Unknown synthetic generator type: {kind!r}")


def materialize_monthly(
    fixture: dict[str, Any], series_spec: dict[str, Any]
) -> list[dict[str, Any]]:
    start = str(fixture["start"])
    months = int(series_spec["months"])
    raw_values = _generate_values(series_spec["raw"], months, start)
    share_values = _generate_values(series_spec["share"], months, start)
    gaps = {int(index) for index in series_spec.get("gaps", [])}
    rows: list[dict[str, Any]] = []
    for index, (raw, share) in enumerate(zip(raw_values, share_values)):
        is_gap = index in gaps
        rows.append(
            {
                "month": _month_add(start, index),
                "views": raw,
                "project_views": 1_000_000.0,
                "share_per_million": share,
                "days_in_range": 30,
                "article_observed_days": 0 if is_gap else 30,
                "implicit_zero_days": 0,
                "project_observed_days": 0 if is_gap else 30,
                "data_status": "project_data_gap" if is_gap else "observed",
                "complete_month": True,
            }
        )
    return rows


def _shared_months(monthly_sets: list[list[dict[str, Any]]]) -> set[str] | None:
    if len(monthly_sets) <= 1:
        return None
    eligible = [
        {
            str(row["month"])
            for row in rows
            if row.get("complete_month")
            and row.get("data_status") not in EXCLUDED_STATUSES
        }
        for rows in monthly_sets
    ]
    return set.intersection(*eligible) if eligible else set()


def materialize_analysis(
    fixture_name: str,
    *,
    fixture_data: dict[str, Any] | None = None,
    selected_metric: str = "raw",
    question: str | None = None,
    modules: list[str] | None = None,
    bootstrap_samples: int = 300,
    seed: int = 20260927,
) -> dict[str, Any]:
    fixture = fixture_data or load_fixtures()[fixture_name]
    monthly_sets = [
        materialize_monthly(fixture, spec) for spec in fixture.get("series", [])
    ]
    shared = _shared_months(monthly_sets)
    series: list[dict[str, Any]] = []
    for spec, monthly in zip(fixture.get("series", []), monthly_sets):
        language = str(spec["language"])
        title = str(spec["title"])
        article = {
            "language": language,
            "title": title,
            "url": f"https://example.invalid/{language}/{title.replace(' ', '_')}",
            "resolution_method": "synthetic_fixture",
            "resolution_confidence": "high",
            "redirected_from": None,
            "wikidata_id": None,
            "semantic_match": "synthetic evaluation fixture",
            "candidate_titles": [],
        }
        series.append(
            analyze_series(
                article=article,
                monthly=monthly,
                selected_metric=selected_metric,
                bootstrap_samples=bootstrap_samples,
                base_seed=seed,
                analysis_months=shared,
            )
        )
    series = rank_series(series, RANKING_WEIGHTS)
    plan, adaptive_findings = apply_analysis_plan(
        series,
        question=question,
        requested_modules=modules or ["auto"],
    )
    warnings = list(fixture.get("warnings", []))
    for item in series:
        warnings.extend(
            f"{item['language']}: {warning}" for warning in item.get("warnings", [])
        )
    return {
        "analysis_plan": plan,
        "series": series,
        "findings": adaptive_findings + build_findings(series),
        "warnings": sorted(set(warnings)),
    }


def _path_value(payload: Any, dotted_path: str) -> Any:
    value = payload
    for part in dotted_path.split("."):
        if isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError) as error:
                raise KeyError(dotted_path) from error
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise KeyError(dotted_path)
    return value


def evaluate_assertion(payload: Any, assertion: dict[str, Any]) -> dict[str, Any]:
    path = str(assertion["path"])
    operation = str(assertion["op"])
    try:
        actual = _path_value(payload, path)
    except KeyError:
        return {"passed": False, "path": path, "op": operation, "error": "missing path"}
    expected: Any = assertion.get("value")
    if operation == "between":
        expected = {"min": assertion.get("min"), "max": assertion.get("max")}
    passed = False
    try:
        if operation == "equals":
            passed = actual == expected
        elif operation == "approx":
            tolerance = float(assertion.get("tolerance", 1e-9))
            passed = isinstance(actual, (int, float)) and math.isclose(
                float(actual), float(expected), abs_tol=tolerance, rel_tol=0.0
            )
        elif operation == "between":
            passed = float(assertion["min"]) <= float(actual) <= float(assertion["max"])
        elif operation == "greater":
            passed = float(actual) > float(expected)
        elif operation == "less":
            passed = float(actual) < float(expected)
        elif operation == "contains":
            passed = expected in actual
        elif operation == "is_null":
            passed = actual is None
        elif operation == "not_null":
            passed = actual is not None
        else:
            raise EvalConfigError(f"Unknown assertion operation: {operation}")
    except (TypeError, ValueError):
        passed = False
    return {
        "passed": passed,
        "path": path,
        "op": operation,
        "expected": expected,
        "actual": actual,
    }


def _case_selection(
    cases: list[dict[str, Any]], layers: set[str], case_ids: set[str]
) -> list[dict[str, Any]]:
    selected = [
        case
        for case in cases
        if case["layer"] in layers and (not case_ids or case["id"] in case_ids)
    ]
    if case_ids:
        found = {case["id"] for case in selected}
        missing = sorted(case_ids - found)
        if missing:
            raise EvalConfigError(
                "Unknown or wrong-layer case(s): " + ", ".join(missing)
            )
    return selected


def run_engine_case(case: dict[str, Any]) -> dict[str, Any]:
    fixture = case["_fixture_data"]
    request = case["input"]
    if request.get("operation") != "analyze_metric":
        raise EvalConfigError(f"{case['id']}: unsupported engine operation")
    metric = request.get("metric", "raw")
    positions: list[float] | None = None
    if metric == "literal":
        values = [float(value) for value in request["values"]]
    else:
        spec = fixture["series"][0]
        monthly = materialize_monthly(fixture, spec)
        field = "views" if metric == "raw" else "share_per_million"
        eligible = [
            (index, row)
            for index, row in enumerate(monthly)
            if row["data_status"] not in EXCLUDED_STATUSES
        ]
        positions = [float(index) for index, _ in eligible]
        values = [float(row[field]) for _, row in eligible]
    actual = analyze_metric(
        values,
        positions=positions,
        bootstrap_samples=int(request.get("bootstrap_samples", 200)),
        seed=int(request.get("seed", 1)),
    )
    assertions = [
        evaluate_assertion(actual, assertion)
        for assertion in case["expected"].get("assertions", [])
    ]
    passed = all(result["passed"] for result in assertions)
    return {
        "case_id": case["id"],
        "layer": "engine",
        "status": "passed" if passed else "failed",
        "assertions": assertions,
    }


def run_planner_case(case: dict[str, Any]) -> dict[str, Any]:
    request = case["input"]
    materialized = materialize_analysis(
        case["fixture"],
        fixture_data=case["_fixture_data"],
        selected_metric=request.get("selected_metric", "raw"),
        question=request.get("question"),
        modules=request.get("modules", ["auto"]),
    )
    plan = materialized["analysis_plan"]
    actual_selected = {item["name"] for item in plan["selected_modules"]}
    expected_selected = set(case["expected"].get("selected_modules", []))
    actual_skipped = {item["name"] for item in plan["skipped_modules"]}
    expected_skipped = set(case["expected"].get("skipped_modules", []))
    assertions = [
        evaluate_assertion(materialized, assertion)
        for assertion in case["expected"].get("assertions", [])
    ]
    mode_match = plan["mode"] == case["expected"].get("mode")
    selected_match = actual_selected == expected_selected
    skipped_match = actual_skipped == expected_skipped
    passed = (
        mode_match
        and selected_match
        and skipped_match
        and all(result["passed"] for result in assertions)
    )
    return {
        "case_id": case["id"],
        "layer": "planner",
        "status": "passed" if passed else "failed",
        "mode": {"expected": case["expected"].get("mode"), "actual": plan["mode"]},
        "selected_modules": {
            "expected": sorted(expected_selected),
            "actual": sorted(actual_selected),
        },
        "skipped_modules": {
            "expected": sorted(expected_skipped),
            "actual": sorted(actual_skipped),
        },
        "module_counts": {
            "true_positive": len(actual_selected & expected_selected),
            "false_positive": len(actual_selected - expected_selected),
            "false_negative": len(expected_selected - actual_selected),
        },
        "assertions": assertions,
    }


def _deterministic_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(result["status"] == "passed" for result in results)
    planner = [result for result in results if result["layer"] == "planner"]
    tp = sum(result["module_counts"]["true_positive"] for result in planner)
    fp = sum(result["module_counts"]["false_positive"] for result in planner)
    fn = sum(result["module_counts"]["false_negative"] for result in planner)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    exact = sum(result["status"] == "passed" for result in planner)
    return {
        "case_count": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": passed / len(results) if results else 0.0,
        "planner": {
            "case_count": len(planner),
            "exact_match_rate": exact / len(planner) if planner else 1.0,
            "module_precision": precision,
            "module_recall": recall,
            "module_f1": (
                2 * precision * recall / (precision + recall)
                if precision + recall
                else 0.0
            ),
        },
    }


def run_deterministic(
    *,
    case_roots: list[Path] | None = None,
    layers: set[str] | None = None,
    case_ids: set[str] | None = None,
) -> dict[str, Any]:
    selected_layers = layers or {"engine", "planner"}
    cases = _case_selection(load_cases(case_roots), selected_layers, case_ids or set())
    results: list[dict[str, Any]] = []
    for case in cases:
        if case["layer"] == "engine":
            results.append(run_engine_case(case))
        elif case["layer"] == "planner":
            results.append(run_planner_case(case))
    return {
        "schema_version": "1.0",
        "run_id": run_id("deterministic"),
        "generated_at": utc_now(),
        "skill_sha256": skill_sha256(),
        "layers": sorted(selected_layers),
        "summary": _deterministic_summary(results),
        "cases": results,
    }


def _agent_public_request(case: dict[str, Any]) -> dict[str, Any]:
    request = case["input"]
    fixture = case["_fixture_data"]
    languages = [item["language"] for item in fixture.get("series", [])]
    start = str(fixture["start"]) + "-01"
    month_counts = [int(item["months"]) for item in fixture.get("series", [])]
    end_month = _month_add(str(fixture["start"]), max(month_counts or [1]) - 1)
    return {
        "schema_version": "1.1",
        "topic": f"Synthetic evaluation fixture: {case['fixture']}",
        "question": request.get("question"),
        "analysis_modules": request.get("modules", ["auto"]),
        "source_language": languages[0] if languages else "en",
        "languages": languages,
        "titles": {
            item["language"]: item["title"] for item in fixture.get("series", [])
        },
        "start": start,
        "end": end_month + "-28",
        "metric": request.get("selected_metric", "raw"),
        "weights": RANKING_WEIGHTS,
        "formats": ["json"],
        "bootstrap_samples": 300,
        "seed": 20260927,
        "allow_search_fallback": False,
    }


def _case_definition_digest(case: dict[str, Any]) -> str:
    definition = {
        key: value
        for key, value in case.items()
        if key not in {"definition_file", "_fixture_data", "layer"}
    }
    return canonical_digest(
        {
            "layer": case["layer"],
            "case": definition,
            "fixture": case.get("_fixture_data"),
        }
    )


def write_fixture_bundle(case: dict[str, Any], bundle_dir: Path) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    public_request = _agent_public_request(case)
    materialized = materialize_analysis(
        case["fixture"],
        fixture_data=case["_fixture_data"],
        selected_metric=case["input"].get("selected_metric", "raw"),
        question=case["input"].get("question"),
        modules=case["input"].get("modules", ["auto"]),
    )
    fixture = case["_fixture_data"]
    articles = [
        {
            "language": item["language"],
            "title": item["title"],
            "url": f"https://example.invalid/{item['language']}/{item['title'].replace(' ', '_')}",
            "resolution_method": "synthetic_fixture",
            "resolution_confidence": "high",
            "semantic_match": "synthetic evaluation fixture",
        }
        for item in fixture.get("series", [])
    ]
    resolution = {
        "schema_version": "1.0",
        "source": "synthetic_eval_fixture",
        "source_article": articles[0] if articles else None,
        "articles": articles,
        "warnings": fixture.get("warnings", []),
    }
    digest = canonical_digest(public_request)
    analysis = {
        "schema_version": "1.1",
        "tool_version": "synthetic-eval",
        "generated_at": utc_now(),
        "request": public_request,
        "resolution": {
            "source_article": resolution["source_article"],
            "warnings": resolution["warnings"],
        },
        "comparability": {
            "policy": "Synthetic fixture; all compared series share a calendar window."
        },
        "analysis_plan": materialized["analysis_plan"],
        "methodology": {
            "fixture": case["fixture"],
            "notice": "Synthetic evaluation data; do not treat as a real-world measurement.",
        },
        "series": materialized["series"],
        "findings": materialized["findings"],
        "warnings": materialized["warnings"],
        "caveats": [
            "The data is synthetic and exists only to evaluate reasoning behavior.",
            "Observed attention is not market demand or causal evidence.",
        ],
        "provenance": {
            "source": "synthetic_eval_fixture",
            "fixture": case["fixture"],
            "request_sha256": digest,
        },
        "artifacts": {
            "analysis": "analysis.json",
            "manifest": "manifest.json",
            "request": "request.json",
            "resolution": "resolution.json",
        },
    }
    manifest = {
        "schema_version": "1.1",
        "status": "complete",
        "tool_version": "synthetic-eval",
        "generated_at": utc_now(),
        "request_sha256": digest,
        "files": analysis["artifacts"],
        "api_sources": [],
        "warnings": analysis["warnings"],
    }
    write_json(bundle_dir / "request.json", public_request)
    write_json(bundle_dir / "resolution.json", resolution)
    write_json(bundle_dir / "analysis.json", analysis)
    write_json(bundle_dir / "manifest.json", manifest)


def prepare_agent(
    output_dir: Path,
    *,
    case_roots: list[Path] | None = None,
    case_ids: set[str] | None = None,
) -> dict[str, Any]:
    cases = _case_selection(load_cases(case_roots), {"agent"}, case_ids or set())
    if output_dir.exists() and any(output_dir.iterdir()):
        raise EvalConfigError(f"Preparation directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        SKILL_ROOT,
        output_dir / "skill",
        ignore=shutil.ignore_patterns(".DS_Store", "__pycache__", "*.pyc", "*.pyo"),
    )
    public_cases = []
    for case in cases:
        case_dir = output_dir / "cases" / case["id"]
        bundle_dir = case_dir / "input-bundle"
        output_bundle = case_dir / "output-bundle"
        case_dir.mkdir(parents=True, exist_ok=True)
        output_bundle.mkdir(parents=True, exist_ok=True)
        write_fixture_bundle(case, bundle_dir)
        prompt = (
            str(case["prompt"])
            .replace("{{BUNDLE_DIR}}", str(bundle_dir.resolve()))
            .replace("{{OUTPUT_DIR}}", str(output_bundle.resolve()))
        )
        (case_dir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
        result_template = {
            "schema_version": "1.0",
            "case_id": case["id"],
            "status": "not_run",
            "response_file": "response.md",
            "bundle_dir": (
                "output-bundle"
                if case["input"].get("execution_mode") == "workflow"
                else None
            ),
            "trace_file": "trace.json",
            "telemetry": {
                "model": None,
                "reasoning_effort": None,
                "latency_seconds": None,
                "tool_calls": None,
                "api_requests": None,
            },
            "judge_scores": {},
            "notes": None,
        }
        write_json(case_dir / "agent-result.template.json", result_template)
        public_cases.append(
            {
                "case_id": case["id"],
                "prompt_sha256": hashlib.sha256(
                    (prompt + "\n").encode("utf-8")
                ).hexdigest(),
                "case_definition_sha256": _case_definition_digest(case),
                "case_dir": str(case_dir.relative_to(output_dir)),
            }
        )
    manifest = {
        "schema_version": "1.0",
        "created_at": utc_now(),
        "skill_sha256": skill_sha256(),
        "case_count": len(cases),
        "cases": public_cases,
        "instructions": (
            "Give each agent only skill/, its case prompt, and its input bundle. "
            "Save the response and trace beside agent-result.json. Evaluator expectations "
            "and rubrics are intentionally absent."
        ),
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def _read_trace(path: Path | None) -> list[str]:
    if path is None or not path.is_file():
        return []
    payload = load_json(path)
    events = payload.get("events", []) if isinstance(payload, dict) else []
    return [
        str(event.get("name"))
        for event in events
        if isinstance(event, dict) and event.get("name")
    ]


def _ordered_subsequence(items: list[str], expected: list[str]) -> bool:
    cursor = 0
    for item in items:
        if cursor < len(expected) and item == expected[cursor]:
            cursor += 1
    return cursor == len(expected)


def _pattern_checks(
    text: str, patterns: list[str], *, present: bool
) -> list[dict[str, Any]]:
    checks = []
    for pattern in patterns:
        matched = (
            re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) is not None
        )
        checks.append(
            {
                "pattern": pattern,
                "passed": matched if present else not matched,
                "matched": matched,
            }
        )
    return checks


def _bundle_integrity(bundle_dir: Path | None, artifacts: list[str]) -> dict[str, Any]:
    if bundle_dir is None or not bundle_dir.is_dir():
        return {
            "passed": False,
            "request_digest_matches": False,
            "missing_artifacts": artifacts or ["bundle directory"],
        }
    missing = [name for name in artifacts if not (bundle_dir / name).is_file()]
    digest_matches = False
    request_path = bundle_dir / "request.json"
    manifest_path = bundle_dir / "manifest.json"
    if request_path.is_file() and manifest_path.is_file():
        request = load_json(request_path)
        manifest = load_json(manifest_path)
        digest_matches = manifest.get("request_sha256") == canonical_digest(request)
    return {
        "passed": not missing and digest_matches,
        "request_digest_matches": digest_matches,
        "missing_artifacts": missing,
    }


def _module_expectations(case: dict[str, Any], case_dir: Path) -> dict[str, Any]:
    analysis_path = case_dir / "input-bundle" / "analysis.json"
    if not analysis_path.is_file():
        return {"passed": False, "selected": [], "skipped": []}
    analysis = load_json(analysis_path)
    plan = analysis.get("analysis_plan", {})
    selected = {item["name"] for item in plan.get("selected_modules", [])}
    skipped = {item["name"] for item in plan.get("skipped_modules", [])}
    expected_selected = set(case["expected"].get("expected_modules", []))
    expected_skipped = set(case["expected"].get("expected_skipped_modules", []))
    return {
        "passed": selected == expected_selected and skipped == expected_skipped,
        "selected": sorted(selected),
        "skipped": sorted(skipped),
        "expected_selected": sorted(expected_selected),
        "expected_skipped": sorted(expected_skipped),
    }


def grade_agent_case(case: dict[str, Any], case_dir: Path) -> dict[str, Any]:
    result_path = case_dir / "agent-result.json"
    if not result_path.is_file():
        return {
            "case_id": case["id"],
            "layer": "agent",
            "status": "not_run",
            "weighted_score": 0.0,
            "hard_gates": {},
            "notes": ["agent-result.json is missing"],
        }
    result = load_json(result_path)
    if result.get("case_id") != case["id"]:
        raise EvalConfigError(f"{result_path}: case_id does not match directory")
    if result.get("status") != "completed":
        return {
            "case_id": case["id"],
            "layer": "agent",
            "status": result.get("status", "not_run"),
            "weighted_score": 0.0,
            "hard_gates": {},
            "notes": [result.get("notes") or "agent did not complete the case"],
        }
    response_path = case_dir / str(result.get("response_file", "response.md"))
    response = (
        response_path.read_text(encoding="utf-8") if response_path.is_file() else ""
    )
    trace_name = result.get("trace_file")
    trace_path = case_dir / str(trace_name) if trace_name else None
    trace = _read_trace(trace_path)
    required = _pattern_checks(
        response, list(case["expected"].get("required_patterns", [])), present=True
    )
    forbidden = _pattern_checks(
        response, list(case["expected"].get("forbidden_patterns", [])), present=False
    )
    expected_order = list(case["expected"].get("required_trace_order", []))
    trace_ok = _ordered_subsequence(trace, expected_order)
    modules = _module_expectations(case, case_dir)
    bundle_name = result.get("bundle_dir")
    output_bundle = case_dir / str(bundle_name) if bundle_name else None
    expected_artifacts = list(case["expected"].get("artifacts", []))
    output_integrity = _bundle_integrity(output_bundle, expected_artifacts)
    input_integrity = _bundle_integrity(
        case_dir / "input-bundle",
        ["request.json", "resolution.json", "analysis.json", "manifest.json"],
    )
    language = case["expected"].get("response_language")
    language_ok = True
    if language == "uk":
        cyrillic = len(re.findall(r"[А-Яа-яІіЇїЄєҐґ]", response))
        latin = len(re.findall(r"[A-Za-z]", response))
        language_ok = cyrillic >= 20 and cyrillic > latin

    applicable = list(case["expected"].get("hard_gates", []))
    hard_gates: dict[str, bool] = {}
    for gate in applicable:
        if gate == "analysis_read":
            hard_gates[gate] = "read_analysis" in trace
        elif gate in {"no_market_claim", "no_missing_as_zero"}:
            hard_gates[gate] = all(check["passed"] for check in forbidden)
        elif gate == "module_selection":
            hard_gates[gate] = modules["passed"] and all(
                check["passed"] for check in required
            )
        elif gate == "requested_artifacts":
            hard_gates[gate] = output_integrity["passed"]
        elif gate == "request_digest_matches":
            hard_gates[gate] = (
                output_integrity["request_digest_matches"]
                if output_bundle is not None
                else input_integrity["request_digest_matches"]
            )
        elif gate == "resolution_first":
            hard_gates[gate] = _ordered_subsequence(trace, ["resolve", "run"])
        else:
            hard_gates[gate] = False

    rubric = load_json(RUBRIC_ROOT / f"{case.get('rubric', 'agent-v1')}.json")
    weights = rubric["categories"]
    hard_gate_rate = sum(hard_gates.values()) / len(hard_gates) if hard_gates else 1.0
    required_rate = (
        sum(check["passed"] for check in required) / len(required) if required else 1.0
    )
    forbidden_rate = (
        sum(check["passed"] for check in forbidden) / len(forbidden)
        if forbidden
        else 1.0
    )
    artifact_rate = (
        1.0
        if case["input"].get("execution_mode") != "workflow"
        and input_integrity["passed"]
        else float(output_integrity["passed"])
    )
    category_fractions = {
        "methodological_correctness": hard_gate_rate,
        "planner_behavior": float(modules["passed"]),
        "evidence_and_uncertainty": required_rate,
        "grounded_interpretation": forbidden_rate * float(language_ok),
        "artifacts_and_provenance": artifact_rate,
        "efficiency": 1.0 if result.get("telemetry") else 0.5,
    }
    category_scores = {
        name: float(weights[name]) * category_fractions.get(name, 0.0)
        for name in weights
    }
    for name, judge_score in (result.get("judge_scores") or {}).items():
        if name not in weights:
            raise EvalConfigError(f"{result_path}: unknown judge category {name!r}")
        score = float(judge_score)
        if not 0 <= score <= float(weights[name]):
            raise EvalConfigError(
                f"{result_path}: judge score for {name} must be 0..{weights[name]}"
            )
        category_scores[name] = score
    weighted_score = sum(category_scores.values())
    passed = (
        bool(response)
        and trace_ok
        and language_ok
        and all(check["passed"] for check in required + forbidden)
        and all(hard_gates.values())
        and weighted_score >= 85.0
    )
    return {
        "case_id": case["id"],
        "layer": "agent",
        "status": "passed" if passed else "failed",
        "weighted_score": weighted_score,
        "category_scores": category_scores,
        "hard_gates": hard_gates,
        "checks": {
            "required_patterns": required,
            "forbidden_patterns": forbidden,
            "trace_order": trace_ok,
            "language": language_ok,
            "modules": modules,
            "input_bundle": input_integrity,
            "output_bundle": output_integrity,
        },
        "telemetry": result.get("telemetry", {}),
    }


def grade_agent(
    prepared_dir: Path,
    *,
    case_roots: list[Path] | None = None,
    case_ids: set[str] | None = None,
) -> dict[str, Any]:
    cases = _case_selection(load_cases(case_roots), {"agent"}, case_ids or set())
    prepared_manifest_path = prepared_dir / "manifest.json"
    if not prepared_manifest_path.is_file():
        raise EvalConfigError(f"Prepared manifest is missing: {prepared_manifest_path}")
    prepared_manifest = load_json(prepared_manifest_path)
    prepared_cases = {
        item["case_id"]: item for item in prepared_manifest.get("cases", [])
    }
    for case in cases:
        recorded = prepared_cases.get(case["id"])
        if recorded is None:
            raise EvalConfigError(f"Prepared case is missing: {case['id']}")
        if recorded.get("case_definition_sha256") != _case_definition_digest(case):
            raise EvalConfigError(
                f"Case definition changed after preparation: {case['id']}"
            )
        prompt_path = prepared_dir / "cases" / case["id"] / "prompt.txt"
        prompt_digest = (
            hashlib.sha256(prompt_path.read_bytes()).hexdigest()
            if prompt_path.is_file()
            else None
        )
        if recorded.get("prompt_sha256") != prompt_digest:
            raise EvalConfigError(
                f"Prepared prompt changed or is missing: {case['id']}"
            )
    results = [
        grade_agent_case(case, prepared_dir / "cases" / case["id"]) for case in cases
    ]
    completed = [
        result for result in results if result["status"] in {"passed", "failed"}
    ]
    hard_values = [
        value for result in completed for value in result.get("hard_gates", {}).values()
    ]
    summary = {
        "case_count": len(results),
        "completed": len(completed),
        "passed": sum(result["status"] == "passed" for result in results),
        "failed": sum(result["status"] == "failed" for result in results),
        "not_run": sum(result["status"] == "not_run" for result in results),
        "blocked": sum(result["status"] == "blocked" for result in results),
        "infrastructure_error": sum(
            result["status"] == "infrastructure_error" for result in results
        ),
        "weighted_score_mean": (
            sum(float(result["weighted_score"]) for result in completed)
            / len(completed)
            if completed
            else 0.0
        ),
        "hard_gate_pass_rate": (
            sum(hard_values) / len(hard_values) if hard_values else 0.0
        ),
    }
    return {
        "schema_version": "1.0",
        "run_id": run_id("agent"),
        "generated_at": utc_now(),
        "skill_sha256": prepared_manifest.get("skill_sha256"),
        "layers": ["agent"],
        "summary": summary,
        "cases": results,
    }


def _live_failure_status(output: str, *, timed_out: bool = False) -> str:
    lowered = output.casefold()
    if timed_out or any(term in lowered for term in NETWORK_ERROR_TERMS):
        return "infrastructure_error"
    return "product_failure"


def run_live_case(case: dict[str, Any], output_root: Path) -> dict[str, Any]:
    case_output = output_root / case["id"]
    case_output.mkdir(parents=True, exist_ok=True)
    logs: list[dict[str, Any]] = []
    status = "passed"
    started = time.monotonic()
    timeout_seconds = int(case["input"].get("timeout_seconds", 180))
    for index, raw_args in enumerate(case["input"]["steps"], 1):
        args = [
            str(value).replace("{{OUTPUT_DIR}}", str(case_output.resolve()))
            for value in raw_args
        ]
        command = [sys.executable, str(SCRIPTS_ROOT / "analyze.py"), *args]
        try:
            completed = subprocess.run(
                command,
                cwd=WORKSPACE_ROOT,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            combined = completed.stdout + "\n" + completed.stderr
            logs.append(
                {
                    "step": index,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout[-4000:],
                    "stderr": completed.stderr[-4000:],
                }
            )
            if completed.returncode != 0:
                status = _live_failure_status(combined)
                break
        except subprocess.TimeoutExpired as error:
            logs.append(
                {
                    "step": index,
                    "returncode": None,
                    "stdout": str(error.stdout or "")[-4000:],
                    "stderr": str(error.stderr or "")[-4000:],
                }
            )
            status = _live_failure_status("", timed_out=True)
            break
    expected_artifacts = list(case["expected"].get("artifacts", []))
    integrity = _bundle_integrity(case_output, expected_artifacts)
    missing = integrity["missing_artifacts"]
    if status == "passed" and not integrity["passed"]:
        status = "product_failure"
    return {
        "case_id": case["id"],
        "layer": "live",
        "status": status,
        "latency_seconds": time.monotonic() - started,
        "missing_artifacts": missing,
        "request_digest_matches": integrity["request_digest_matches"],
        "logs": logs,
    }


def run_live(
    output_root: Path,
    *,
    case_roots: list[Path] | None = None,
    case_ids: set[str] | None = None,
) -> dict[str, Any]:
    cases = _case_selection(load_cases(case_roots), {"live"}, case_ids or set())
    results = [run_live_case(case, output_root) for case in cases]
    summary = {
        "case_count": len(results),
        "passed": sum(result["status"] == "passed" for result in results),
        "product_failure": sum(
            result["status"] == "product_failure" for result in results
        ),
        "infrastructure_error": sum(
            result["status"] == "infrastructure_error" for result in results
        ),
    }
    return {
        "schema_version": "1.0",
        "run_id": run_id("live"),
        "generated_at": utc_now(),
        "skill_sha256": skill_sha256(),
        "layers": ["live"],
        "summary": summary,
        "cases": results,
    }


def _baseline_regressions(
    current: dict[str, Any], baseline: dict[str, Any]
) -> list[str]:
    old = {case["case_id"]: case.get("status") for case in baseline.get("cases", [])}
    return [
        case["case_id"]
        for case in current.get("cases", [])
        if old.get(case["case_id"]) == "passed" and case.get("status") != "passed"
    ]


def check_release(
    deterministic: dict[str, Any],
    *,
    agent: dict[str, Any] | None,
    live: dict[str, Any] | None,
    baseline: dict[str, Any] | None,
    gates: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, actual: Any, expected: str, passed: bool) -> None:
        checks.append(
            {"name": name, "actual": actual, "expected": expected, "passed": passed}
        )

    deterministic_summary = deterministic.get("summary", {})
    planner = deterministic_summary.get("planner", {})
    current_skill_digest = skill_sha256()
    deterministic_digest = deterministic.get("skill_sha256")
    add(
        "deterministic_skill_is_current",
        deterministic_digest,
        f"= {current_skill_digest}",
        deterministic_digest == current_skill_digest,
    )
    add(
        "deterministic_pass_rate",
        deterministic_summary.get("pass_rate"),
        f">= {gates['deterministic_pass_rate_min']}",
        float(deterministic_summary.get("pass_rate", 0.0))
        >= float(gates["deterministic_pass_rate_min"]),
    )
    for key, gate_key in (
        ("exact_match_rate", "planner_exact_match_min"),
        ("module_precision", "planner_module_precision_min"),
        ("module_recall", "planner_module_recall_min"),
    ):
        add(
            f"planner_{key}",
            planner.get(key),
            f">= {gates[gate_key]}",
            float(planner.get(key, 0.0)) >= float(gates[gate_key]),
        )
    incomplete: list[str] = []
    if agent is None:
        incomplete.append("agent evaluation summary is required")
    else:
        agent_summary = agent.get("summary", {})
        add(
            "agent_skill_matches_deterministic",
            agent.get("skill_sha256"),
            f"= {deterministic_digest}",
            agent.get("skill_sha256") == deterministic_digest,
        )
        if int(agent_summary.get("completed", 0)) < int(
            agent_summary.get("case_count", 0)
        ):
            incomplete.append("not all agent cases were completed")
        add(
            "agent_hard_gate_pass_rate",
            agent_summary.get("hard_gate_pass_rate"),
            f">= {gates['critical_agent_hard_gate_pass_rate_min']}",
            float(agent_summary.get("hard_gate_pass_rate", 0.0))
            >= float(gates["critical_agent_hard_gate_pass_rate_min"]),
        )
        add(
            "agent_weighted_score_mean",
            agent_summary.get("weighted_score_mean"),
            f">= {gates['agent_weighted_score_min']}",
            float(agent_summary.get("weighted_score_mean", 0.0))
            >= float(gates["agent_weighted_score_min"]),
        )
        add(
            "agent_failed_cases",
            agent_summary.get("failed"),
            f"<= {gates['maximum_agent_failed_cases']}",
            int(agent_summary.get("failed", 0))
            <= int(gates["maximum_agent_failed_cases"]),
        )
    regressions = (
        _baseline_regressions(deterministic, baseline) if baseline is not None else []
    )
    add(
        "regressions",
        len(regressions),
        f"<= {gates['maximum_regressions']}",
        len(regressions) <= int(gates["maximum_regressions"]),
    )
    live_policy = gates.get("live_policy", {})
    if live is not None:
        live_summary = live.get("summary", {})
        add(
            "live_skill_matches_deterministic",
            live.get("skill_sha256"),
            f"= {deterministic_digest}",
            live.get("skill_sha256") == deterministic_digest,
        )
        if live_policy.get("not_run_is_incomplete") and not live.get("cases"):
            incomplete.append("live evaluation contains no cases")
        if live_policy.get("product_failure_blocks_release"):
            add(
                "live_product_failures",
                live_summary.get("product_failure", 0),
                "= 0",
                int(live_summary.get("product_failure", 0)) == 0,
            )
    failed = [check["name"] for check in checks if not check["passed"]]
    status = "incomplete" if incomplete else "failed" if failed else "passed"
    return {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "status": status,
        "checks": checks,
        "regressions": regressions,
        "failures": failed,
        "incomplete_reasons": incomplete,
    }


def _case_roots(values: list[str] | None) -> list[Path] | None:
    return [Path(value).resolve() for value in values] if values else None


def _default_result(label: str) -> Path:
    return RESULTS_ROOT / f"{run_id(label)}.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate case definitions")
    validate.add_argument("--case-root", action="append")

    deterministic = subparsers.add_parser(
        "run-deterministic", help="Run engine and planner cases"
    )
    deterministic.add_argument(
        "--layer", action="append", choices=("engine", "planner")
    )
    deterministic.add_argument("--case-id", action="append")
    deterministic.add_argument("--case-root", action="append")
    deterministic.add_argument("--output", type=Path)

    prepare = subparsers.add_parser(
        "prepare-agent", help="Prepare blind agent workspaces"
    )
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--case-id", action="append")
    prepare.add_argument("--case-root", action="append")

    grade = subparsers.add_parser("grade-agent", help="Grade prepared agent results")
    grade.add_argument("--prepared", type=Path, required=True)
    grade.add_argument("--case-id", action="append")
    grade.add_argument("--case-root", action="append")
    grade.add_argument("--output", type=Path)

    live = subparsers.add_parser("run-live", help="Run opt-in network smoke cases")
    live.add_argument("--allow-network", action="store_true")
    live.add_argument("--case-id", action="append")
    live.add_argument("--case-root", action="append")
    live.add_argument("--output-dir", type=Path, required=True)
    live.add_argument("--summary-output", type=Path)

    release = subparsers.add_parser("check-release", help="Apply release gates")
    release.add_argument("--deterministic", type=Path, required=True)
    release.add_argument("--agent", type=Path)
    release.add_argument("--live", type=Path)
    release.add_argument("--baseline", type=Path)
    release.add_argument(
        "--gates", type=Path, default=RUBRIC_ROOT / "release-gates-v1.json"
    )
    release.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            cases = load_cases(_case_roots(args.case_root))
            counts = {
                layer: sum(case["layer"] == layer for case in cases)
                for layer in ("engine", "planner", "agent", "live")
            }
            print(json.dumps({"status": "valid", "counts": counts}, indent=2))
            return 0
        if args.command == "run-deterministic":
            payload = run_deterministic(
                case_roots=_case_roots(args.case_root),
                layers=set(args.layer or ["engine", "planner"]),
                case_ids=set(args.case_id or []),
            )
            output = (args.output or _default_result("deterministic")).resolve()
            write_json(output, payload)
            print(json.dumps({"output": str(output), **payload["summary"]}, indent=2))
            return 0 if payload["summary"]["failed"] == 0 else 1
        if args.command == "prepare-agent":
            payload = prepare_agent(
                args.output.resolve(),
                case_roots=_case_roots(args.case_root),
                case_ids=set(args.case_id or []),
            )
            print(
                json.dumps({"output": str(args.output.resolve()), **payload}, indent=2)
            )
            return 0
        if args.command == "grade-agent":
            payload = grade_agent(
                args.prepared.resolve(),
                case_roots=_case_roots(args.case_root),
                case_ids=set(args.case_id or []),
            )
            output = (args.output or _default_result("agent")).resolve()
            write_json(output, payload)
            print(json.dumps({"output": str(output), **payload["summary"]}, indent=2))
            if payload["summary"]["completed"] < payload["summary"]["case_count"]:
                return 2
            return 0 if payload["summary"]["failed"] == 0 else 1
        if args.command == "run-live":
            if not args.allow_network:
                raise EvalConfigError(
                    "Live cases require explicit --allow-network; deterministic and agent-fixture cases do not."
                )
            payload = run_live(
                args.output_dir.resolve(),
                case_roots=_case_roots(args.case_root),
                case_ids=set(args.case_id or []),
            )
            output = (args.summary_output or _default_result("live")).resolve()
            write_json(output, payload)
            print(json.dumps({"output": str(output), **payload["summary"]}, indent=2))
            return 0 if payload["summary"]["product_failure"] == 0 else 1
        if args.command == "check-release":
            payload = check_release(
                load_json(args.deterministic.resolve()),
                agent=load_json(args.agent.resolve()) if args.agent else None,
                live=load_json(args.live.resolve()) if args.live else None,
                baseline=load_json(args.baseline.resolve()) if args.baseline else None,
                gates=load_json(args.gates.resolve()),
            )
            if args.output:
                write_json(args.output.resolve(), payload)
            print(json.dumps(payload, indent=2))
            return (
                0
                if payload["status"] == "passed"
                else 2
                if payload["status"] == "incomplete"
                else 1
            )
    except EvalConfigError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
