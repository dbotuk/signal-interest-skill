"""Pageview retrieval, monthly aggregation, trend estimates, and ranking."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
import hashlib
import math
import random
import statistics
from typing import Any, Iterable
from urllib.parse import quote

from .client import JsonClient


PAGEVIEW_START = date(2015, 7, 1)
PAGEVIEW_BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews"


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Invalid date {value!r}; expected YYYY-MM-DD") from error
    return parsed


def month_start(value: date) -> date:
    return value.replace(day=1)


def previous_month_start(value: date, months: int = 1) -> date:
    absolute = value.year * 12 + value.month - 1 - months
    return date(absolute // 12, absolute % 12 + 1, 1)


def default_date_range(today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    first_current = month_start(today)
    end = first_current - timedelta(days=1)
    start = previous_month_start(first_current, 24)
    return start, end


def validate_date_range(start: date, end: date) -> None:
    if start < PAGEVIEW_START:
        raise ValueError(
            f"Wikimedia Pageviews data begins on {PAGEVIEW_START.isoformat()}"
        )
    if end < start:
        raise ValueError("End date must not be earlier than start date")
    if (end - start).days > 3653:
        raise ValueError(
            "A single run is limited to 10 years; split longer research into "
            "comparable windows."
        )


def iter_days(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _chunks(start: date, end: date, max_days: int = 366) -> Iterable[tuple[date, date]]:
    current = start
    while current <= end:
        chunk_end = min(end, current + timedelta(days=max_days - 1))
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def _api_stamp(value: date) -> str:
    return value.strftime("%Y%m%d00")


def _parse_items(payload: dict[str, Any]) -> dict[date, int]:
    values: dict[date, int] = {}
    for item in payload.get("items", []) or []:
        timestamp = str(item.get("timestamp", ""))
        if len(timestamp) < 8:
            continue
        try:
            day = date(int(timestamp[:4]), int(timestamp[4:6]), int(timestamp[6:8]))
            views = int(item.get("views", 0))
        except (TypeError, ValueError):
            continue
        values[day] = max(0, views)
    return values


def fetch_article_daily(
    client: JsonClient,
    *,
    language: str,
    title: str,
    start: date,
    end: date,
) -> tuple[dict[date, int], set[date]]:
    project = f"{language}.wikipedia.org"
    encoded_title = quote(title.replace(" ", "_"), safe="")
    values: dict[date, int] = {}
    for chunk_start, chunk_end in _chunks(start, end):
        url = (
            f"{PAGEVIEW_BASE}/per-article/{quote(project, safe='')}/all-access/"
            f"user/{encoded_title}/daily/{_api_stamp(chunk_start)}/"
            f"{_api_stamp(chunk_end)}"
        )
        payload = client.get_json(url)
        values.update(_parse_items(payload))
    observed = set(values)
    return ({day: values.get(day, 0) for day in iter_days(start, end)}, observed)


def fetch_project_daily(
    client: JsonClient,
    *,
    language: str,
    start: date,
    end: date,
) -> tuple[dict[date, int], set[date]]:
    project = f"{language}.wikipedia.org"
    values: dict[date, int] = {}
    for chunk_start, chunk_end in _chunks(start, end):
        url = (
            f"{PAGEVIEW_BASE}/aggregate/{quote(project, safe='')}/all-access/"
            f"user/daily/{_api_stamp(chunk_start)}/{_api_stamp(chunk_end)}"
        )
        payload = client.get_json(url)
        values.update(_parse_items(payload))
    observed = set(values)
    return ({day: values.get(day, 0) for day in iter_days(start, end)}, observed)


def _month_keys(start: date, end: date) -> list[str]:
    keys: list[str] = []
    current = month_start(start)
    final = month_start(end)
    while current <= final:
        keys.append(current.strftime("%Y-%m"))
        current = previous_month_start(current, -1)
    return keys


def aggregate_monthly(
    article_daily: dict[date, int],
    project_daily: dict[date, int],
    *,
    start: date,
    end: date,
    article_observed: set[date] | None = None,
    project_observed: set[date] | None = None,
    created_at: date | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    article_observed = (
        article_observed if article_observed is not None else set(article_daily)
    )
    project_observed = (
        project_observed if project_observed is not None else set(project_daily)
    )
    today = today or date.today()
    rows: list[dict[str, Any]] = []
    for key in _month_keys(start, end):
        year, month = (int(part) for part in key.split("-"))
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        range_first = max(start, first)
        range_last = min(end, last)
        days = list(iter_days(range_first, range_last))
        article_views = sum(article_daily.get(day, 0) for day in days)
        project_views = sum(project_daily.get(day, 0) for day in days)
        observed_article_days = sum(day in article_observed for day in days)
        observed_project_days = sum(day in project_observed for day in days)
        if created_at and range_last < created_at:
            data_status = "not_created"
        elif created_at and range_first <= created_at <= range_last:
            # The page existed for only part of this month. Treating the days before
            # creation as zero attention can create an enormous, artificial trend.
            data_status = "creation_partial"
        elif observed_project_days < len(days):
            data_status = "project_data_gap"
        elif range_last >= today - timedelta(days=2):
            data_status = "provisional"
        elif observed_article_days < len(days):
            data_status = "implicit_zero_days"
        else:
            data_status = "observed"
        share = article_views / project_views * 1_000_000 if project_views > 0 else None
        rows.append(
            {
                "month": key,
                "views": article_views,
                "project_views": project_views,
                "share_per_million": share,
                "days_in_range": len(days),
                "article_observed_days": observed_article_days,
                "implicit_zero_days": len(days) - observed_article_days,
                "project_observed_days": observed_project_days,
                "data_status": data_status,
                "complete_month": range_first == first and range_last == last,
            }
        )
    return rows


def _linear_fit(
    values: list[float], positions: list[float] | None = None
) -> tuple[float, float, list[float], list[float]]:
    n = len(values)
    positions = positions if positions is not None else list(range(n))
    if len(positions) != n:
        raise ValueError("positions must have the same length as values")
    if n < 2:
        return 0.0, values[0] if values else 0.0, [0.0] * n, [0.0] * n
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
    return slope, intercept, fitted, residuals


def _annualized_percent(slope: float) -> float:
    exponent = max(-20.0, min(20.0, slope * 12))
    return math.expm1(exponent) * 100


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _bootstrap_trend_interval(
    log_values: list[float],
    *,
    positions: list[float],
    samples: int,
    seed: int,
) -> tuple[float | None, float | None]:
    # Fewer than 100 resamples is useful for debugging only and must not be
    # presented as a 95% uncertainty interval or rewarded as strong evidence.
    if len(log_values) < 6 or samples < 100:
        return None, None
    slope, _, fitted, residuals = _linear_fit(log_values, positions)
    if max(residuals) - min(residuals) < 1e-12:
        point = _annualized_percent(slope)
        return point, point
    rng = random.Random(seed)
    block_size = min(3, len(residuals))
    max_start = len(residuals) - block_size
    estimates: list[float] = []
    for _ in range(samples):
        sampled: list[float] = []
        while len(sampled) < len(residuals):
            block_start = rng.randint(0, max_start)
            sampled.extend(residuals[block_start : block_start + block_size])
        synthetic = [
            fit + error for fit, error in zip(fitted, sampled[: len(residuals)])
        ]
        estimates.append(_annualized_percent(_linear_fit(synthetic, positions)[0]))
    return _percentile(estimates, 0.025), _percentile(estimates, 0.975)


def _growth_percent(recent: float, earlier: float) -> float | None:
    if earlier <= 0:
        return None
    return (recent / earlier - 1) * 100


def _is_contiguous(positions: list[float]) -> bool:
    return all(
        math.isclose(current - previous, 1.0)
        for previous, current in zip(positions, positions[1:])
    )


def analyze_metric(
    values: list[float],
    *,
    positions: list[float] | None = None,
    bootstrap_samples: int,
    seed: int,
) -> dict[str, Any]:
    n = len(values)
    positions = (
        positions if positions is not None else [float(index) for index in range(n)]
    )
    if len(positions) != n:
        raise ValueError("positions must have the same length as values")
    if not values:
        return {
            "months": 0,
            "annualized_trend_pct": None,
            "trend_ci_95_pct": [None, None],
            "trailing_12m_growth_pct": None,
            "recent_3m_momentum_pct": None,
            "mean_monthly": None,
            "median_monthly": None,
            "peak_month_share": None,
            "trend_without_peak_pct": None,
            "event_sensitive": None,
        }

    log_values = [math.log1p(max(0.0, value)) for value in values]
    slope, _, _, _ = _linear_fit(log_values, positions)
    trend = _annualized_percent(slope) if n >= 6 else None
    if trend is None:
        interval: tuple[float | None, float | None] = (None, None)
    else:
        interval = _bootstrap_trend_interval(
            log_values,
            positions=positions,
            samples=bootstrap_samples,
            seed=seed,
        )

    trailing_growth = None
    if n >= 24 and _is_contiguous(positions[-24:]):
        trailing_growth = _growth_percent(sum(values[-12:]), sum(values[-24:-12]))
    momentum = None
    if n >= 6 and _is_contiguous(positions[-6:]):
        momentum = _growth_percent(
            statistics.fmean(values[-3:]), statistics.fmean(values[-6:-3])
        )

    total = sum(values)
    peak_index = max(range(n), key=values.__getitem__)
    peak_share = values[peak_index] / total if total > 0 else 0.0
    without_peak = [
        value for index, value in enumerate(log_values) if index != peak_index
    ]
    positions_without_peak = [
        position for index, position in enumerate(positions) if index != peak_index
    ]
    trend_without_peak = (
        _annualized_percent(_linear_fit(without_peak, positions_without_peak)[0])
        if len(without_peak) >= 6
        else trend
    )
    event_sensitive = False
    if trend is not None and trend_without_peak is not None:
        sign_flip = (
            trend != 0
            and trend_without_peak != 0
            and (trend > 0) != (trend_without_peak > 0)
        )
        material_change = abs(trend - trend_without_peak) > max(10.0, abs(trend) * 0.75)
        event_sensitive = sign_flip or material_change

    return {
        "months": n,
        "annualized_trend_pct": trend,
        "trend_ci_95_pct": list(interval),
        "trailing_12m_growth_pct": trailing_growth,
        "recent_3m_momentum_pct": momentum,
        "mean_monthly": statistics.fmean(values),
        "median_monthly": statistics.median(values),
        "peak_month_share": peak_share,
        "trend_without_peak_pct": trend_without_peak,
        "event_sensitive": event_sensitive,
    }


def _stable_seed(value: str, base_seed: int) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return base_seed ^ int.from_bytes(digest[:8], "big")


def _evidence_quality(
    *,
    selected_stats: dict[str, Any],
    raw_mean: float,
    resolution_confidence: str,
) -> tuple[float, str, dict[str, float]]:
    months = int(selected_stats.get("months") or 0)
    sample_component = min(1.0, months / 24)
    interval = selected_stats.get("trend_ci_95_pct") or [None, None]
    trend = selected_stats.get("annualized_trend_pct")
    if trend is None or interval[0] is None or interval[1] is None:
        signal_component = 0.0
    elif interval[0] > 0 or interval[1] < 0:
        signal_component = 1.0
    else:
        width = max(1.0, abs(interval[1] - interval[0]))
        signal_component = min(0.65, abs(trend) / width)
    traffic_component = min(1.0, math.log10(max(0.0, raw_mean) + 1) / 3)
    peak_share = float(selected_stats.get("peak_month_share") or 0.0)
    spike_component = max(0.0, min(1.0, 1 - max(0.0, peak_share - 0.20) / 0.50))
    resolution_component = {"high": 1.0, "medium": 0.8, "low": 0.55}.get(
        resolution_confidence, 0.55
    )
    components = {
        "sample_length": sample_component,
        "trend_signal": signal_component,
        "traffic_base": traffic_component,
        "spike_resistance": spike_component,
        "title_resolution": resolution_component,
    }
    score = 100 * (
        0.25 * sample_component
        + 0.30 * signal_component
        + 0.20 * traffic_component
        + 0.15 * spike_component
        + 0.10 * resolution_component
    )
    label = "high" if score >= 75 else "medium" if score >= 50 else "low"
    return score, label, components


def analyze_series(
    *,
    article: dict[str, Any],
    monthly: list[dict[str, Any]],
    selected_metric: str,
    bootstrap_samples: int,
    base_seed: int,
    analysis_months: set[str] | None = None,
) -> dict[str, Any]:
    excluded_statuses = {
        "not_created",
        "creation_partial",
        "project_data_gap",
        "provisional",
    }
    indexed_rows = list(enumerate(monthly))
    complete = [
        (index, row)
        for index, row in indexed_rows
        if row["complete_month"]
        and row["data_status"] not in excluded_statuses
        and (analysis_months is None or row["month"] in analysis_months)
    ]
    eligible = [
        (index, row)
        for index, row in indexed_rows
        if row["data_status"] not in excluded_statuses
        and (analysis_months is None or row["month"] in analysis_months)
    ]
    analysis_rows = complete if complete else eligible
    positions = [float(index) for index, _ in analysis_rows]
    rows_only = [row for _, row in analysis_rows]
    raw_values = [float(row["views"]) for row in rows_only]
    share_values = [float(row["share_per_million"] or 0.0) for row in rows_only]
    language = article["language"]
    raw_stats = analyze_metric(
        raw_values,
        positions=positions,
        bootstrap_samples=bootstrap_samples,
        seed=_stable_seed(f"{language}:raw", base_seed),
    )
    share_stats = analyze_metric(
        share_values,
        positions=positions,
        bootstrap_samples=bootstrap_samples,
        seed=_stable_seed(f"{language}:share", base_seed),
    )
    selected_stats = share_stats if selected_metric == "share" else raw_stats
    score, label, components = _evidence_quality(
        selected_stats=selected_stats,
        raw_mean=float(raw_stats["mean_monthly"] or 0.0),
        resolution_confidence=article.get("resolution_confidence", "low"),
    )
    warnings: list[str] = []
    if len(rows_only) < 12:
        warnings.append("fewer_than_12_complete_months")
    if positions and not _is_contiguous(positions):
        warnings.append("analysis_window_contains_gaps")
    if any(row["data_status"] == "creation_partial" for row in monthly):
        warnings.append("creation_month_excluded")
    if any(row["data_status"] == "project_data_gap" for row in monthly):
        warnings.append("project_data_gap_excluded")
    if bootstrap_samples < 100:
        warnings.append("bootstrap_interval_disabled")
    if float(raw_stats["mean_monthly"] or 0.0) < 100:
        warnings.append("low_traffic_base")
    if selected_stats.get("event_sensitive"):
        warnings.append("trend_sensitive_to_peak_month")
    if article.get("resolution_confidence") == "low":
        warnings.append("verify_low_confidence_title_mapping")
    raw_trend = raw_stats.get("annualized_trend_pct")
    share_trend = share_stats.get("annualized_trend_pct")
    if (
        raw_trend is not None
        and share_trend is not None
        and raw_trend != 0
        and share_trend != 0
        and (raw_trend > 0) != (share_trend > 0)
    ):
        warnings.append("raw_and_normalized_trends_disagree")

    return {
        "language": language,
        "title": article["title"],
        "url": article["url"],
        "resolution": {
            "method": article.get("resolution_method"),
            "confidence": article.get("resolution_confidence"),
            "redirected_from": article.get("redirected_from"),
            "wikidata_id": article.get("wikidata_id"),
            "semantic_match": article.get("semantic_match"),
            "candidates": article.get("candidate_titles", []),
        },
        "analysis_window": {
            "start": rows_only[0]["month"] if rows_only else None,
            "end": rows_only[-1]["month"] if rows_only else None,
            "months": len(rows_only),
            "included_months": [row["month"] for row in rows_only],
            "shared_across_series": analysis_months is not None,
        },
        "monthly": monthly,
        "metrics": {"raw": raw_stats, "share": share_stats},
        "selected_metric": selected_metric,
        "evidence_quality": {
            "score": score,
            "label": label,
            "components": components,
        },
        "warnings": warnings,
        "rank": None,
        "screening_score": None,
    }


def rank_series(
    series: list[dict[str, Any]], weights: dict[str, float]
) -> list[dict[str, Any]]:
    if len(series) <= 1:
        if series:
            series[0]["rank"] = 1
        return series
    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise ValueError("At least one ranking weight must be greater than zero")
    normalized_weights = {key: value / total_weight for key, value in weights.items()}
    for item in series:
        stats = item["metrics"][item["selected_metric"]]
        growth_value = stats.get("annualized_trend_pct")
        raw_mean = float(item["metrics"]["raw"].get("mean_monthly") or 0.0)
        components = {
            # Anchors prevent the least-bad declining candidate from scoring 100.
            # -25% annual trend maps to 0, flat maps to 0.5, and +25% maps to 1.
            "growth": (
                0.0
                if growth_value is None
                else max(0.0, min(1.0, (float(growth_value) + 25.0) / 50.0))
            ),
            # A five-order magnitude scale: 100 monthly views ~= 0.4,
            # 1,000 ~= 0.6, 10,000 ~= 0.8, and 100,000 ~= 1.0.
            "volume": max(0.0, min(1.0, math.log10(raw_mean + 1) / 5.0)),
            "reliability": float(item["evidence_quality"]["score"]) / 100,
        }
        item["screening_components"] = components
        item["screening_score"] = 100 * sum(
            normalized_weights[key] * components[key]
            for key in ("growth", "volume", "reliability")
        )
    series.sort(
        key=lambda item: (
            float(item["screening_score"] or 0.0),
            float(item["evidence_quality"]["score"]),
        ),
        reverse=True,
    )
    for rank, item in enumerate(series, 1):
        item["rank"] = rank
    return series


def build_findings(series: list[dict[str, Any]]) -> list[str]:
    if not series:
        return []
    findings: list[str] = []
    if len(series) == 1:
        item = series[0]
        trend = item["metrics"][item["selected_metric"]].get("annualized_trend_pct")
        if trend is None:
            findings.append(
                f"{item['language']} has too little eligible monthly data for a trend estimate."
            )
        else:
            direction = (
                "increasing" if trend > 0 else "decreasing" if trend < 0 else "flat"
            )
            findings.append(
                f"{item['language']} has an estimated annualized "
                f"{item['selected_metric']} trend of {trend:+.1f}%, indicating "
                f"{direction} Wikipedia attention over this window."
            )
    else:
        top = min(series, key=lambda item: int(item["rank"] or 9999))
        positive_count = sum(
            1
            for item in series
            if float(
                item["metrics"][item["selected_metric"]].get("annualized_trend_pct")
                or 0.0
            )
            > 0
        )
        context = (
            " No compared series has a positive primary trend."
            if positive_count == 0
            else ""
        )
        findings.append(
            f"{top['language']} ranks highest at {top['screening_score']:.0f}/100."
            f"{context} Treat it as a validation candidate, not a launch decision."
        )
        strongest = max(
            series,
            key=lambda item: float(
                item["metrics"][item["selected_metric"]].get("annualized_trend_pct")
                if item["metrics"][item["selected_metric"]].get("annualized_trend_pct")
                is not None
                else -1e12
            ),
        )
        trend = strongest["metrics"][strongest["selected_metric"]].get(
            "annualized_trend_pct"
        )
        if trend is not None and trend > 0:
            findings.append(
                f"{strongest['language']} has the strongest estimated annualized "
                f"{strongest['selected_metric']} trend at {trend:+.1f}%."
            )
        elif trend is not None and trend < 0:
            findings.append(
                f"All compared series are declining; {strongest['language']} has the "
                f"smallest estimated decline at {trend:+.1f}% annualized "
                f"{strongest['selected_metric']} trend."
            )
        elif trend is not None:
            findings.append(
                f"No compared series has a positive primary trend; the highest estimate "
                f"is {strongest['language']} at {trend:+.1f}% annualized "
                f"{strongest['selected_metric']} trend."
            )
    fragile = [
        item["language"]
        for item in series
        if item["evidence_quality"]["label"] == "low" or item["warnings"]
    ]
    if fragile:
        findings.append(
            "Interpret " + ", ".join(fragile) + " cautiously and review its warnings."
        )
    return findings
