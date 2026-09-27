"""Write auditable data files, charts, HTML, and a one-page PDF report."""

from __future__ import annotations

import csv
from html import escape
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import textwrap
from typing import Any
import unicodedata


COLORS = [
    "#2563eb",
    "#db2777",
    "#059669",
    "#d97706",
    "#7c3aed",
    "#0891b2",
    "#dc2626",
    "#4f46e5",
]
UNAVAILABLE_STATUSES = {
    "not_created",
    "creation_partial",
    "project_data_gap",
    "provisional",
}

# ReportLab's canvas API embeds glyphs but does not provide a verified general
# text-layout engine here. Keep the release boundary closed: letter characters
# must belong to a family whose simple horizontal layout has been reviewed.
ALLOWED_PDF_LETTER_NAME_PREFIXES = (
    "LATIN ",
    "GREEK ",
    "CYRILLIC ",
    "CJK UNIFIED IDEOGRAPH-",
    "CJK COMPATIBILITY IDEOGRAPH-",
    "IDEOGRAPHIC ",
    "HIRAGANA ",
    "KATAKANA ",
    "KATAKANA-HIRAGANA PROLONGED SOUND MARK",
    "HANGUL SYLLABLE ",
    "BOPOMOFO ",
    "FULLWIDTH LATIN ",
)
BIDI_LAYOUT_CLASSES = {"R", "AL", "AN", "RLE", "RLO", "RLI"}
PDF_COMPOSITION_CHARACTER_RANGES = (
    (0x1807, 0x1807, "Mongolian joining punctuation"),
    (0x180A, 0x180A, "Mongolian joining punctuation"),
    (0x1F1E6, 0x1F1FF, "regional-indicator flag characters"),
    (0x1F3FB, 0x1F3FF, "emoji modifier characters"),
)


def _metric_key(analysis: dict[str, Any]) -> str:
    return "share_per_million" if analysis["request"]["metric"] == "share" else "views"


def _metric_label(analysis: dict[str, Any]) -> str:
    if analysis["request"]["metric"] == "share":
        return "Article views per 1M project views"
    return "Monthly article views"


def format_number(value: float | int | None, *, decimals: int = 0) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if abs(number) >= 1_000:
        return f"{number / 1_000:.1f}k"
    return f"{number:.{decimals}f}"


def format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f}%"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _csv_safe(value: str) -> str:
    """Prevent spreadsheet software from interpreting article titles as formulas."""

    return "'" + value if value.startswith(("=", "+", "-", "@")) else value


