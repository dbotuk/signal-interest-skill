"""Question-aware selection and execution of optional analysis modules."""

from __future__ import annotations

from collections import defaultdict
import math
import statistics
from typing import Any, Callable


CORE_ANALYSES = (
    "trend",
    "trend_uncertainty",
    "momentum",
    "peak_sensitivity",
    "evidence_quality",
)
OPTIONAL_MODULES = (
    "seasonality",
    "changepoints",
    "anomalies",
    "stability",
)
EXCLUDED_STATUSES = {
    "not_created",
    "creation_partial",
    "project_data_gap",
    "provisional",
}
INTENT_TERMS = {
    "seasonality": (
        "season",
        "cycle",
        "cyclic",
        "recurring",
        "month of year",
        "сезон",
        "цикл",
        "повторюван",
    ),
    "changepoints": (
        "changepoint",
        "change point",
        "structural break",
        "regime",
        "inflection",
        "sudden change",
        "before and after",
        "злам",
        "перелом",
        "різк",
        "до і після",
        "зміна режим",
    ),
    "anomalies": (
        "anomal",
        "outlier",
        "spike",
        "unusual",
        "event",
        "аномал",
        "викид",
        "сплеск",
        "пік",
        "поді",
    ),
    "stability": (
        "stability",
        "stable",
        "volatil",
        "consistent",
        "reliable",
        "стабіль",
        "волатиль",
        "коливан",
        "надійн",
    ),
}


def _linear_fit(
    values: list[float], positions: list[float]
) -> tuple[float, list[float], list[float]]:
    if len(values) != len(positions):
        raise ValueError("positions must have the same length as values")
    if len(values) < 2:
        fitted = values.copy()
        return 0.0, fitted, [0.0 for _ in values]
    mean_x = statistics.fmean(positions)
    mean_y = statistics.fmean(values)
    denominator = sum((position - mean_x) ** 2 for position in positions)
    slope = (
        sum(
            (position - mean_x) * (value - mean_y)
            for position, value in zip(positions, values)
        )
        / denominator
        if denominator
        else 0.0
    )
    intercept = mean_y - slope * mean_x
    fitted = [intercept + slope * position for position in positions]
    residuals = [value - fit for value, fit in zip(values, fitted)]
    return slope, fitted, residuals


def _annualized_percent(slope: float) -> float:
    return math.expm1(max(-20.0, min(20.0, slope * 12))) * 100


def _month_position(value: str) -> int:
    year, month = (int(part) for part in value.split("-"))
    return year * 12 + month - 1


def _series_values(
    item: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[float], list[float]]:
    included = set(item.get("analysis_window", {}).get("included_months") or [])
    rows = [
        row
        for row in item.get("monthly", [])
        if row.get("complete_month")
        and row.get("data_status") not in EXCLUDED_STATUSES
        and (not included or row.get("month") in included)
    ]
    rows.sort(key=lambda row: str(row["month"]))
    field = "share_per_million" if item.get("selected_metric") == "share" else "views"
    values = [float(row.get(field) or 0.0) for row in rows]
    if not rows:
        return rows, values, []
    first = _month_position(str(rows[0]["month"]))
    positions = [float(_month_position(str(row["month"])) - first) for row in rows]
    return rows, values, positions


def _is_contiguous(positions: list[float]) -> bool:
    return all(
        math.isclose(current - previous, 1.0)
        for previous, current in zip(positions, positions[1:])
    )


def _skipped(module: str, reason: str) -> dict[str, Any]:
    return {"module": module, "status": "skipped", "reason": reason}


