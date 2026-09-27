from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[2] / "interest-signals"
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402


class CliContractTests(unittest.TestCase):
    def test_normalizes_minimal_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = analyze._parser().parse_args(
                [
                    "--topic",
                    "astronomy",
                    "--languages",
                    "uk,pl",
                    "--start",
                    "2024-01-01",
                    "--end",
                    "2025-12-31",
                    "--formats",
                    "csv,svg",
                    "--output-dir",
                    directory,
                ]
            )
            request = analyze.normalize_request(args)
        self.assertEqual(request["metric"], "share")
        self.assertEqual(request["languages"], ["uk", "pl"])
        self.assertEqual(request["formats"], ["csv", "svg"])
        self.assertIsNone(request["question"])
        self.assertEqual(request["analysis_modules"], ["auto"])

    def test_normalizes_question_and_explicit_analysis_modules(self) -> None:
        args = analyze._parser().parse_args(
            [
                "--topic",
                "astronomy",
                "--languages",
                "uk",
                "--question",
                "Was there a structural break?",
                "--analysis-modules",
                "changepoints,stability",
            ]
        )
        request = analyze.normalize_request(args)
        self.assertEqual(request["question"], "Was there a structural break?")
        self.assertEqual(request["analysis_modules"], ["changepoints", "stability"])

    def test_rejects_auto_combined_with_explicit_module(self) -> None:
        args = analyze._parser().parse_args(
            [
                "--topic",
                "astronomy",
                "--languages",
                "uk",
                "--analysis-modules",
                "auto,anomalies",
            ]
        )
        with self.assertRaisesRegex(analyze.ConfigError, "cannot be combined"):
            analyze.normalize_request(args)

    def test_rejects_unknown_analysis_module(self) -> None:
        args = analyze._parser().parse_args(
            [
                "--topic",
                "astronomy",
                "--languages",
                "uk",
                "--analysis-modules",
                "forecasting",
            ]
        )
        with self.assertRaisesRegex(analyze.ConfigError, "Unknown analysis module"):
            analyze.normalize_request(args)

    def test_rejects_unknown_weight(self) -> None:
        args = analyze._parser().parse_args(
            [
                "--topic",
                "x",
                "--languages",
                "en",
                "--weight",
                "magic=1",
            ]
        )
        with self.assertRaises(analyze.ConfigError):
            analyze.normalize_request(args)

    def test_rejects_unused_title_override(self) -> None:
        args = analyze._parser().parse_args(
            [
                "--topic",
                "astronomy",
                "--languages",
                "uk",
                "--title",
                "pl=Astronomia",
            ]
        )
        with self.assertRaisesRegex(analyze.ConfigError, "neither requested"):
            analyze.normalize_request(args)

    def test_rejects_future_end_date(self) -> None:
        args = analyze._parser().parse_args(
            [
                "--topic",
                "astronomy",
                "--languages",
                "uk",
                "--end",
                "2999-12-31",
            ]
        )
        with self.assertRaisesRegex(analyze.ConfigError, "future"):
            analyze.normalize_request(args)

    def test_rejects_non_finite_weight(self) -> None:
        args = analyze._parser().parse_args(
            ["--topic", "astronomy", "--languages", "uk", "--weight", "growth=nan"]
        )
        with self.assertRaisesRegex(analyze.ConfigError, "finite"):
            analyze.normalize_request(args)

    def test_rejects_string_boolean_in_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "request.json"
            config.write_text(
                json.dumps(
                    {
                        "topic": "astronomy",
                        "languages": ["uk"],
                        "allow_search_fallback": "false",
                    }
                ),
                encoding="utf-8",
            )
            args = analyze._parser().parse_args(["--config", str(config)])
            with self.assertRaisesRegex(analyze.ConfigError, "JSON boolean"):
                analyze.normalize_request(args)