def write_series_csv(path: Path, series: list[dict[str, Any]]) -> None:
    fieldnames = [
        "language",
        "title",
        "month",
        "views",
        "project_views",
        "share_per_million",
        "data_status",
        "article_observed_days",
        "implicit_zero_days",
        "complete_month",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in series:
            for row in item["monthly"]:
                writer.writerow(
                    {
                        "language": item["language"],
                        "title": _csv_safe(item["title"]),
                        "month": row["month"],
                        "views": row["views"],
                        "project_views": row["project_views"],
                        "share_per_million": (
                            ""
                            if row["share_per_million"] is None
                            else f"{row['share_per_million']:.8f}"
                        ),
                        "data_status": row["data_status"],
                        "article_observed_days": row["article_observed_days"],
                        "implicit_zero_days": row["implicit_zero_days"],
                        "complete_month": str(row["complete_month"]).lower(),
                    }
                )


def _chart_series(analysis: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    key = _metric_key(analysis)
    selected: list[dict[str, Any]] = []
    ordered = sorted(analysis["series"], key=lambda row: int(row["rank"] or 9999))
    for item in ordered[:limit]:
        selected.append(
            {
                "language": item["language"],
                "title": item["title"],
                "points": [
                    (
                        row["month"],
                        None
                        if row.get("data_status") in UNAVAILABLE_STATUSES
                        or row.get(key) is None
                        else float(row[key]),
                    )
                    for row in item["monthly"]
                ],
            }
        )
    return selected


def _report_warnings(analysis: dict[str, Any], *, limit: int = 3) -> list[str]:
    warnings = [str(value).replace("_", " ") for value in analysis.get("warnings", [])]
    return warnings[:limit]


def render_svg(
    analysis: dict[str, Any], *, width: int = 1000, height: int = 390
) -> str:
    series = _chart_series(analysis)
    left, right, top, bottom = 78, 24, 48, 65
    plot_width = width - left - right
    plot_height = height - top - bottom
    all_values = [
        value for item in series for _, value in item["points"] if value is not None
    ]
    maximum = max(all_values, default=1.0)
    if maximum <= 0:
        maximum = 1.0
    maximum *= 1.08
    months = series[0]["points"] if series else []
    month_labels = [month for month, _ in months]
    point_count = max(1, len(month_labels))
    has_gaps = any(value is None for item in series for _, value in item["points"])
    warnings = _report_warnings(analysis, limit=1)
    status_notes: list[str] = []
    if has_gaps:
        status_notes.append("Gaps are not plotted as zero")
    if len(analysis.get("series", [])) > len(series):
        status_notes.append(
            f"{len(analysis['series']) - len(series)} additional series omitted"
        )
    if warnings:
        status_notes.append("Warning: " + warnings[0])

    def x_position(index: int) -> float:
        if point_count <= 1:
            return left + plot_width / 2
        return left + index / (point_count - 1) * plot_width

    def y_position(value: float) -> float:
        return top + plot_height - value / maximum * plot_height

    output = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Interest signals trend chart">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;fill:#334155}"
        ".axis{stroke:#94a3b8;stroke-width:1}.grid{stroke:#e2e8f0;stroke-width:1}"
        ".series{fill:none;stroke-width:2.5;stroke-linejoin:round;stroke-linecap:round}</style>",
        '<rect width="100%" height="100%" fill="#ffffff" rx="12"/>',
        f'<text x="{left}" y="24" font-size="17" font-weight="700">{escape(_metric_label(analysis))}</text>',
    ]
    if status_notes:
        note = " | ".join(status_notes)
        if len(note) > 115:
            note = note[:112] + "..."
        output.append(
            f'<text x="{width - right}" y="24" font-size="10" text-anchor="end" '
            f'fill="#9a3412">{escape(note)}</text>'
        )
    for tick in range(5):
        value = maximum * tick / 4
        y = y_position(value)
        output.append(
            f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>'
        )
        output.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" font-size="11" text-anchor="end">'
            f"{escape(format_number(value, decimals=1))}</text>"
        )
    output.append(
        f'<line class="axis" x1="{left}" x2="{left}" y1="{top}" y2="{top + plot_height}"/>'
    )
    output.append(
        f'<line class="axis" x1="{left}" x2="{width - right}" y1="{top + plot_height}" y2="{top + plot_height}"/>'
    )
    if month_labels:
        tick_count = min(7, len(month_labels))
        indices = sorted(
            {
                round(index * (len(month_labels) - 1) / max(1, tick_count - 1))
                for index in range(tick_count)
            }
        )
        for index in indices:
            x = x_position(index)
            output.append(
                f'<text x="{x:.1f}" y="{height - 40}" font-size="11" text-anchor="middle">'
                f"{escape(month_labels[index])}</text>"
            )
    for index, item in enumerate(series):
        color = COLORS[index % len(COLORS)]
        segment: list[str] = []
        for point_index, (_, value) in enumerate(item["points"]):
            if value is None:
                if len(segment) >= 2:
                    output.append(
                        f'<polyline class="series" stroke="{color}" '
                        f'points="{" ".join(segment)}"/>'
                    )
                segment = []
                continue
            segment.append(f"{x_position(point_index):.1f},{y_position(value):.1f}")
        if len(segment) >= 2:
            output.append(
                f'<polyline class="series" stroke="{color}" points="{" ".join(segment)}"/>'
            )
    legend_y = height - 13
    legend_x = left
    for index, item in enumerate(series):
        color = COLORS[index % len(COLORS)]
        label = f"{item['language']}: {item['title']}"
        if len(label) > 30:
            label = label[:27] + "..."
        output.append(
            f'<line x1="{legend_x}" x2="{legend_x + 17}" y1="{legend_y}" y2="{legend_y}" '
            f'stroke="{color}" stroke-width="3"/>'
        )
        output.append(
            f'<text x="{legend_x + 22}" y="{legend_y + 4}" font-size="10">{escape(label)}</text>'
        )
        legend_x += 115 + min(150, len(label) * 5)
        if legend_x > width - 180:
            legend_x = left
            legend_y -= 15
    output.append("</svg>")
    return "\n".join(output)


def write_svg(path: Path, analysis: dict[str, Any]) -> None:
    path.write_text(render_svg(analysis) + "\n", encoding="utf-8")