def _seasonality(item: dict[str, Any]) -> dict[str, Any]:
    rows, values, positions = _series_values(item)
    if len(values) < 24:
        return _skipped("seasonality", "requires at least 24 eligible months")
    if not _is_contiguous(positions):
        return _skipped("seasonality", "requires a contiguous monthly window")

    log_values = [math.log1p(max(0.0, value)) for value in values]
    _, _, residuals = _linear_fit(log_values, positions)
    by_month: dict[int, list[float]] = defaultdict(list)
    for row, residual in zip(rows, residuals):
        by_month[int(str(row["month"])[5:7])].append(residual)
    if len(by_month) != 12 or min(len(group) for group in by_month.values()) < 2:
        return _skipped(
            "seasonality", "requires at least two observations for every calendar month"
        )

    effects = {month: statistics.fmean(group) for month, group in by_month.items()}
    strongest = max(effects, key=effects.__getitem__)
    weakest = min(effects, key=effects.__getitem__)
    residual_mean = statistics.fmean(residuals)
    total_variation = sum((value - residual_mean) ** 2 for value in residuals)
    remaining_variation = sum(
        (residual - effects[int(str(row["month"])[5:7])]) ** 2
        for row, residual in zip(rows, residuals)
    )
    strength = (
        max(0.0, min(1.0, 1 - remaining_variation / total_variation))
        if total_variation > 1e-12
        else 0.0
    )
    amplitude = math.expm1(max(0.0, effects[strongest] - effects[weakest])) * 100
    return {
        "module": "seasonality",
        "status": "computed",
        "metric": item.get("selected_metric"),
        "months": len(values),
        "cycles": min(len(group) for group in by_month.values()),
        "strongest_calendar_month": f"{strongest:02d}",
        "weakest_calendar_month": f"{weakest:02d}",
        "seasonal_amplitude_pct": amplitude,
        "seasonal_strength": strength,
        "method": "month-of-year means of detrended log1p values",
        "caveat": "exploratory seasonality estimate; it does not identify a cause",
    }


def _changepoints(item: dict[str, Any]) -> dict[str, Any]:
    rows, values, positions = _series_values(item)
    if len(values) < 12:
        return _skipped("changepoints", "requires at least 12 eligible months")
    if not _is_contiguous(positions):
        return _skipped("changepoints", "requires a contiguous monthly window")

    log_values = [math.log1p(max(0.0, value)) for value in values]
    _, _, base_residuals = _linear_fit(log_values, positions)
    base_sse = sum(value**2 for value in base_residuals)
    candidates: list[tuple[float, int, float, float]] = []
    for split in range(6, len(values) - 5):
        pre_slope, _, pre_residuals = _linear_fit(log_values[:split], positions[:split])
        post_slope, _, post_residuals = _linear_fit(
            log_values[split:], positions[split:]
        )
        sse = sum(value**2 for value in pre_residuals + post_residuals)
        candidates.append((sse, split, pre_slope, post_slope))
    best_sse, split, pre_slope, post_slope = min(candidates)
    improvement = (
        max(0.0, min(1.0, 1 - best_sse / base_sse)) if base_sse > 1e-12 else 0.0
    )
    before = statistics.fmean(values[max(0, split - 3) : split])
    after = statistics.fmean(values[split : min(len(values), split + 3)])
    level_change = (after / before - 1) * 100 if before > 0 else None
    pre_trend = _annualized_percent(pre_slope)
    post_trend = _annualized_percent(post_slope)
    material = improvement >= 0.25 and (
        (level_change is not None and abs(level_change) >= 15)
        or abs(post_trend - pre_trend) >= 20
    )
    return {
        "module": "changepoints",
        "status": "computed",
        "metric": item.get("selected_metric"),
        "months": len(values),
        "candidate_month": str(rows[split]["month"]),
        "segmented_fit_improvement": improvement,
        "local_level_change_pct": level_change,
        "pre_annualized_trend_pct": pre_trend,
        "post_annualized_trend_pct": post_trend,
        "candidate_detected": material,
        "method": "best two-segment log-linear fit with at least six months per segment",
        "caveat": "descriptive scan without multiple-testing correction or causal attribution",
    }