class TransactionalBundleTests(unittest.TestCase):
    @staticmethod
    def _request(directory: str, *, resolve_only: bool = False) -> dict:
        args = analyze._parser().parse_args(
            [
                "--topic",
                "astronomy",
                "--languages",
                "uk",
                "--start",
                "2024-01-01",
                "--end",
                "2025-12-31",
                "--formats",
                "csv",
                "--output-dir",
                directory,
            ]
            + (["--resolve-only"] if resolve_only else [])
        )
        return analyze.normalize_request(args)

    def test_failed_rerun_preserves_previous_complete_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            old_manifest = '{"status":"complete","generation":"old"}\n'
            old_analysis = '{"generation":"old"}\n'
            (output / "manifest.json").write_text(old_manifest, encoding="utf-8")
            (output / "analysis.json").write_text(old_analysis, encoding="utf-8")
            request = self._request(directory)
            with patch.object(
                analyze, "resolve_topic", side_effect=RuntimeError("boom")
            ):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    analyze.run(request)
            self.assertEqual(
                (output / "manifest.json").read_text(encoding="utf-8"), old_manifest
            )
            self.assertEqual(
                (output / "analysis.json").read_text(encoding="utf-8"), old_analysis
            )
            self.assertFalse(any(output.glob(".run-*")))

    def test_resolve_only_commit_removes_stale_full_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name in ("analysis.json", "series.csv", "report.pdf"):
                (output / name).write_text("stale", encoding="utf-8")
            resolution = {
                "topic": "astronomy",
                "source_language": "en",
                "source_article": {"title": "Astronomy"},
                "articles": [],
                "warnings": [],
            }
            request = self._request(directory, resolve_only=True)
            with patch.object(analyze, "resolve_topic", return_value=resolution):
                result = analyze.run(request)
            self.assertEqual(result["status"], "resolved")
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "resolved")
            self.assertFalse((output / "analysis.json").exists())
            self.assertFalse((output / "series.csv").exists())
            self.assertFalse((output / "report.pdf").exists())

    def test_shared_months_exclude_gaps_and_pre_creation_periods(self) -> None:
        first = [
            {"month": "2025-01", "complete_month": True, "data_status": "observed"},
            {"month": "2025-02", "complete_month": True, "data_status": "observed"},
            {"month": "2025-03", "complete_month": True, "data_status": "observed"},
        ]
        second = [
            {"month": "2025-01", "complete_month": True, "data_status": "not_created"},
            {
                "month": "2025-02",
                "complete_month": True,
                "data_status": "project_data_gap",
            },
            {"month": "2025-03", "complete_month": True, "data_status": "observed"},
        ]
        self.assertEqual(analyze._shared_analysis_months([first, second]), {"2025-03"})

    def test_completed_bundle_persists_adaptive_plan_and_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = self._request(directory)
            request["question"] = "Were there unusual spikes?"
            request["analysis_modules"] = ["auto"]
            resolution = {
                "topic": "astronomy",
                "source_language": "en",
                "source_article": {"title": "Astronomy"},
                "articles": [
                    {
                        "language": "uk",
                        "title": "Астрономія",
                        "url": "https://uk.wikipedia.org/wiki/A",
                        "resolution_method": "interlanguage_link",
                        "resolution_confidence": "high",
                    }
                ],
                "warnings": [],
            }
            monthly = []
            for index in range(24):
                year = 2024 + index // 12
                month = index % 12 + 1
                value = 5000 if index == 23 else 100
                monthly.append(
                    {
                        "month": f"{year:04d}-{month:02d}",
                        "views": value,
                        "project_views": 1_000_000,
                        "share_per_million": value,
                        "days_in_range": 30,
                        "article_observed_days": 30,
                        "implicit_zero_days": 0,
                        "project_observed_days": 30,
                        "data_status": "observed",
                        "complete_month": True,
                    }
                )
            with (
                patch.object(analyze, "resolve_topic", return_value=resolution),
                patch.object(analyze, "fetch_article_daily", return_value=({}, set())),
                patch.object(analyze, "fetch_project_daily", return_value=({}, set())),
                patch.object(analyze, "aggregate_monthly", return_value=monthly),
            ):
                analyze.run(request)
            analysis = json.loads(
                (Path(directory) / "analysis.json").read_text(encoding="utf-8")
            )
            self.assertEqual(analysis["schema_version"], "1.1")
            self.assertEqual(
                analysis["analysis_plan"]["selected_modules"][0]["name"],
                "anomalies",
            )
            self.assertEqual(
                analysis["series"][0]["adaptive_analysis"]["anomalies"]["status"],
                "computed",
            )


if __name__ == "__main__":
    unittest.main()