def _table_rows(analysis: dict[str, Any], limit: int = 8) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    ordered = sorted(analysis["series"], key=lambda row: int(row["rank"] or 9999))
    for item in ordered[:limit]:
        selected = item["metrics"][item["selected_metric"]]
        raw = item["metrics"]["raw"]
        share = item["metrics"]["share"]
        interval = selected["trend_ci_95_pct"]
        ci = (
            "n/a"
            if interval[0] is None
            else f"{interval[0]:+.0f}% to {interval[1]:+.0f}%"
        )
        rows.append(
            {
                "rank": str(item["rank"] or "-"),
                "language": item["language"],
                "article": item["title"],
                "views": format_number(raw["mean_monthly"]),
                "share": format_number(share["mean_monthly"], decimals=2),
                "trend": format_percent(selected["annualized_trend_pct"]),
                "ci": ci,
                "evidence": f"{item['evidence_quality']['label']} ({item['evidence_quality']['score']:.0f})",
                "score": (
                    "-"
                    if item["screening_score"] is None
                    else f"{item['screening_score']:.0f}"
                ),
            }
        )
    return rows


def render_html(analysis: dict[str, Any]) -> str:
    request = analysis["request"]
    rows = _table_rows(analysis)
    row_html = []
    for row in rows:
        row_html.append(
            "<tr>"
            + "".join(
                f"<td>{escape(row[key])}</td>"
                for key in (
                    "rank",
                    "language",
                    "article",
                    "views",
                    "share",
                    "trend",
                    "ci",
                    "evidence",
                    "score",
                )
            )
            + "</tr>"
        )
    findings = "".join(
        f"<li>{escape(finding)}</li>" for finding in analysis.get("findings", [])[:3]
    )
    omitted = max(0, len(analysis["series"]) - len(rows))
    omitted_note = (
        f"<p class='small'>{omitted} additional language(s) are available in analysis.json.</p>"
        if omitted
        else ""
    )
    warning_values = _report_warnings(analysis, limit=5)
    warnings_html = (
        "<section><h2>Warnings</h2><ul>"
        + "".join(f"<li>{escape(value)}</li>" for value in warning_values)
        + "</ul></section>"
        if warning_values
        else ""
    )
    if request["metric"] == "share":
        comparison_note = (
            "Cross-language raw counts reflect different project sizes; attention per "
            "1M project views is the primary comparison."
        )
    else:
        comparison_note = (
            "Raw article views are primary for this run. Use attention per 1M project "
            "views before comparing differently sized language editions."
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Interest signals - {escape(request["topic"])}</title>
<style>
@page {{ size: A4 landscape; margin: 10mm; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; color: #172033; font: 12px/1.35 Arial, sans-serif; background: #f8fafc; }}
main {{ width: 1120px; min-height: 760px; margin: 0 auto; padding: 26px 34px; background: white; }}
h1 {{ margin: 0 0 3px; font-size: 27px; letter-spacing: -0.4px; }}
.subtitle {{ color: #64748b; margin-bottom: 15px; }}
.kicker {{ color: #2563eb; font-weight: 700; text-transform: uppercase; letter-spacing: 1.2px; font-size: 10px; }}
table {{ border-collapse: collapse; width: 100%; table-layout: fixed; margin-bottom: 12px; }}
th {{ text-align: left; background: #eff6ff; color: #1e3a8a; font-size: 10px; padding: 6px; }}
td {{ border-bottom: 1px solid #e2e8f0; padding: 5px 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
th:nth-child(1), td:nth-child(1) {{ width: 4%; }}
th:nth-child(2), td:nth-child(2) {{ width: 6%; }}
th:nth-child(3), td:nth-child(3) {{ width: 20%; }}
th:nth-child(4), td:nth-child(4) {{ width: 10%; }}
th:nth-child(5), td:nth-child(5) {{ width: 11%; }}
th:nth-child(6), td:nth-child(6) {{ width: 10%; }}
th:nth-child(7), td:nth-child(7) {{ width: 14%; }}
th:nth-child(8), td:nth-child(8) {{ width: 13%; }}
.chart {{ height: 340px; overflow: hidden; border: 1px solid #e2e8f0; border-radius: 12px; }}
.chart svg {{ width: 100%; height: 100%; }}
.bottom {{ display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 24px; margin-top: 12px; }}
h2 {{ font-size: 13px; margin: 0 0 5px; }}
ul {{ margin: 0; padding-left: 18px; }}
.small {{ color: #64748b; font-size: 9px; margin: 5px 0 0; }}
footer {{ margin-top: 8px; padding-top: 7px; border-top: 1px solid #e2e8f0; color: #64748b; font-size: 9px; }}
@media print {{ body {{ background: white; }} main {{ width: auto; min-height: auto; padding: 0; }} }}
</style>
</head>
<body><main>
<div class="kicker">Decision signal, not market proof</div>
<h1>Interest signals: {escape(request["topic"])}</h1>
<div class="subtitle">{escape(request["start"])} to {escape(request["end"])} | metric: {escape(_metric_label(analysis))} | generated {escape(analysis["generated_at"][:10])}</div>
<table><thead><tr><th>#</th><th>Lang</th><th>Article</th><th>Monthly views</th><th>Attention / 1M</th><th>Annual trend</th><th>95% interval</th><th>Evidence</th><th>Score</th></tr></thead>
<tbody>{"".join(row_html)}</tbody></table>
{omitted_note}
<div class="chart">{render_svg(analysis, width=1050, height=360)}</div>
<div class="bottom"><section><h2>Readout</h2><ul>{findings}</ul>{warnings_html}</section>
<section><h2>Interpretation guardrails</h2><p>Wikipedia views measure attention, not unique people, purchase intent, or addressable market. {escape(comparison_note)} Verify title mappings, event spikes, product demand, and conversion through separate research.</p></section></div>
<footer>Source: Wikimedia Pageviews API and MediaWiki Action API. User-agent traffic only. Daily data rolled into complete months. Current-title history may omit views credited to old titles or redirects. Screening uses fixed anchors and the stated weights; it is not a launch threshold.</footer>
</main></body></html>"""


def write_html(path: Path, analysis: dict[str, Any]) -> None:
    path.write_text(render_html(analysis), encoding="utf-8")


FontSource = str | bytes


def _font_candidates() -> list[tuple[FontSource, FontSource]]:
    """Return regular/bold font pairs in deterministic preference order."""

    candidates: list[tuple[FontSource, FontSource]] = []
    environment_regular = os.environ.get("INTEREST_SIGNALS_FONT", "")
    environment_bold = os.environ.get("INTEREST_SIGNALS_BOLD_FONT", "")
    if environment_regular:
        candidates.append(
            (environment_regular, environment_bold or environment_regular)
        )

    # This pure-font package exposes compressed TrueType buffers without needing
    # PyMuPDF itself. FiraGO covers 68 languages across several writing systems.
    if importlib.util.find_spec("pymupdf_fonts"):
        from pymupdf_fonts import fontbuffers

        candidates.append((fontbuffers["figo"](), fontbuffers["figbo"]()))

    candidates.extend(
        [
            (
                "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
                "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            ),
            (
                "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            ),
            (
                "/Library/Fonts/Arial Unicode.ttf",
                "/Library/Fonts/Arial Unicode.ttf",
            ),
            (
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            ),
            (
                "/usr/local/share/fonts/DejaVuSans.ttf",
                "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
            ),
            (
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/arialbd.ttf",
            ),
            (
                "C:/Windows/Fonts/DejaVuSans.ttf",
                "C:/Windows/Fonts/DejaVuSans-Bold.ttf",
            ),
        ]
    )
    return candidates


def _register_report_fonts(
    required_codepoints: set[int] | None = None,
) -> tuple[str, str, set[int] | None]:
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFError, TTFont
    except ImportError as error:
        raise RuntimeError(
            "PDF output requires ReportLab. Run `python -m pip install -r "
            "requirements.lock.txt` in the skill directory."
        ) from error

    def available(source: FontSource) -> bool:
        return isinstance(source, bytes) or Path(source).is_file()

    def load_font(name: str, source: FontSource) -> Any:
        return TTFont(name, BytesIO(source) if isinstance(source, bytes) else source)

    usable: list[tuple[FontSource, FontSource, set[int]]] = []
    invalid_bold_font = False
    for regular_source, bold_source in _font_candidates():
        if not available(regular_source):
            continue
        try:
            regular_coverage = set(
                load_font(
                    "InterestSignalsCandidateRegular", regular_source
                ).face.charToGlyph
            )
        except TTFError:
            continue

        if available(bold_source):
            try:
                bold_coverage = set(
                    load_font(
                        "InterestSignalsCandidateBold", bold_source
                    ).face.charToGlyph
                )
            except TTFError:
                invalid_bold_font = True
                continue
            effective_bold_source = bold_source
        else:
            bold_coverage = regular_coverage
            effective_bold_source = regular_source

        shared_coverage = regular_coverage & bold_coverage
        usable.append((regular_source, effective_bold_source, shared_coverage))
        if required_codepoints is None or required_codepoints.issubset(shared_coverage):
            break
    if usable:
        regular_source, bold_source, coverage = usable[-1]
        pdfmetrics.registerFont(load_font("InterestSignalsRegular", regular_source))
        pdfmetrics.registerFont(load_font("InterestSignalsBold", bold_source))
        return "InterestSignalsRegular", "InterestSignalsBold", coverage
    if invalid_bold_font:
        raise RuntimeError(
            "A configured PDF bold font is not a valid TrueType font. Set "
            "INTEREST_SIGNALS_BOLD_FONT to a valid file, or unset it to reuse "
            "the regular face."
        )
    return "Helvetica", "Helvetica-Bold", None


def _pdf_text(text: str, font_coverage: set[int] | None) -> str:
    if font_coverage is not None:
        missing = sorted(
            {character for character in text if ord(character) not in font_coverage}
        )
        if missing:
            preview = "".join(missing[:8])
            raise RuntimeError(
                "The selected PDF font does not cover all report characters. Set "
                "INTEREST_SIGNALS_FONT and INTEREST_SIGNALS_BOLD_FONT to compatible "
                f"TrueType fonts. Unsupported characters include: {preview!r}."
            )
        return text
    try:
        text.encode("latin-1")
    except UnicodeEncodeError as error:
        raise RuntimeError(
            "PDF text contains characters that the built-in Latin font cannot "
            "represent. Install requirements.lock.txt or set "
            "INTEREST_SIGNALS_FONT to a Unicode TrueType font."
        ) from error
    return text


def _visible_pdf_text(analysis: dict[str, Any]) -> list[str]:
    request = analysis["request"]
    values = [
        str(request["topic"]),
        str(request["start"]),
        str(request["end"]),
        str(analysis["generated_at"][:10]),
        _metric_label(analysis),
    ]
    values.extend(str(finding) for finding in analysis.get("findings", [])[:3])
    values.extend(_report_warnings(analysis, limit=2))
    for row in _table_rows(analysis, limit=6):
        values.extend(row.values())
    for item in _chart_series(analysis, limit=6):
        values.extend((str(item["language"]), str(item["title"])))
        values.extend(str(month) for month, _ in item["points"])
    return values


def _require_simple_pdf_text_layout(analysis: dict[str, Any]) -> None:
    issues: set[str] = set()
    examples: set[str] = set()
    for value in _visible_pdf_text(analysis):
        for character in value:
            codepoint = ord(character)
            category = unicodedata.category(character)
            name = unicodedata.name(character, "")
            for start, end, label in PDF_COMPOSITION_CHARACTER_RANGES:
                if start <= codepoint <= end:
                    issues.add(label)
                    examples.add(character)
                    break
            if unicodedata.bidirectional(character) in BIDI_LAYOUT_CLASSES:
                issues.add("right-to-left or Arabic-number bidi classes")
                examples.add(character)
            if category.startswith("M"):
                issues.add("Unicode mark characters")
                examples.add(character)
                continue
            if category.startswith("C"):
                issues.add(
                    "Unicode control, format, private, surrogate, or unassigned characters"
                )
                examples.add(character)
                continue
            if category.startswith("L") and not name.startswith(
                ALLOWED_PDF_LETTER_NAME_PREFIXES
            ):
                issues.add("letters outside the verified PDF script allowlist")
                examples.add(character)
                continue
            if category.startswith("Z") and category != "Zs":
                issues.add("line or paragraph separator characters")
                examples.add(character)
                continue
            if not (category.startswith(("L", "N", "P", "S")) or category == "Zs"):
                issues.add("characters outside the verified PDF allowlist")
                examples.add(character)

    if issues:
        issue_text = ", ".join(sorted(issues))
        preview = "".join(sorted(examples)[:8])
        raise RuntimeError(
            "PDF output contains text outside this renderer's conservative simple-layout "
            "allowlist. "
            f"Detected: {issue_text}; example characters: {preview!r}. Omit `pdf` "
            "from formats (for example, `--formats csv,svg,html`) and use the Unicode "
            "HTML/SVG outputs, or use an externally verified shaping-capable PDF workflow."
        )


def _require_font_coverage(
    analysis: dict[str, Any], *, font_coverage: set[int] | None
) -> None:
    unsupported = sorted(
        {
            character
            for value in _visible_pdf_text(analysis)
            for character in value
            if (
                ord(character) > 255
                if font_coverage is None
                else ord(character) not in font_coverage
            )
        }
    )
    if unsupported:
        preview = "".join(unsupported[:8])
        if font_coverage is None:
            message = (
                "PDF output contains non-Latin text but no Unicode TrueType font was found. "
                "Install requirements.lock.txt or set INTEREST_SIGNALS_FONT and "
                "INTEREST_SIGNALS_BOLD_FONT"
            )
        else:
            message = (
                "The selected PDF font does not cover all report characters. Set "
                "INTEREST_SIGNALS_FONT and INTEREST_SIGNALS_BOLD_FONT to compatible "
                "TrueType fonts"
            )
        raise RuntimeError(f"{message}. Unsupported characters include: {preview!r}.")


def _draw_pdf_chart(
    canvas: Any,
    analysis: dict[str, Any],
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    font_name: str,
    font_coverage: set[int] | None,
) -> None:
    from reportlab.lib import colors

    series = _chart_series(analysis, limit=6)
    all_values = [
        value for item in series for _, value in item["points"] if value is not None
    ]
    maximum = max(all_values, default=1.0)
    if maximum <= 0:
        maximum = 1.0
    maximum *= 1.08
    inner_left, inner_right, inner_top, inner_bottom = 45, 10, 20, 34
    plot_x = x + inner_left
    plot_y = y + inner_bottom
    plot_width = width - inner_left - inner_right
    plot_height = height - inner_top - inner_bottom
    canvas.setFillColor(colors.HexColor("#ffffff"))
    canvas.setStrokeColor(colors.HexColor("#dbe4ef"))
    canvas.roundRect(x, y, width, height, 7, fill=1, stroke=1)
    canvas.setFont(font_name, 8.2)
    canvas.setFillColor(colors.HexColor("#334155"))
    canvas.drawString(
        x + 10,
        y + height - 14,
        _pdf_text(_metric_label(analysis), font_coverage),
    )
    for tick in range(5):
        value = maximum * tick / 4
        tick_y = plot_y + value / maximum * plot_height
        canvas.setStrokeColor(colors.HexColor("#e2e8f0"))
        canvas.line(plot_x, tick_y, plot_x + plot_width, tick_y)
        canvas.setFont(font_name, 6.5)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawRightString(
            plot_x - 5,
            tick_y - 2,
            _pdf_text(format_number(value, decimals=1), font_coverage),
        )
    point_count = max(1, len(series[0]["points"]) if series else 1)

    def px(index: int) -> float:
        if point_count <= 1:
            return plot_x + plot_width / 2
        return plot_x + index / (point_count - 1) * plot_width

    def py(value: float) -> float:
        return plot_y + value / maximum * plot_height

    for series_index, item in enumerate(series):
        canvas.setStrokeColor(colors.HexColor(COLORS[series_index % len(COLORS)]))
        canvas.setLineWidth(1.6)
        path = canvas.beginPath()
        drawing = False
        for point_index, (_, value) in enumerate(item["points"]):
            if value is None:
                drawing = False
                continue
            if not drawing:
                path.moveTo(px(point_index), py(value))
                drawing = True
            else:
                path.lineTo(px(point_index), py(value))
        canvas.drawPath(path, stroke=1, fill=0)
    if series and series[0]["points"]:
        labels = [month for month, _ in series[0]["points"]]
        tick_count = min(6, len(labels))
        indices = sorted(
            {
                round(index * (len(labels) - 1) / max(1, tick_count - 1))
                for index in range(tick_count)
            }
        )
        canvas.setFont(font_name, 6.3)
        canvas.setFillColor(colors.HexColor("#64748b"))
        for index in indices:
            canvas.drawCentredString(
                px(index),
                plot_y - 11,
                _pdf_text(labels[index], font_coverage),
            )
    legend_x = plot_x
    legend_y = y + 10
    canvas.setFont(font_name, 6.5)
    for series_index, item in enumerate(series):
        color = colors.HexColor(COLORS[series_index % len(COLORS)])
        canvas.setStrokeColor(color)
        canvas.setLineWidth(2)
        canvas.line(legend_x, legend_y + 2, legend_x + 11, legend_y + 2)
        canvas.setFillColor(colors.HexColor("#334155"))
        label = f"{item['language']}: {item['title']}"
        if len(label) > 24:
            label = label[:21] + "..."
        canvas.drawString(legend_x + 15, legend_y, _pdf_text(label, font_coverage))
        legend_x += 103

    if any(value is None for item in series for _, value in item["points"]):
        canvas.setFillColor(colors.HexColor("#9a3412"))
        canvas.setFont(font_name, 5.8)
        canvas.drawRightString(
            x + width - 10,
            y + 9,
            "Gaps are not plotted as zero.",
        )


def write_pdf(path: Path, analysis: dict[str, Any]) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.pdfgen import canvas as pdf_canvas
    except ImportError as error:
        raise RuntimeError(
            "PDF output requires ReportLab. Install requirements.lock.txt."
        ) from error

    _require_simple_pdf_text_layout(analysis)
    required_codepoints = {
        ord(character) for value in _visible_pdf_text(analysis) for character in value
    }
    regular, bold, font_coverage = _register_report_fonts(required_codepoints)
    _require_font_coverage(analysis, font_coverage=font_coverage)
    page_width, page_height = landscape(A4)
    canvas = pdf_canvas.Canvas(str(path), pagesize=(page_width, page_height))
    canvas.setTitle(f"Interest signals - {analysis['request']['topic']}")
    margin = 32

    canvas.setFillColor(colors.HexColor("#2563eb"))
    canvas.setFont(bold, 7.5)
    canvas.drawString(margin, page_height - 29, "DECISION SIGNAL, NOT MARKET PROOF")
    canvas.setFillColor(colors.HexColor("#172033"))
    canvas.setFont(bold, 20)
    title = _pdf_text(
        f"Interest signals: {analysis['request']['topic']}", font_coverage
    )
    canvas.drawString(margin, page_height - 52, title[:95])
    canvas.setFillColor(colors.HexColor("#64748b"))
    canvas.setFont(regular, 8)
    subtitle = (
        f"{analysis['request']['start']} to {analysis['request']['end']} | "
        f"metric: {_metric_label(analysis)} | generated {analysis['generated_at'][:10]}"
    )
    canvas.drawString(margin, page_height - 67, _pdf_text(subtitle, font_coverage))

    rows = _table_rows(analysis, limit=6)
    table_x = margin
    table_top = page_height - 82
    row_height = 15
    columns = [
        ("#", 24, "rank"),
        ("Lang", 38, "language"),
        ("Article", 145, "article"),
        ("Monthly", 65, "views"),
        ("Attn / 1M", 66, "share"),
        ("Annual", 62, "trend"),
        ("95% interval", 85, "ci"),
        ("Evidence", 78, "evidence"),
        ("Score", 45, "score"),
    ]
    table_width = sum(width for _, width, _ in columns)
    canvas.setFillColor(colors.HexColor("#eff6ff"))
    canvas.roundRect(
        table_x, table_top - row_height, table_width, row_height, 3, fill=1, stroke=0
    )
    current_x = table_x
    canvas.setFont(bold, 6.7)
    canvas.setFillColor(colors.HexColor("#1e3a8a"))
    for label, width, _ in columns:
        canvas.drawString(current_x + 4, table_top - 10.5, label)
        current_x += width
    for row_index, row in enumerate(rows):
        baseline = table_top - row_height * (row_index + 2) + 4.2
        canvas.setStrokeColor(colors.HexColor("#e2e8f0"))
        canvas.setLineWidth(0.5)
        canvas.line(table_x, baseline - 4, table_x + table_width, baseline - 4)
        current_x = table_x
        canvas.setFillColor(colors.HexColor("#243047"))
        canvas.setFont(regular, 6.7)
        for _, width, key in columns:
            value = row[key]
            max_chars = max(4, int(width / 4.2))
            if len(value) > max_chars:
                value = value[: max_chars - 3] + "..."
            canvas.drawString(current_x + 4, baseline, _pdf_text(value, font_coverage))
            current_x += width

    omitted_count = max(0, len(analysis["series"]) - len(rows))
    table_bottom = table_top - row_height * (len(rows) + 1)
    if omitted_count:
        canvas.setFont(regular, 6.2)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(
            table_x,
            table_bottom - 10,
            f"{omitted_count} additional language(s) omitted from this one-page summary; "
            "see analysis.json and series.csv.",
        )

    chart_y = 155
    chart_height = 300
    if rows:
        chart_height = min(
            chart_height,
            table_bottom - chart_y - (18 if omitted_count else 8),
        )
    chart_height = max(165, chart_height)
    _draw_pdf_chart(
        canvas,
        analysis,
        x=margin,
        y=chart_y,
        width=page_width - 2 * margin,
        height=chart_height,
        font_name=regular,
        font_coverage=font_coverage,
    )

    notes_top = chart_y - 14
    canvas.setFillColor(colors.HexColor("#172033"))
    canvas.setFont(bold, 8)
    canvas.drawString(margin, notes_top, "Readout")
    canvas.setFont(regular, 7)
    canvas.setFillColor(colors.HexColor("#334155"))
    note_y = notes_top - 12
    for finding in analysis.get("findings", [])[:3]:
        wrapped = textwrap.wrap(finding, width=84)
        if len(wrapped) > 2:
            wrapped = [wrapped[0], wrapped[1][:80].rstrip() + "..."]
        else:
            wrapped = wrapped[:2]
        if not wrapped:
            continue
        canvas.drawString(
            margin + 5, note_y, _pdf_text("- " + wrapped[0], font_coverage)
        )
        note_y -= 9
        if len(wrapped) > 1:
            canvas.drawString(margin + 12, note_y, _pdf_text(wrapped[1], font_coverage))
            note_y -= 9

    warning_values = _report_warnings(analysis, limit=2)
    if warning_values:
        note_y -= 1
        canvas.setFont(bold, 7)
        canvas.setFillColor(colors.HexColor("#9a3412"))
        canvas.drawString(margin, note_y, "Warnings")
        note_y -= 10
        canvas.setFont(regular, 6.3)
        for warning in warning_values:
            warning_text = warning if len(warning) <= 92 else warning[:89] + "..."
            canvas.drawString(
                margin + 5,
                note_y,
                _pdf_text("- " + warning_text, font_coverage),
            )
            note_y -= 8

    guard_x = page_width / 2 + 16
    canvas.setFont(bold, 8)
    canvas.setFillColor(colors.HexColor("#172033"))
    canvas.drawString(guard_x, notes_top, "Interpretation guardrails")
    metric_note = (
        "Attention per 1M project views is primary for cross-language comparison."
        if analysis["request"]["metric"] == "share"
        else "Raw views are primary here; normalize before comparing project sizes."
    )
    guardrail = (
        "Wikipedia views measure attention, not unique people, purchase intent, or market size. "
        f"{metric_note} Validate titles, event spikes, demand, and conversion separately."
    )
    canvas.setFont(regular, 7)
    canvas.setFillColor(colors.HexColor("#334155"))
    guard_y = notes_top - 12
    for line in textwrap.wrap(guardrail, width=86)[:5]:
        canvas.drawString(guard_x, guard_y, _pdf_text(line, font_coverage))
        guard_y -= 9

    canvas.setStrokeColor(colors.HexColor("#dbe4ef"))
    canvas.line(margin, 35, page_width - margin, 35)
    canvas.setFont(regular, 5.8)
    canvas.setFillColor(colors.HexColor("#64748b"))
    footer = (
        "Source: Wikimedia Pageviews API and MediaWiki Action API. User traffic only; daily "
        "data rolled into complete months. Current-title history can omit views credited to "
        "old titles or redirects. Screening uses fixed anchors and stated weights; it is not "
        "a launch threshold."
    )
    canvas.drawString(margin, 23, _pdf_text(footer, font_coverage))
    canvas.showPage()
    canvas.save()


def write_artifacts(
    output_dir: Path,
    analysis: dict[str, Any],
    *,
    formats: set[str],
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    if "csv" in formats:
        path = output_dir / "series.csv"
        write_series_csv(path, analysis["series"])
        files["csv"] = path.name
    if "svg" in formats:
        path = output_dir / "trend.svg"
        write_svg(path, analysis)
        files["svg"] = path.name
    if "html" in formats:
        path = output_dir / "report.html"
        write_html(path, analysis)
        files["html"] = path.name
    if "pdf" in formats:
        path = output_dir / "report.pdf"
        write_pdf(path, analysis)
        files["pdf"] = path.name
    return files