def _anomalies(item: dict[str, Any]) -> dict[str, Any]:
    rows, values, positions = _series_values(item)
    if len(values) < 12:
        return _skipped("anomalies", "requires at least 12 eligible months")

    log_values = [math.log1p(max(0.0, value)) for value in values]
    _, _, residuals = _linear_fit(log_values, positions)
    center = statistics.median(residuals)
    deviations = [abs(value - center) for value in residuals]
    mad = statistics.median(deviations)
    scale = 1.4826 * mad
    scale_method = "median absolute deviation"
    if scale < 1e-12:
        scale = statistics.pstdev(residuals)
        scale_method = "residual standard deviation fallback"
    scored = []
    if scale >= 1e-12:
        for row, value, residual in zip(rows, values, residuals):
            score = (residual - center) / scale
            if abs(score) >= 3.5:
                scored.append(
                    {
                        "month": str(row["month"]),
                        "value": value,
                        "direction": "high" if score > 0 else "low",
                        "robust_z": score,
                    }
                )
    scored.sort(key=lambda result: abs(float(result["robust_z"])), reverse=True)
    return {
        "module": "anomalies",
        "status": "computed",
        "metric": item.get("selected_metric"),
        "months": len(values),
        "threshold_abs_z": 3.5,
        "candidates": scored[:5],
        "method": f"detrended log1p residuals scaled by {scale_method}",
        "caveat": "candidate anomalies require event, data-quality, and title-history review",
    }


def _stability(item: dict[str, Any]) -> dict[str, Any]:
    _, values, positions = _series_values(item)
    if len(values) < 6:
        return _skipped("stability", "requires at least six eligible months")

    log_values = [math.log1p(max(0.0, value)) for value in values]
    _, _, residuals = _linear_fit(log_values, positions)
    residual_volatility = math.expm1(statistics.pstdev(residuals)) * 100
    monthly_changes = [
        abs(current - previous)
        for previous, current, previous_position, current_position in zip(
            log_values,
            log_values[1:],
            positions,
            positions[1:],
        )
        if math.isclose(current_position - previous_position, 1.0)
    ]
    median_change = (
        math.expm1(statistics.median(monthly_changes)) * 100 if monthly_changes else 0.0
    )
    if residual_volatility <= 10 and median_change <= 15:
        label = "stable"
    elif residual_volatility <= 25 and median_change <= 35:
        label = "variable"
    else:
        label = "volatile"
    return {
        "module": "stability",
        "status": "computed",
        "metric": item.get("selected_metric"),
        "months": len(values),
        "residual_volatility_pct": residual_volatility,
        "median_abs_monthly_change_pct": median_change,
        "label": label,
        "method": "dispersion around a log-linear trend plus month-to-month log changes",
        "caveat": "stability describes this historical window and is not a forecast",
    }


MODULE_RUNNERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "seasonality": _seasonality,
    "changepoints": _changepoints,
    "anomalies": _anomalies,
    "stability": _stability,
}


def _intent_modules(question: str | None) -> list[tuple[str, str]]:
    if not question:
        return []
    normalized = question.casefold()
    matches: list[tuple[str, str]] = []
    for module in OPTIONAL_MODULES:
        matched = next(
            (term for term in INTENT_TERMS[module] if term in normalized), None
        )
        if matched:
            matches.append(
                (module, f"research question matched intent term {matched!r}")
            )
    return matches


