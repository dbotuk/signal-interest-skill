from __future__ import annotations

from datetime import date, timedelta
import math
from pathlib import Path
import sys
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[2] / "interest-signals"
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from interest_signals.metrics import (  # noqa: E402
    aggregate_monthly,
    analyze_metric,
    analyze_series,
    build_findings,
    default_date_range,
    rank_series,
)


class DateAndAggregationTests(unittest.TestCase):
    def test_default_range_is_24_complete_months(self) -> None:
        start, end = default_date_range(date(2026, 9, 27))
        self.assertEqual(start.isoformat(), "2024-09-01")
        self.assertEqual(end.isoformat(), "2026-08-31")

    def test_monthly_status_distinguishes_implicit_zero(self) -> None:
        start = date(2024, 1, 1)
        end = date(2024, 1, 31)
        days = [start + timedelta(days=index) for index in range(31)]
        article = {day: 10 for day in days}
        project = {day: 1000 for day in days}
        observed = set(days[:-2])
        rows = aggregate_monthly(
            article,
            project,
            start=start,
            end=end,
            article_observed=observed,
            project_observed=set(days),
            today=date(2026, 1, 1),
        )
        self.assertEqual(rows[0]["data_status"], "implicit_zero_days")
        self.assertEqual(rows[0]["implicit_zero_days"], 2)
        self.assertAlmostEqual(rows[0]["share_per_million"], 10_000)

    def test_project_gap_is_not_labeled_as_article_zero(self) -> None:
        start = date(2024, 1, 1)
        end = date(2024, 1, 31)
        days = [start + timedelta(days=index) for index in range(31)]
        rows = aggregate_monthly(
            {day: 10 for day in days},
            {day: 1000 for day in days},
            start=start,
            end=end,
            article_observed=set(days),
            project_observed=set(days[:-1]),
            today=date(2026, 1, 1),
        )
        self.assertEqual(rows[0]["data_status"], "project_data_gap")

    def test_creation_month_is_excluded_as_partial(self) -> None:
        start = date(2024, 1, 1)
        end = date(2024, 1, 31)
        days = [start + timedelta(days=index) for index in range(31)]
        rows = aggregate_monthly(
            {day: 10 for day in days[24:]},
            {day: 1000 for day in days},
            start=start,
            end=end,
            article_observed=set(days[24:]),
            project_observed=set(days),
            created_at=date(2024, 1, 25),
            today=date(2026, 1, 1),
        )
        self.assertEqual(rows[0]["data_status"], "creation_partial")


