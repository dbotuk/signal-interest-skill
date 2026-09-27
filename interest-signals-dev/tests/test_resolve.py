from __future__ import annotations

from pathlib import Path
import sys
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[2] / "interest-signals"
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from interest_signals.client import ApiError  # noqa: E402
from interest_signals.resolve import resolve_topic  # noqa: E402


class FakeClient:
    def get_json(self, base_url, params=None):
        params = params or {}
        language = base_url.split("//", 1)[1].split(".", 1)[0]
        if params.get("list") == "search":
            return {"query": {"search": [{"title": "Seed article"}]}}
        if params.get("prop") == "langlinks":
            return {
                "query": {
                    "pages": [
                        {
                            "title": "Seed article",
                            "langlinks": [{"lang": "pl", "title": "Artykuł"}],
                        }
                    ]
                }
            }
        if "pageprops" in str(params.get("prop")):
            title = params["titles"]
            canonical = "Seed article" if language == "en" else title
            return {
                "query": {
                    "pages": [
                        {
                            "pageid": 42,
                            "title": canonical,
                            "pageprops": {"wikibase_item": "Q1"},
                            "revisions": [{"timestamp": "2020-01-01T00:00:00Z"}],
                        }
                    ]
                }
            }
        raise AssertionError((base_url, params))


class ResolutionTests(unittest.TestCase):
    def test_uses_interlanguage_link(self) -> None:
        result = resolve_topic(
            FakeClient(),
            topic="seed",
            source_language="en",
            languages=["en", "pl"],
        )
        self.assertEqual(result["articles"][1]["title"], "Artykuł")
        self.assertEqual(
            result["articles"][1]["resolution_method"], "interlanguage_link"
        )

    def test_missing_link_is_not_silently_treated_as_zero(self) -> None:
        with self.assertRaisesRegex(ApiError, "not evidence of zero interest"):
            resolve_topic(
                FakeClient(),
                topic="seed",
                source_language="en",
                languages=["cs"],
            )

    def test_explicit_mapping_handles_missing_link(self) -> None:
        result = resolve_topic(
            FakeClient(),
            topic="seed",
            source_language="en",
            languages=["cs"],
            title_overrides={"cs": "Článek"},
        )
        self.assertEqual(result["articles"][0]["title"], "Článek")
        self.assertEqual(result["articles"][0]["resolution_method"], "explicit_title")
        self.assertEqual(result["articles"][0]["resolution_confidence"], "high")
        self.assertEqual(result["articles"][0]["semantic_match"], "wikidata_qid_match")

    def test_explicit_mapping_qid_mismatch_is_low_confidence(self) -> None:
        class MismatchClient(FakeClient):
            def get_json(self, base_url, params=None):
                payload = super().get_json(base_url, params)
                params = params or {}
                language = base_url.split("//", 1)[1].split(".", 1)[0]
                if "pageprops" in str(params.get("prop")) and language == "cs":
                    payload["query"]["pages"][0]["pageprops"]["wikibase_item"] = "Q2"
                return payload

        result = resolve_topic(
            MismatchClient(),
            topic="seed",
            source_language="en",
            languages=["cs"],
            title_overrides={"cs": "Článek"},
        )
        article = result["articles"][0]
        self.assertEqual(article["resolution_confidence"], "low")
        self.assertEqual(article["semantic_match"], "wikidata_qid_mismatch")
        self.assertIn("differs from source item", result["warnings"][0])

    def test_explicit_mapping_without_qid_is_medium_confidence(self) -> None:
        class MissingQidClient(FakeClient):
            def get_json(self, base_url, params=None):
                payload = super().get_json(base_url, params)
                params = params or {}
                language = base_url.split("//", 1)[1].split(".", 1)[0]
                if "pageprops" in str(params.get("prop")) and language == "cs":
                    payload["query"]["pages"][0]["pageprops"].pop("wikibase_item")
                return payload

        result = resolve_topic(
            MissingQidClient(),
            topic="seed",
            source_language="en",
            languages=["cs"],
            title_overrides={"cs": "Článek"},
        )
        article = result["articles"][0]
        self.assertEqual(article["resolution_confidence"], "medium")
        self.assertEqual(article["semantic_match"], "qid_unavailable")
        self.assertIn("could not be matched", result["warnings"][0])

    def test_unrelated_search_hit_is_rejected(self) -> None:
        class UnrelatedClient(FakeClient):
            def get_json(self, base_url, params=None):
                if (params or {}).get("list") == "search":
                    return {"query": {"search": [{"title": "Charlie Kirk"}]}}
                return super().get_json(base_url, params)

        with self.assertRaisesRegex(ApiError, "No sufficiently close"):
            resolve_topic(
                UnrelatedClient(),
                topic="post przerywany",
                source_language="pl",
                languages=["pl"],
            )


if __name__ == "__main__":
    unittest.main()
