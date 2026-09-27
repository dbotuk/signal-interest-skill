from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[2] / "interest-signals"
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from interest_signals.artifacts import (  # noqa: E402
    _require_simple_pdf_text_layout,
    render_html,
    render_svg,
    write_artifacts,
)


def sample_analysis() -> dict:
    monthly = []
    for month in range(24):
        year = 2024 + month // 12
        number = month % 12 + 1
        monthly.append(
            {
                "month": f"{year:04d}-{number:02d}",
                "views": 1000 + month * 50,
                "project_views": 100_000_000,
                "share_per_million": 10 + month * 0.5,
                "days_in_range": 30,
                "article_observed_days": 30,
                "implicit_zero_days": 0,
                "project_observed_days": 30,
                "data_status": "observed",
                "complete_month": True,
            }
        )
    stats = {
        "months": 24,
        "annualized_trend_pct": 22.5,
        "trend_ci_95_pct": [15.0, 29.0],
        "trailing_12m_growth_pct": 20.0,
        "recent_3m_momentum_pct": 4.0,
        "mean_monthly": 1575.0,
        "median_monthly": 1575.0,
        "peak_month_share": 0.06,
        "trend_without_peak_pct": 21.0,
        "event_sensitive": False,
    }
    item = {
        "language": "uk",
        "title": "Астрономія",
        "url": "https://uk.wikipedia.org/wiki/A",
        "resolution": {"method": "interlanguage_link", "confidence": "high"},
        "monthly": monthly,
        "metrics": {"raw": stats, "share": {**stats, "mean_monthly": 15.75}},
        "selected_metric": "share",
        "evidence_quality": {"score": 82.0, "label": "high", "components": {}},
        "warnings": [],
        "rank": 1,
        "screening_score": None,
    }
    return {
        "schema_version": "1.1",
        "generated_at": datetime.now(UTC).isoformat(),
        "request": {
            "topic": "астрономія",
            "start": "2024-01-01",
            "end": "2025-12-31",
            "metric": "share",
        },
        "series": [item],
        "warnings": [],
        "findings": [
            "uk has a sustained positive trend, but validate product demand separately."
        ],
    }


def many_series_analysis(count: int = 9) -> dict:
    analysis = sample_analysis()
    template = analysis["series"][0]
    series = []
    for index in range(count):
        item = deepcopy(template)
        item["language"] = "uk" if index == 0 else f"x{index}"
        item["title"] = "Астрономія" if index == 0 else f"Article {index}"
        item["rank"] = index + 1
        item["screening_score"] = 80.0 - index
        series.append(item)
    analysis["series"] = series
    analysis["warnings"] = ["uk: low_traffic_base", "x1: project_data_gap_excluded"]
    return analysis


def packaged_font_pair() -> list[tuple[bytes, bytes]]:
    from pymupdf_fonts import fontbuffers

    return [(fontbuffers["figo"](), fontbuffers["figbo"]())]


