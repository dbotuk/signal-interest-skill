#!/usr/bin/env python3
"""Analyze interest signals and create an auditable report bundle."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any

from interest_signals import __version__
from interest_signals.artifacts import write_artifacts, write_json
from interest_signals.client import ApiError, JsonClient
from interest_signals.metrics import (
    aggregate_monthly,
    analyze_series,
    build_findings,
    default_date_range,
    fetch_article_daily,
    fetch_project_daily,
    parse_date,
    rank_series,
    validate_date_range,
)
from interest_signals.planner import OPTIONAL_MODULES, apply_analysis_plan
from interest_signals.resolve import resolve_topic


DEFAULT_USER_AGENT = (
    "InterestSignalsSkill/1.1 "
    "(https://gist.github.com/edugenesis/84f332ba58642cf12110b196775a8b72)"
)
ALLOWED_CONFIG_KEYS = {
    "topic",
    "question",
    "analysis_modules",
    "source_language",
    "languages",
    "titles",
    "start",
    "end",
    "metric",
    "weights",
    "formats",
    "output_dir",
    "user_agent",
    "bootstrap_samples",
    "seed",
    "allow_search_fallback",
    "refresh",
    "resolve_only",
}
LANGUAGE_RE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")
RUN_FILE_NAMES = {
    "request.json",
    "resolution.json",
    "analysis.json",
    "manifest.json",
    "series.csv",
    "trend.svg",
    "report.html",
    "report.pdf",
}


class ConfigError(ValueError):
    """Raised when the request contract is invalid."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze interest signals over time and write auditable "
            "JSON/CSV/SVG/HTML/PDF outputs. The current source is Wikimedia Pageviews."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", type=Path, help="JSON request file")
    parser.add_argument("--topic", help="Human topic or source-language search phrase")
    parser.add_argument(
        "--question",
        help="Research question used to select optional analysis modules",
    )
    parser.add_argument(
        "--source-language", help="Wikipedia language code used to identify the concept"
    )
    parser.add_argument(
        "--languages", help="Comma-separated Wikipedia language codes to compare"
    )
    parser.add_argument(
        "--title",
        action="append",
        default=[],
        metavar="LANG=ARTICLE",
        help="Exact article title override; repeat for multiple languages",
    )
    parser.add_argument("--start", help="Inclusive UTC start date, YYYY-MM-DD")
    parser.add_argument("--end", help="Inclusive UTC end date, YYYY-MM-DD")
    parser.add_argument(
        "--metric",
        choices=("raw", "share"),
        help="Primary trend metric; share normalizes by total project pageviews",
    )
    parser.add_argument(
        "--weight",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Ranking weight for growth, volume, or reliability",
    )
    parser.add_argument(
        "--formats",
        help="Comma-separated report outputs from csv,svg,html,pdf",
    )
    parser.add_argument(
        "--analysis-modules",
        help=(
            "Comma-separated optional modules: auto,seasonality,changepoints,"
            "anomalies,stability"
        ),
    )
    parser.add_argument("--output-dir", type=Path, help="Directory for the run bundle")
    parser.add_argument("--user-agent", help="Descriptive Wikimedia User-Agent")
    parser.add_argument(
        "--bootstrap-samples", type=int, help="Residual block-bootstrap iterations"
    )
    parser.add_argument("--seed", type=int, help="Deterministic bootstrap seed")
    parser.add_argument(
        "--allow-search-fallback",
        action="store_true",
        help="Search a target wiki when no interlanguage link exists; verify manually",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="Ignore cached API responses"
    )
    parser.add_argument(
        "--resolve-only",
        action="store_true",
        help="Write title mappings, then stop before downloading pageviews",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _load_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"Cannot read config {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ConfigError(f"Config is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ConfigError("Config root must be a JSON object")
    unknown = sorted(set(payload) - ALLOWED_CONFIG_KEYS)
    if unknown:
        raise ConfigError("Unknown config key(s): " + ", ".join(unknown))
    return payload


def _parse_assignments(
    values: list[str], *, allowed: set[str] | None = None
) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ConfigError(f"Expected NAME=VALUE, got {value!r}")
        key, item = value.split("=", 1)
        key = key.strip().lower()
        item = item.strip()
        if not key or not item:
            raise ConfigError(f"Expected non-empty NAME=VALUE, got {value!r}")
        if allowed is not None and key not in allowed:
            raise ConfigError(
                f"Unknown name {key!r}; expected one of {', '.join(sorted(allowed))}"
            )
        parsed[key] = item
    return parsed


def _pick(cli_value: Any, config: dict[str, Any], key: str, default: Any) -> Any:
    return cli_value if cli_value is not None else config.get(key, default)


def _require_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a JSON boolean")
    return value


def _finite_float(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{label} must be a finite number") from error
    if not math.isfinite(number):
        raise ConfigError(f"{label} must be a finite number")
    return number


def normalize_request(args: argparse.Namespace) -> dict[str, Any]:
    source = _load_config(args.config)
    topic = _pick(args.topic, source, "topic", None)
    if not isinstance(topic, str) or not topic.strip():
        raise ConfigError("--topic is required (or set topic in --config)")
    topic = topic.strip()
    question_value = _pick(args.question, source, "question", None)
    if question_value is not None and not isinstance(question_value, str):
        raise ConfigError("question must be a string or null")
    question = question_value.strip() if isinstance(question_value, str) else None
    if not question:
        question = None
    if question is not None and len(question) > 1000:
        raise ConfigError("question must not exceed 1000 characters")

    source_language_value = _pick(args.source_language, source, "source_language", "en")
    if not isinstance(source_language_value, str):
        raise ConfigError("source_language must be a string")
    source_language = source_language_value.strip().lower()
    language_value = _pick(args.languages, source, "languages", None)
    if isinstance(language_value, str):
        languages = [value.strip().lower() for value in language_value.split(",")]
    elif isinstance(language_value, list):
        if not all(isinstance(value, str) for value in language_value):
            raise ConfigError("languages must contain only strings")
        languages = [value.strip().lower() for value in language_value]
    else:
        raise ConfigError("--languages is required (or set languages in --config)")
    languages = list(dict.fromkeys(value for value in languages if value))
    if not languages:
        raise ConfigError("At least one language is required")
    if len(languages) > 12:
        raise ConfigError(
            "A run is limited to 12 languages for readable reports and polite API use"
        )
    for language in [source_language, *languages]:
        if not LANGUAGE_RE.fullmatch(language):
            raise ConfigError(f"Invalid Wikipedia language code: {language!r}")

    titles = source.get("titles", {})
    if not isinstance(titles, dict):
        raise ConfigError(
            "titles must be a JSON object mapping language to article title"
        )
    if not all(
        isinstance(key, str) and isinstance(value, str) for key, value in titles.items()
    ):
        raise ConfigError(
            "titles must map string language codes to string article titles"
        )
    titles = {key.lower(): value for key, value in titles.items()}
    titles.update(_parse_assignments(args.title))
    for language, title in titles.items():
        if not LANGUAGE_RE.fullmatch(language) or not title.strip():
            raise ConfigError(f"Invalid title mapping: {language}={title!r}")
    unused_title_languages = sorted(set(titles) - set(languages) - {source_language})
    if unused_title_languages:
        raise ConfigError(
            "Title override language is neither requested nor the source language: "
            + ", ".join(unused_title_languages)
        )

    default_start, default_end = default_date_range()
    start_text = _pick(args.start, source, "start", default_start.isoformat())
    end_text = _pick(args.end, source, "end", default_end.isoformat())
    if not isinstance(start_text, str) or not isinstance(end_text, str):
        raise ConfigError("start and end must be YYYY-MM-DD strings")
    start = parse_date(start_text)
    end = parse_date(end_text)
    if end > date.today():
        raise ConfigError("End date cannot be in the future")
    validate_date_range(start, end)

    metric_default = "share" if len(languages) > 1 else "raw"
    metric_value = _pick(args.metric, source, "metric", metric_default)
    if not isinstance(metric_value, str):
        raise ConfigError("metric must be raw or share")
    metric = metric_value.lower()
    if metric not in {"raw", "share"}:
        raise ConfigError("metric must be raw or share")

    weights: dict[str, float] = {
        "growth": 0.50,
        "volume": 0.20,
        "reliability": 0.30,
    }
    config_weights = source.get("weights", {})
    if not isinstance(config_weights, dict):
        raise ConfigError("weights must be a JSON object")
    for key, value in config_weights.items():
        if key not in weights:
            raise ConfigError(f"Unknown ranking weight: {key}")
        weights[key] = _finite_float(value, label=f"weight {key}")
    for key, value in _parse_assignments(args.weight, allowed=set(weights)).items():
        weights[key] = _finite_float(value, label=f"weight {key}")
    if any(value < 0 for value in weights.values()) or sum(weights.values()) <= 0:
        raise ConfigError("Ranking weights must be non-negative and not all zero")

    formats_value = _pick(
        args.formats, source, "formats", ["csv", "svg", "html", "pdf"]
    )
    if isinstance(formats_value, str):
        formats = [value.strip().lower() for value in formats_value.split(",")]
    elif isinstance(formats_value, list):
        if not all(isinstance(value, str) for value in formats_value):
            raise ConfigError("formats must contain only strings")
        formats = [value.strip().lower() for value in formats_value]
    else:
        raise ConfigError("formats must be a list or comma-separated string")
    formats = list(dict.fromkeys(value for value in formats if value))
    invalid_formats = sorted(set(formats) - {"csv", "svg", "html", "pdf"})
    if invalid_formats:
        raise ConfigError("Unknown format(s): " + ", ".join(invalid_formats))

    modules_value = _pick(args.analysis_modules, source, "analysis_modules", ["auto"])
    if isinstance(modules_value, str):
        analysis_modules = [value.strip().lower() for value in modules_value.split(",")]
    elif isinstance(modules_value, list):
        if not all(isinstance(value, str) for value in modules_value):
            raise ConfigError("analysis_modules must contain only strings")
        analysis_modules = [value.strip().lower() for value in modules_value]
    else:
        raise ConfigError("analysis_modules must be a list or comma-separated string")
    analysis_modules = list(dict.fromkeys(value for value in analysis_modules if value))
    if not analysis_modules:
        raise ConfigError("analysis_modules must contain auto or an optional module")
    allowed_modules = {"auto", *OPTIONAL_MODULES}
    invalid_modules = sorted(set(analysis_modules) - allowed_modules)
    if invalid_modules:
        raise ConfigError("Unknown analysis module(s): " + ", ".join(invalid_modules))
    if "auto" in analysis_modules and len(analysis_modules) > 1:
        raise ConfigError("auto cannot be combined with explicit analysis modules")

    output_dir_value = _pick(args.output_dir, source, "output_dir", Path("run-output"))
    if not isinstance(output_dir_value, (str, Path)):
        raise ConfigError("output_dir must be a path string")
    output_dir = Path(output_dir_value).expanduser().resolve()
    user_agent_value = _pick(args.user_agent, source, "user_agent", DEFAULT_USER_AGENT)
    if not isinstance(user_agent_value, str) or not user_agent_value.strip():
        raise ConfigError("user_agent must be a non-empty string")
    user_agent = user_agent_value.strip()
    bootstrap_samples_value = _pick(
        args.bootstrap_samples, source, "bootstrap_samples", 600
    )
    if isinstance(bootstrap_samples_value, bool) or not isinstance(
        bootstrap_samples_value, int
    ):
        raise ConfigError("bootstrap_samples must be an integer")
    bootstrap_samples = bootstrap_samples_value
    if not 0 <= bootstrap_samples <= 10_000:
        raise ConfigError("bootstrap_samples must be between 0 and 10000")
    seed_value = _pick(args.seed, source, "seed", 1729)
    if isinstance(seed_value, bool) or not isinstance(seed_value, int):
        raise ConfigError("seed must be an integer")
    seed = seed_value
    allow_search_fallback = args.allow_search_fallback or _require_bool(
        source, "allow_search_fallback"
    )
    refresh = args.refresh or _require_bool(source, "refresh")
    resolve_only = args.resolve_only or _require_bool(source, "resolve_only")

    return {
        "topic": topic,
        "question": question,
        "analysis_modules": analysis_modules,
        "source_language": source_language,
        "languages": languages,
        "titles": titles,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "metric": metric,
        "weights": weights,
        "formats": formats,
        "output_dir": str(output_dir),
        "user_agent": user_agent,
        "bootstrap_samples": bootstrap_samples,
        "seed": seed,
        "allow_search_fallback": allow_search_fallback,
        "refresh": refresh,
        "resolve_only": resolve_only,
    }


def _creation_date(article: dict[str, Any]) -> date | None:
    value = article.get("created_at")
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _public_request(request: dict[str, Any]) -> dict[str, Any]:
    # The output path and cache controls are operational details, not methodology.
    return {
        key: value
        for key, value in request.items()
        if key not in {"output_dir", "refresh", "resolve_only", "user_agent"}
    }


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _publish_bundle(staging_dir: Path, output_dir: Path) -> None:
    """Publish a fully staged bundle, making the manifest the commit marker."""

    staged_names = {path.name for path in staging_dir.iterdir() if path.is_file()}
    if "manifest.json" not in staged_names:
        raise RuntimeError("Refusing to publish a run without manifest.json")
    unexpected = staged_names - RUN_FILE_NAMES
    if unexpected:
        raise RuntimeError(
            "Refusing to publish unexpected staged file(s): "
            + ", ".join(sorted(unexpected))
        )

    # Absence of the manifest explicitly marks the tiny commit window as
    # incomplete. A failed run never reaches this function, so the previous
    # complete bundle remains untouched.
    (output_dir / "manifest.json").unlink(missing_ok=True)
    for name in sorted(RUN_FILE_NAMES - {"manifest.json"}):
        staged_path = staging_dir / name
        final_path = output_dir / name
        if staged_path.is_file():
            os.replace(staged_path, final_path)
        else:
            final_path.unlink(missing_ok=True)
    os.replace(staging_dir / "manifest.json", output_dir / "manifest.json")


def _shared_analysis_months(
    monthly_sets: list[list[dict[str, Any]]],
) -> set[str] | None:
    """Return one comparable calendar-month set for a multi-series run."""

    if len(monthly_sets) <= 1:
        return None
    excluded = {"not_created", "creation_partial", "project_data_gap", "provisional"}
    complete_sets = [
        {
            row["month"]
            for row in monthly
            if row["complete_month"] and row["data_status"] not in excluded
        }
        for monthly in monthly_sets
    ]
    shared = set.intersection(*complete_sets) if complete_sets else set()
    if not shared:
        eligible_sets = [
            {row["month"] for row in monthly if row["data_status"] not in excluded}
            for monthly in monthly_sets
        ]
        shared = set.intersection(*eligible_sets) if eligible_sets else set()
    if not shared:
        raise RuntimeError(
            "No shared eligible calendar months exist across the resolved articles. "
            "Choose a later or wider date range, or compare the mappings separately."
        )
    return shared


def run(request: dict[str, Any]) -> dict[str, Any]:
    output_dir = Path(request["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=".run-", dir=output_dir))
    public_request = _public_request(request)
    try:
        write_json(staging_dir / "request.json", public_request)

        if (
            not request["resolve_only"]
            and "pdf" in request["formats"]
            and importlib.util.find_spec("reportlab") is None
        ):
            raise RuntimeError(
                "PDF was requested but ReportLab is unavailable. From the skill directory run "
                "`python -m pip install -r requirements.lock.txt`, or omit pdf in --formats."
            )

        client = JsonClient(
            output_dir / ".cache",
            request["user_agent"],
            refresh=request["refresh"],
        )
        resolution = resolve_topic(
            client,
            topic=request["topic"],
            source_language=request["source_language"],
            languages=request["languages"],
            title_overrides=request["titles"],
            allow_search_fallback=request["allow_search_fallback"],
        )
        write_json(staging_dir / "resolution.json", resolution)
        generated_at = datetime.now(UTC).isoformat()
        request_sha256 = _digest(public_request)
        if request["resolve_only"]:
            resolved_files = {
                "manifest": "manifest.json",
                "request": "request.json",
                "resolution": "resolution.json",
            }
            write_json(
                staging_dir / "manifest.json",
                {
                    "schema_version": "1.1",
                    "status": "resolved",
                    "tool_version": __version__,
                    "generated_at": generated_at,
                    "request_sha256": request_sha256,
                    "files": resolved_files,
                    "api_sources": client.sources,
                    "warnings": resolution["warnings"],
                },
            )
            _publish_bundle(staging_dir, output_dir)
            result = {
                "status": "resolved",
                "output_dir": str(output_dir),
                "resolution": str(output_dir / "resolution.json"),
                "files": resolved_files,
                "warnings": resolution["warnings"],
            }
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return result

        start = parse_date(request["start"])
        end = parse_date(request["end"])
        monthly_inputs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        project_cache: dict[str, tuple[dict[date, int], set[date]]] = {}
        for article in resolution["articles"]:
            language = article["language"]
            article_daily, article_observed = fetch_article_daily(
                client,
                language=language,
                title=article["title"],
                start=start,
                end=end,
            )
            if language not in project_cache:
                project_cache[language] = fetch_project_daily(
                    client, language=language, start=start, end=end
                )
            project_daily, project_observed = project_cache[language]
            monthly = aggregate_monthly(
                article_daily,
                project_daily,
                start=start,
                end=end,
                article_observed=article_observed,
                project_observed=project_observed,
                created_at=_creation_date(article),
            )
            monthly_inputs.append((article, monthly))

        shared_months = _shared_analysis_months(
            [monthly for _, monthly in monthly_inputs]
        )
        series = [
            analyze_series(
                article=article,
                monthly=monthly,
                selected_metric=request["metric"],
                bootstrap_samples=request["bootstrap_samples"],
                base_seed=request["seed"],
                analysis_months=shared_months,
            )
            for article, monthly in monthly_inputs
        ]
        series = rank_series(series, request["weights"])
        analysis_plan, adaptive_findings = apply_analysis_plan(
            series,
            question=request["question"],
            requested_modules=request["analysis_modules"],
        )
        warnings = list(resolution["warnings"])
        for item in series:
            warnings.extend(
                f"{item['language']}: {warning}" for warning in item["warnings"]
            )
        if len(series) > 6:
            warnings.append(
                "The one-page PDF shows the top six languages; JSON and CSV contain all series."
            )
        analysis: dict[str, Any] = {
            "schema_version": "1.1",
            "tool_version": __version__,
            "generated_at": generated_at,
            "request": public_request,
            "resolution": {
                "source_article": resolution["source_article"],
                "warnings": resolution["warnings"],
            },
            "comparability": {
                "shared_analysis_months": (
                    sorted(shared_months) if shared_months is not None else None
                ),
                "policy": (
                    "All compared series use the intersection of eligible calendar months."
                    if shared_months is not None
                    else "The single series uses its eligible calendar months."
                ),
            },
            "analysis_plan": analysis_plan,
            "methodology": {
                "traffic_filter": "all-access/user",
                "aggregation": "UTC daily observations rolled into calendar months",
                "cross_language_metric": (
                    "article views per 1,000,000 total views in the same language project"
                ),
                "trend": (
                    "OLS slope of log1p monthly values against original calendar-month ordinals, "
                    "annualized"
                ),
                "trend_interval": (
                    "95% residual moving-block bootstrap interval with three-month blocks; "
                    "suppressed below 100 resamples"
                ),
                "ranking": (
                    "weighted screen across anchored growth (-25%=0, 0%=0.5, +25%=1), "
                    "log absolute volume (five-order scale), and evidence quality"
                ),
                "adaptive_analysis": (
                    "Question-aware optional modules are selected explicitly or by "
                    "intent, then gated by minimum data requirements."
                ),
            },
            "series": series,
            "findings": adaptive_findings + build_findings(series),
            "warnings": sorted(set(warnings)),
            "caveats": [
                "Wikipedia pageviews measure attention, not unique people, willingness to pay, or market size.",
                "Bot classification is heuristic; the user filter excludes identified automated traffic only.",
                "Current article titles do not automatically include views credited to redirects or old titles.",
                "Cross-language normalization reduces project-size bias but does not adjust for population or internet access.",
                "A title mapping can be semantically imperfect; inspect resolution.json before acting on results.",
                "The screening score is a heuristic, depends on the chosen weights, and is not a launch threshold.",
                "Adaptive modules are descriptive diagnostics with module-specific data requirements; they do not establish causality or guarantee forecasts.",
            ],
            "provenance": {
                "retrieved_at": generated_at,
                "api_sources": client.sources,
                "request_sha256": request_sha256,
            },
            "artifacts": {},
        }

        artifacts = write_artifacts(
            staging_dir, analysis, formats=set(request["formats"])
        )
        artifacts.update(
            {
                "analysis": "analysis.json",
                "manifest": "manifest.json",
                "request": "request.json",
                "resolution": "resolution.json",
            }
        )
        analysis["artifacts"] = artifacts
        write_json(staging_dir / "analysis.json", analysis)
        write_json(
            staging_dir / "manifest.json",
            {
                "schema_version": "1.1",
                "status": "complete",
                "tool_version": __version__,
                "generated_at": generated_at,
                "request_sha256": request_sha256,
                "files": artifacts,
                "api_sources": client.sources,
                "warnings": analysis["warnings"],
            },
        )
        _publish_bundle(staging_dir, output_dir)
        result = {
            "status": "complete",
            "output_dir": str(output_dir),
            "top_language": series[0]["language"] if series else None,
            "files": artifacts,
            "warnings": analysis["warnings"],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return result
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        request = normalize_request(parser.parse_args(argv))
        run(request)
        return 0
    except (ConfigError, ValueError, ApiError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