def _adaptive_findings(
    series: list[dict[str, Any]], selected_modules: list[str], limit: int = 2
) -> list[str]:
    findings: list[str] = []
    computed = {
        module: [
            (item, item.get("adaptive_analysis", {}).get(module, {}))
            for item in series
            if item.get("adaptive_analysis", {}).get(module, {}).get("status")
            == "computed"
        ]
        for module in selected_modules
    }
    for module in selected_modules:
        values = computed[module]
        if not values:
            continue
        if module == "seasonality":
            item, result = max(
                values, key=lambda pair: float(pair[1]["seasonal_strength"])
            )
            findings.append(
                f"Exploratory seasonality scan for {item['language']} finds month "
                f"{result['strongest_calendar_month']} strongest and "
                f"{result['weakest_calendar_month']} weakest after detrending "
                f"(strength {float(result['seasonal_strength']):.0%})."
            )
        elif module == "changepoints":
            detected = [pair for pair in values if pair[1]["candidate_detected"]]
            item, result = max(
                detected or values,
                key=lambda pair: float(pair[1]["segmented_fit_improvement"]),
            )
            if result["candidate_detected"]:
                findings.append(
                    f"A descriptive changepoint scan flags {item['language']} around "
                    f"{result['candidate_month']} (segmented fit improvement "
                    f"{float(result['segmented_fit_improvement']):.0%}); this is not causal evidence."
                )
            else:
                findings.append(
                    "The changepoint scan found no material break under its descriptive threshold."
                )
        elif module == "anomalies":
            flagged = [pair for pair in values if pair[1]["candidates"]]
            if flagged:
                item, result = max(
                    flagged,
                    key=lambda pair: abs(float(pair[1]["candidates"][0]["robust_z"])),
                )
                candidate = result["candidates"][0]
                findings.append(
                    f"Anomaly scan flags {candidate['month']} in {item['language']} as "
                    f"unusually {candidate['direction']} (robust z {float(candidate['robust_z']):+.1f}); "
                    "review events and title history."
                )
            else:
                findings.append(
                    "The anomaly scan found no eligible month beyond its robust threshold."
                )
        elif module == "stability":
            item, result = min(
                values, key=lambda pair: float(pair[1]["residual_volatility_pct"])
            )
            findings.append(
                f"Stability scan classifies {item['language']} as {result['label']} "
                f"with {float(result['residual_volatility_pct']):.1f}% residual volatility."
            )
        if len(findings) >= limit:
            break
    return findings


def apply_analysis_plan(
    series: list[dict[str, Any]],
    *,
    question: str | None,
    requested_modules: list[str],
) -> tuple[dict[str, Any], list[str]]:
    """Select optional modules, attach per-series results, and return findings."""

    adaptive = requested_modules == ["auto"]
    candidate_reasons: dict[str, str] = {}
    if adaptive:
        candidate_reasons.update(_intent_modules(question))
        if any(
            item.get("metrics", {})
            .get(item.get("selected_metric"), {})
            .get("event_sensitive")
            for item in series
        ):
            candidate_reasons.setdefault(
                "anomalies", "core peak-sensitivity diagnostic flagged a fragile trend"
            )
    else:
        candidate_reasons = {
            module: "explicitly requested" for module in requested_modules
        }

    for item in series:
        item["adaptive_analysis"] = {}

    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for module in OPTIONAL_MODULES:
        if module not in candidate_reasons:
            continue
        runner = MODULE_RUNNERS[module]
        results = []
        for item in series:
            result = runner(item)
            item["adaptive_analysis"][module] = result
            results.append(result)
        computed_count = sum(result["status"] == "computed" for result in results)
        if computed_count:
            selected.append(
                {
                    "name": module,
                    "reason": candidate_reasons[module],
                    "series_computed": computed_count,
                    "series_total": len(series),
                }
            )
        else:
            skipped.append(
                {
                    "name": module,
                    "reason": "; ".join(
                        sorted({str(result["reason"]) for result in results})
                    ),
                }
            )

    selected_names = [item["name"] for item in selected]
    plan = {
        "mode": (
            "baseline_only"
            if not candidate_reasons
            else "adaptive"
            if adaptive
            else "explicit"
        ),
        "question": question,
        "requested_modules": requested_modules,
        "core_analyses": list(CORE_ANALYSES),
        "selected_modules": selected,
        "skipped_modules": skipped,
        "not_selected_modules": [
            module for module in OPTIONAL_MODULES if module not in candidate_reasons
        ],
        "selection_policy": (
            "Optional modules are selected from the research question and core "
            "diagnostics, then gated by minimum data requirements."
        ),
    }
    return plan, _adaptive_findings(series, selected_names)