class ArtifactTests(unittest.TestCase):
    def test_writes_text_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            files = write_artifacts(
                Path(directory), sample_analysis(), formats={"csv", "svg", "html"}
            )
            self.assertEqual(set(files), {"csv", "svg", "html"})
            html = (Path(directory) / "report.html").read_text()
            svg = (Path(directory) / "trend.svg").read_text()
            self.assertIn("Астрономія", html)
            self.assertIn("<h1>Interest signals: астрономія</h1>", html)
            self.assertIn('aria-label="Interest signals trend chart"', svg)
            self.assertTrue(svg.startswith("<svg"))

    def test_non_pdf_artifacts_preserve_complex_script_text(self) -> None:
        analysis = sample_analysis()
        analysis["request"]["topic"] = "اللغة العربية"
        analysis["series"][0]["title"] = "اللغة العربية"
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            write_artifacts(output_dir, analysis, formats={"csv", "svg", "html"})
            for filename in ("series.csv", "trend.svg", "report.html"):
                self.assertIn(
                    "اللغة العربية", output_dir.joinpath(filename).read_text()
                )

    def test_pdf_preflight_accepts_verified_precomposed_scripts(self) -> None:
        accepted = (
            "Crème brûlée",
            "Ελληνικά",
            "Українська",
            "中文⺀㇀々〆",
            "ひらがな",
            "カタカナー",
            "한국어",
            "ㄅㄆㄇ",
            "ＦＵＬＬＷＩＤＴＨ１２３",
        )
        for text in accepted:
            with self.subTest(text=text):
                analysis = sample_analysis()
                analysis["request"]["topic"] = text
                analysis["series"][0]["title"] = text
                _require_simple_pdf_text_layout(analysis)

    def test_chart_breaks_gaps_instead_of_drawing_zero(self) -> None:
        analysis = sample_analysis()
        analysis["series"][0]["monthly"][10]["data_status"] = "project_data_gap"
        svg = render_svg(analysis)
        self.assertEqual(svg.count("<polyline"), 2)
        self.assertIn("Gaps are not plotted as zero", svg)

    def test_human_reports_disclose_warnings_and_omissions(self) -> None:
        analysis = many_series_analysis()
        html = render_html(analysis)
        svg = render_svg(analysis)
        self.assertIn("1 additional language(s)", html)
        self.assertIn("Warnings", html)
        self.assertIn("low traffic base", html)
        self.assertIn("1 additional series omitted", svg)
        self.assertIn("Warning: uk: low traffic base", svg)

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab")
        and importlib.util.find_spec("pypdf")
        and importlib.util.find_spec("pymupdf_fonts"),
        "PDF runtime dependencies not installed",
    )
    def test_pdf_is_one_page_and_preserves_cyrillic(self) -> None:
        from pypdf import PdfReader

        with tempfile.TemporaryDirectory() as directory:
            analysis = many_series_analysis()
            analysis["findings"] = [
                "First concise finding.",
                "Second concise finding.",
                "This deliberately long third finding verifies that a constrained "
                "one-page readout marks omitted words instead of ending as though an "
                "incomplete sentence were the complete result.",
            ]
            with patch(
                "interest_signals.artifacts._font_candidates",
                return_value=packaged_font_pair(),
            ):
                files = write_artifacts(Path(directory), analysis, formats={"pdf"})
            pdf_path = Path(directory) / files["pdf"]
            self.assertGreater(pdf_path.stat().st_size, 5000)
            reader = PdfReader(str(pdf_path))
            self.assertEqual(len(reader.pages), 1)
            self.assertEqual(reader.metadata.title, "Interest signals - астрономія")
            extracted_text = reader.pages[0].extract_text()
            self.assertIn("Interest signals: астрономія", extracted_text)
            self.assertIn("Астрономія", extracted_text)
            self.assertIn("астрономія", extracted_text)
            self.assertIn("3 additional language(s) omitted", extracted_text)
            self.assertIn("Warnings", extracted_text)
            self.assertIn("...", extracted_text)

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab"), "ReportLab not installed"
    )
    def test_pdf_rejects_non_latin_text_without_unicode_font(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with patch("interest_signals.artifacts._font_candidates", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "non-Latin text"):
                    write_artifacts(output_dir, sample_analysis(), formats={"pdf"})
            self.assertFalse((output_dir / "report.pdf").exists())

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab")
        and importlib.util.find_spec("pymupdf_fonts"),
        "PDF font dependencies not installed",
    )
    def test_pdf_requires_bold_face_to_cover_visible_unicode(self) -> None:
        import reportlab

        from pymupdf_fonts import fontbuffers

        narrow_bold = Path(reportlab.__file__).resolve().parent / "fonts" / "VeraBd.ttf"
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with (
                patch(
                    "interest_signals.artifacts._font_candidates",
                    return_value=[(fontbuffers["figo"](), str(narrow_bold))],
                ),
                patch("reportlab.pdfgen.canvas.Canvas") as canvas_constructor,
            ):
                with self.assertRaisesRegex(RuntimeError, "does not cover"):
                    write_artifacts(output_dir, sample_analysis(), formats={"pdf"})
                canvas_constructor.assert_not_called()
            self.assertFalse((output_dir / "report.pdf").exists())

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab")
        and importlib.util.find_spec("pymupdf_fonts"),
        "PDF font dependencies not installed",
    )
    def test_pdf_rejects_invalid_bold_font_cleanly(self) -> None:
        from pymupdf_fonts import fontbuffers

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with (
                patch(
                    "interest_signals.artifacts._font_candidates",
                    return_value=[(fontbuffers["figo"](), b"not a TrueType font")],
                ),
                patch("reportlab.pdfgen.canvas.Canvas") as canvas_constructor,
            ):
                with self.assertRaisesRegex(RuntimeError, "bold font is not a valid"):
                    write_artifacts(output_dir, sample_analysis(), formats={"pdf"})
                canvas_constructor.assert_not_called()
            self.assertFalse((output_dir / "report.pdf").exists())

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab"), "ReportLab not installed"
    )
    def test_pdf_preflights_displayed_chart_month_labels(self) -> None:
        analysis = sample_analysis()
        analysis["series"][0]["monthly"][0]["month"] = "עברית"
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with patch("reportlab.pdfgen.canvas.Canvas") as canvas_constructor:
                with self.assertRaisesRegex(RuntimeError, "right-to-left"):
                    write_artifacts(output_dir, analysis, formats={"pdf"})
                canvas_constructor.assert_not_called()
            self.assertFalse((output_dir / "report.pdf").exists())

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab")
        and importlib.util.find_spec("pymupdf_fonts"),
        "PDF font dependencies not installed",
    )
    def test_pdf_rejects_cjk_when_selected_font_lacks_glyphs(self) -> None:
        analysis = sample_analysis()
        analysis["request"]["topic"] = "日本語"
        analysis["series"][0]["title"] = "日本語"
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with patch(
                "interest_signals.artifacts._font_candidates",
                return_value=packaged_font_pair(),
            ):
                with self.assertRaisesRegex(RuntimeError, "does not cover"):
                    write_artifacts(output_dir, analysis, formats={"pdf"})
            self.assertFalse((output_dir / "report.pdf").exists())

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab"), "ReportLab not installed"
    )
    def test_pdf_rejects_scripts_needing_shaping_or_bidi(self) -> None:
        cases = (
            ("Arabic", "اللغة العربية", "right-to-left or Arabic-number"),
            ("Hebrew", "עברית", "right-to-left or Arabic-number"),
            ("Devanagari", "हिन्दी", "Unicode mark characters"),
        )
        for label, text, expected in cases:
            with self.subTest(script=label), tempfile.TemporaryDirectory() as directory:
                analysis = sample_analysis()
                analysis["request"]["topic"] = text
                analysis["series"][0]["title"] = text
                output_dir = Path(directory)
                with self.assertRaisesRegex(RuntimeError, expected):
                    write_artifacts(output_dir, analysis, formats={"pdf"})
                self.assertFalse((output_dir / "report.pdf").exists())

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab"), "ReportLab not installed"
    )
    def test_pdf_rejects_unverified_scripts_before_canvas_creation(self) -> None:
        cases = (
            ("decomposed Hangul Jamo", "한", "letters outside"),
            ("Phags-pa", "ꡀ", "letters outside"),
            ("Saurashtra", "ꢂ", "letters outside"),
            ("Cham", "ꨀ", "letters outside"),
            ("Egyptian layout control", "𓐰", "Unicode control"),
            ("ordinary unverified Armenian", "Հայերեն", "letters outside"),
            ("Mongolian joining punctuation", "᠊᠇᠊", "Mongolian joining punctuation"),
            ("regional-indicator flag", "🇺🇦", "regional-indicator flag"),
            ("emoji skin-tone modifier", "👍🏽", "emoji modifier"),
        )
        for label, text, expected in cases:
            with self.subTest(script=label), tempfile.TemporaryDirectory() as directory:
                analysis = sample_analysis()
                analysis["request"]["topic"] = text
                analysis["series"][0]["title"] = text
                output_dir = Path(directory)
                with patch("reportlab.pdfgen.canvas.Canvas") as canvas_constructor:
                    with self.assertRaisesRegex(RuntimeError, expected):
                        write_artifacts(output_dir, analysis, formats={"pdf"})
                    canvas_constructor.assert_not_called()
                self.assertFalse((output_dir / "report.pdf").exists())


if __name__ == "__main__":
    unittest.main()
