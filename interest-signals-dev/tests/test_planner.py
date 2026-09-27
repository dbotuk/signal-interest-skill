from __future__ import annotations

from pathlib import Path
import sys
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[2] / "interest-signals"
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from interest_signals.planner import apply_analysis_plan  # noqa: E402


def make_series(values: list[float], *, event_sensitive: bool = False) -> dict:
    monthly = []
    included = []
    for index, value in enumerate(values):
        absolute = 2022 * 12 + index
        month = f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"
        included.append(month)
        monthly.append(
            {
                "month": month,
                "views": value,
                "share_per_million": value,
                "data_status": "observed",
                "complete_month": True,
            }
        )
    return {
        "language": "en",
        "rank": 1,
        "selected_metric": "raw",
        "metrics": {"raw": {"event_sensitive": event_sensitive}},
        "analysis_window": {
            "start": included[0] if included else None,
            "end": included[-1] if included else None,
            "months": len(included),
            "included_months": included,
        },
        "monthly": monthly,
    }


class AnalysisPlannerTests(unittest.TestCase):
    def test_no_specialized_intent_keeps_reproducible_baseline(self) -> None:
        series = [make_series([100.0] * 24)]
        plan, findings = apply_analysis_plan(
            series, question=None, requested_modules=["auto"]
        )
        self.assertEqual(plan["mode"], "baseline_only")
        self.assertEqual(plan["selected_modules"], [])
        self.assertEqual(findings, [])

    def test_ukrainian_question_routes_to_relevant_modules(self) -> None:
        values = [180.0 if index % 12 == 8 else 100.0 + index for index in range(36)]
        series = [make_series(values)]
        plan, _ = apply_analysis_plan(
            series,
            question="Чи є сезонність або аномальні сплески?",
            requested_modules=["auto"],
        )
        selected = {item["name"] for item in plan["selected_modules"]}
        self.assertEqual(selected, {"seasonality", "anomalies"})
        self.assertEqual(
            series[0]["adaptive_analysis"]["seasonality"]["status"], "computed"
        )
        seasonality = series[0]["adaptive_analysis"]["seasonality"]
        self.assertEqual(seasonality["strongest_calendar_month"], "09")
        self.assertGreater(seasonality["seasonal_strength"], 0.5)

    def test_core_peak_diagnostic_triggers_anomaly_scan(self) -> None:
        series = [make_series([100.0] * 23 + [5000.0], event_sensitive=True)]
        plan, findings = apply_analysis_plan(
            series, question=None, requested_modules=["auto"]
        )
        self.assertEqual(plan["selected_modules"][0]["name"], "anomalies")
        candidates = series[0]["adaptive_analysis"]["anomalies"]["candidates"]
        self.assertEqual(candidates[0]["month"], "2023-12")
        self.assertTrue(any("2023-12" in finding for finding in findings))

    def test_explicit_module_records_data_requirement_failure(self) -> None:
        series = [make_series([100.0] * 8)]
        plan, findings = apply_analysis_plan(
            series, question=None, requested_modules=["seasonality"]
        )
        self.assertEqual(plan["mode"], "explicit")
        self.assertEqual(plan["selected_modules"], [])
        self.assertEqual(plan["skipped_modules"][0]["name"], "seasonality")
        self.assertIn("24 eligible months", plan["skipped_modules"][0]["reason"])
        self.assertEqual(findings, [])

    def test_changepoint_scan_detects_material_level_shift(self) -> None:
        series = [make_series([100.0] * 12 + [500.0] * 12)]
        plan, findings = apply_analysis_plan(
            series,
            question="Was there a sudden change in the signal?",
            requested_modules=["auto"],
        )
        self.assertEqual(plan["selected_modules"][0]["name"], "changepoints")
        result = series[0]["adaptive_analysis"]["changepoints"]
        self.assertTrue(result["candidate_detected"])
        self.assertEqual(result["candidate_month"], "2023-01")
        self.assertGreater(result["segmented_fit_improvement"], 0.9)
        self.assertTrue(any("2023-01" in finding for finding in findings))

    def test_smooth_trend_does_not_create_anomaly_or_changepoint_claim(self) -> None:
        values = [100.0 * (1.02**index) for index in range(24)]
        series = [make_series(values)]
        plan, findings = apply_analysis_plan(
            series,
            question="Are there anomalies or a sudden change?",
            requested_modules=["auto"],
        )
        selected = {item["name"] for item in plan["selected_modules"]}
        self.assertEqual(selected, {"anomalies", "changepoints"})
        self.assertEqual(series[0]["adaptive_analysis"]["anomalies"]["candidates"], [])
        self.assertFalse(
            series[0]["adaptive_analysis"]["changepoints"]["candidate_detected"]
        )
        self.assertTrue(any("no eligible month" in finding for finding in findings))
        self.assertTrue(any("no material break" in finding for finding in findings))

    def test_stability_module_is_question_driven(self) -> None:
        series = [make_series([100.0] * 18)]
        plan, findings = apply_analysis_plan(
            series,
            question="Наскільки стабільний цей сигнал?",
            requested_modules=["auto"],
        )
        self.assertEqual(plan["selected_modules"][0]["name"], "stability")
        result = series[0]["adaptive_analysis"]["stability"]
        self.assertEqual(result["label"], "stable")
        self.assertTrue(any("stable" in finding for finding in findings))


if __name__ == "__main__":
    unittest.main()