class TrendAndRankingTests(unittest.TestCase):
    def test_trend_detects_sustained_growth(self) -> None:
        values = [100 * (1.03**month) for month in range(30)]
        stats = analyze_metric(values, bootstrap_samples=200, seed=9)
        self.assertGreater(stats["annualized_trend_pct"], 35)
        self.assertGreater(stats["trend_ci_95_pct"][0], 30)
        self.assertFalse(stats["event_sensitive"])

    def test_peak_sensitivity_is_flagged(self) -> None:
        values = [100.0] * 23 + [5000.0]
        stats = analyze_metric(values, bootstrap_samples=200, seed=7)
        self.assertTrue(stats["event_sensitive"])
        self.assertGreater(stats["peak_month_share"], 0.5)

    def test_debug_bootstrap_does_not_claim_95_percent_interval(self) -> None:
        values = [100 * (1.02**month) for month in range(24)]
        for samples in (0, 1, 99):
            with self.subTest(samples=samples):
                stats = analyze_metric(values, bootstrap_samples=samples, seed=4)
                self.assertEqual(stats["trend_ci_95_pct"], [None, None])
        production = analyze_metric(values, bootstrap_samples=100, seed=4)
        self.assertIsNotNone(production["trend_ci_95_pct"][0])

    def test_trend_preserves_calendar_spacing_across_gap(self) -> None:
        positions = [0.0, 1.0, 5.0, 6.0, 7.0, 8.0]
        values = [math.expm1(4.0 + 0.02 * position) for position in positions]
        stats = analyze_metric(
            values,
            positions=positions,
            bootstrap_samples=100,
            seed=3,
        )
        expected = math.expm1(0.02 * 12) * 100
        self.assertAlmostEqual(stats["annualized_trend_pct"], expected, places=8)
        self.assertIsNone(stats["recent_3m_momentum_pct"])

    def test_peak_removal_keeps_original_month_positions(self) -> None:
        positions = [float(index) for index in range(12)]
        values = [math.expm1(5.0 - 0.01 * position) for position in positions]
        values[5] = 20_000.0
        stats = analyze_metric(
            values,
            positions=positions,
            bootstrap_samples=100,
            seed=7,
        )
        self.assertLess(stats["trend_without_peak_pct"], 0)

    def test_ranking_uses_requested_weights(self) -> None:
        base_months = []
        for month in range(24):
            year = 2024 + month // 12
            number = month % 12 + 1
            base_months.append(
                {
                    "month": f"{year:04d}-{number:02d}",
                    "views": 100 + month * 10,
                    "project_views": 1_000_000,
                    "share_per_million": 100 + month * 10,
                    "days_in_range": 30,
                    "article_observed_days": 30,
                    "implicit_zero_days": 0,
                    "project_observed_days": 30,
                    "data_status": "observed",
                    "complete_month": True,
                }
            )
        first = analyze_series(
            article={
                "language": "aa",
                "title": "A",
                "url": "https://aa.wikipedia.org/wiki/A",
                "resolution_method": "explicit_title",
                "resolution_confidence": "high",
            },
            monthly=base_months,
            selected_metric="share",
            bootstrap_samples=50,
            base_seed=1,
        )
        flat_months = [
            {**row, "views": 10_000, "share_per_million": 10_000} for row in base_months
        ]
        second = analyze_series(
            article={
                "language": "bb",
                "title": "B",
                "url": "https://bb.wikipedia.org/wiki/B",
                "resolution_method": "explicit_title",
                "resolution_confidence": "high",
            },
            monthly=flat_months,
            selected_metric="share",
            bootstrap_samples=50,
            base_seed=1,
        )
        ranked = rank_series(
            [first, second], {"growth": 1.0, "volume": 0.0, "reliability": 0.0}
        )
        self.assertEqual(ranked[0]["language"], "aa")
        self.assertEqual(ranked[0]["rank"], 1)

    def test_pre_creation_months_are_excluded_from_trend(self) -> None:
        monthly = []
        for month in range(12):
            monthly.append(
                {
                    "month": f"2025-{month + 1:02d}",
                    "views": 0 if month < 6 else 100,
                    "project_views": 1_000_000,
                    "share_per_million": 0 if month < 6 else 100,
                    "days_in_range": 30,
                    "article_observed_days": 0 if month < 6 else 30,
                    "implicit_zero_days": 30 if month < 6 else 0,
                    "project_observed_days": 30,
                    "data_status": "not_created" if month < 6 else "observed",
                    "complete_month": True,
                }
            )
        result = analyze_series(
            article={
                "language": "aa",
                "title": "A",
                "url": "https://aa.wikipedia.org/wiki/A",
                "resolution_method": "explicit_title",
                "resolution_confidence": "high",
            },
            monthly=monthly,
            selected_metric="raw",
            bootstrap_samples=30,
            base_seed=1,
        )
        self.assertEqual(result["metrics"]["raw"]["months"], 6)
        self.assertAlmostEqual(result["metrics"]["raw"]["annualized_trend_pct"], 0)

    def test_series_excludes_creation_partial_and_preserves_effective_window(
        self,
    ) -> None:
        monthly = []
        for month in range(8):
            status = "creation_partial" if month == 0 else "observed"
            monthly.append(
                {
                    "month": f"2025-{month + 1:02d}",
                    "views": 10 if month == 0 else 100,
                    "project_views": 1_000_000,
                    "share_per_million": 10 if month == 0 else 100,
                    "days_in_range": 30,
                    "article_observed_days": 1 if month == 0 else 30,
                    "implicit_zero_days": 29 if month == 0 else 0,
                    "project_observed_days": 30,
                    "data_status": status,
                    "complete_month": True,
                }
            )
        result = analyze_series(
            article={
                "language": "aa",
                "title": "A",
                "url": "https://aa.wikipedia.org/wiki/A",
                "resolution_method": "explicit_title",
                "resolution_confidence": "high",
            },
            monthly=monthly,
            selected_metric="raw",
            bootstrap_samples=100,
            base_seed=1,
        )
        self.assertEqual(result["analysis_window"]["start"], "2025-02")
        self.assertEqual(result["analysis_window"]["months"], 7)
        self.assertAlmostEqual(result["metrics"]["raw"]["annualized_trend_pct"], 0)
        self.assertIn("creation_month_excluded", result["warnings"])

    def test_single_series_finding_is_not_comparative(self) -> None:
        series = [
            {
                "language": "uk",
                "selected_metric": "share",
                "metrics": {"share": {"annualized_trend_pct": -12.5}},
                "evidence_quality": {"label": "high"},
                "warnings": [],
            }
        ]
        findings = build_findings(series)
        self.assertIn("decreasing", findings[0])
        self.assertNotIn("least negative", findings[0])


if __name__ == "__main__":
    unittest.main()
