from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


DEV_ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = DEV_ROOT / "evals"
sys.path.insert(0, str(EVAL_ROOT))

import runner  # noqa: E402


class EvalRunnerTests(unittest.TestCase):
    def test_default_case_catalog_is_valid_and_unique(self) -> None:
        cases = runner.load_cases()
        self.assertEqual(len(cases), 33)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        self.assertEqual(
            {case["layer"] for case in cases},
            {"engine", "planner", "agent", "live"},
        )

    def test_deterministic_layers_pass_release_thresholds(self) -> None:
        payload = runner.run_deterministic()
        self.assertEqual(payload["summary"]["failed"], 0)
        self.assertEqual(payload["summary"]["pass_rate"], 1.0)
        self.assertGreaterEqual(payload["summary"]["planner"]["exact_match_rate"], 0.9)
        self.assertGreaterEqual(payload["summary"]["planner"]["module_precision"], 0.9)
        self.assertGreaterEqual(payload["summary"]["planner"]["module_recall"], 0.9)

    def test_agent_preparation_hides_expectations_and_rubrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepared = Path(directory) / "prepared"
            manifest = runner.prepare_agent(prepared, case_ids={"agent-growth-summary"})
            case_dir = prepared / "cases" / "agent-growth-summary"
            self.assertEqual(manifest["case_count"], 1)
            self.assertTrue((prepared / "skill" / "SKILL.md").is_file())
            self.assertTrue((case_dir / "input-bundle" / "analysis.json").is_file())
            self.assertTrue((case_dir / "prompt.txt").is_file())
            self.assertTrue((case_dir / "agent-result.template.json").is_file())
            visible_names = {
                path.name for path in prepared.rglob("*") if path.is_file()
            }
            self.assertNotIn("agent-v1.json", visible_names)
            self.assertFalse(any("expected" in name for name in visible_names))
            self.assertFalse(
                any("__pycache__" in path.parts for path in prepared.rglob("*"))
            )

    def test_completed_agent_case_can_be_graded_programmatically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepared = Path(directory) / "prepared"
            runner.prepare_agent(prepared, case_ids={"agent-growth-summary"})
            case_dir = prepared / "cases" / "agent-growth-summary"
            (case_dir / "response.md").write_text(
                "Observed attention rose by about 34.5%. The uncertainty interval "
                "should be reported; this does not establish market demand.\n",
                encoding="utf-8",
            )
            runner.write_json(
                case_dir / "trace.json", {"events": [{"name": "read_analysis"}]}
            )
            template = json.loads(
                (case_dir / "agent-result.template.json").read_text(encoding="utf-8")
            )
            template["status"] = "completed"
            runner.write_json(case_dir / "agent-result.json", template)
            payload = runner.grade_agent(prepared, case_ids={"agent-growth-summary"})
            self.assertEqual(payload["summary"]["passed"], 1)
            self.assertEqual(payload["summary"]["hard_gate_pass_rate"], 1.0)
            self.assertGreaterEqual(payload["summary"]["weighted_score_mean"], 85)

    def test_external_holdout_can_supply_private_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            holdout = Path(directory) / "holdout"
            cases = holdout / "cases"
            fixtures = holdout / "fixtures"
            cases.mkdir(parents=True)
            fixtures.mkdir(parents=True)
            runner.write_json(
                fixtures / "private.json",
                {
                    "schema_version": "1.0",
                    "fixtures": {
                        "private-flat": {
                            "start": "2024-01",
                            "series": [
                                {
                                    "language": "en",
                                    "title": "Private holdout",
                                    "months": 12,
                                    "raw": {"type": "constant", "value": 7},
                                    "share": {"type": "constant", "value": 1},
                                }
                            ],
                        }
                    },
                },
            )
            runner.write_json(
                cases / "engine.json",
                {
                    "schema_version": "1.0",
                    "layer": "engine",
                    "cases": [
                        {
                            "id": "private-engine-flat",
                            "title": "Private fixture is discoverable",
                            "tags": ["holdout"],
                            "fixture": "private-flat",
                            "input": {
                                "operation": "analyze_metric",
                                "metric": "raw",
                                "bootstrap_samples": 100,
                                "seed": 1,
                            },
                            "expected": {
                                "assertions": [
                                    {
                                        "path": "annualized_trend_pct",
                                        "op": "approx",
                                        "value": 0,
                                        "tolerance": 0.000001,
                                    }
                                ]
                            },
                        }
                    ],
                },
            )
            payload = runner.run_deterministic(case_roots=[cases], layers={"engine"})
            self.assertEqual(payload["summary"]["passed"], 1)

    def test_release_is_incomplete_without_agent_evaluation(self) -> None:
        deterministic = runner.run_deterministic(
            case_ids={"engine-flat-signal", "planner-baseline-only"}
        )
        gates = runner.load_json(runner.RUBRIC_ROOT / "release-gates-v1.json")
        status = runner.check_release(
            deterministic,
            agent=None,
            live=None,
            baseline=None,
            gates=gates,
        )
        self.assertEqual(status["status"], "incomplete")
        self.assertTrue(status["incomplete_reasons"])


if __name__ == "__main__":
    unittest.main()
