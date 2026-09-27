"""Resolve a human topic to canonical articles in multiple Wikipedias."""

from __future__ import annotations

from difflib import SequenceMatcher
import re
import unicodedata
from typing import Any
from urllib.parse import quote

from .client import ApiError, JsonClient


def _api_url(language: str) -> str:
    return f"https://{language}.wikipedia.org/w/api.php"


def _page_url(language: str, title: str) -> str:
    return f"https://{language}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"


def search_titles(
    client: JsonClient, language: str, query: str, *, limit: int = 5
) -> list[str]:
    payload = client.get_json(
        _api_url(language),
        {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srnamespace": 0,
            "srlimit": limit,
            "srprop": "",
            "format": "json",
            "formatversion": 2,
            "utf8": 1,
            "maxlag": 5,
        },
    )
    results = payload.get("query", {}).get("search", [])
    return [item["title"] for item in results if isinstance(item.get("title"), str)]


def _normalized_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)


def _plausible_title(query: str, title: str) -> bool:
    query_tokens = _normalized_tokens(query)
    title_tokens = _normalized_tokens(title)
    if not query_tokens or not title_tokens:
        return False
    query_text = " ".join(query_tokens)
    title_text = " ".join(title_tokens)
    query_set = set(query_tokens)
    title_set = set(title_tokens)
    if (
        query_text == title_text
        or query_set.issubset(title_set)
        or title_set.issubset(query_set)
    ):
        return True
    overlap = len(query_set & title_set) / len(query_set)
    similarity = SequenceMatcher(None, query_text, title_text).ratio()
    return overlap >= 0.5 and similarity >= 0.35


def page_info(client: JsonClient, language: str, title: str) -> dict[str, Any]:
    payload = client.get_json(
        _api_url(language),
        {
            "action": "query",
            "titles": title,
            "redirects": 1,
            "converttitles": 1,
            "prop": "pageprops|description|info|revisions",
            "ppprop": "wikibase_item|disambiguation",
            "rvdir": "newer",
            "rvlimit": 1,
            "rvprop": "timestamp",
            "format": "json",
            "formatversion": 2,
            "maxlag": 5,
        },
    )
    query = payload.get("query", {})
    pages = query.get("pages", [])
    if not pages:
        return {"missing": True, "requested_title": title, "language": language}
    page = pages[0]
    missing = bool(page.get("missing", False) or page.get("invalid", False))
    pageprops = page.get("pageprops", {}) or {}
    redirects = query.get("redirects", []) or []
    redirected_from = None
    if redirects:
        redirected_from = redirects[0].get("from")
    canonical = page.get("title", title)
    revisions = page.get("revisions", []) or []
    created_at = revisions[0].get("timestamp") if revisions else None
    return {
        "language": language,
        "requested_title": title,
        "title": canonical,
        "missing": missing,
        "is_disambiguation": "disambiguation" in pageprops,
        "wikidata_id": pageprops.get("wikibase_item"),
        "description": page.get("description"),
        "redirected_from": redirected_from,
        "page_id": page.get("pageid"),
        "created_at": created_at,
        "url": _page_url(language, canonical),
    }


def choose_search_result(
    client: JsonClient, language: str, query: str
) -> tuple[dict[str, Any], list[str]]:
    candidates = search_titles(client, language, query)
    if not candidates:
        raise ApiError(
            f"No article candidates found in {language} Wikipedia for {query!r}. "
            f"Provide an explicit title with --title {language}=ARTICLE."
        )
    inspected: list[str] = []
    for candidate in candidates:
        inspected.append(candidate)
        if not _plausible_title(query, candidate):
            continue
        info = page_info(client, language, candidate)
        if not info["missing"] and not info["is_disambiguation"]:
            return info, inspected
    raise ApiError(
        f"No sufficiently close, non-disambiguation article was found in {language} "
        f"Wikipedia for {query!r}. Provide --title {language}=ARTICLE after reviewing "
        "the candidates; do not interpret this as zero interest. Candidates: "
        + ", ".join(inspected)
    )


def language_links(
    client: JsonClient, source_language: str, source_title: str
) -> dict[str, str]:
    links: dict[str, str] = {}
    continuation: dict[str, Any] = {}
    while True:
        params: dict[str, Any] = {
            "action": "query",
            "titles": source_title,
            "prop": "langlinks",
            "lllimit": "max",
            "format": "json",
            "formatversion": 2,
            "maxlag": 5,
        }
        params.update(continuation)
        payload = client.get_json(_api_url(source_language), params)
        pages = payload.get("query", {}).get("pages", [])
        if pages:
            for link in pages[0].get("langlinks", []) or []:
                language = link.get("lang")
                title = link.get("title")
                if isinstance(language, str) and isinstance(title, str):
                    links[language] = title
        next_page = payload.get("continue")
        if not isinstance(next_page, dict):
            break
        continuation = next_page
    return links


def resolve_topic(
    client: JsonClient,
    *,
    topic: str,
    source_language: str,
    languages: list[str],
    title_overrides: dict[str, str] | None = None,
    allow_search_fallback: bool = False,
) -> dict[str, Any]:
    """Return auditable canonical title mappings for each requested language."""

    title_overrides = title_overrides or {}
    source_override = title_overrides.get(source_language)
    if source_override:
        source = page_info(client, source_language, source_override)
        source_candidates = [source_override]
        source_method = "explicit_title"
        if source["missing"] or source["is_disambiguation"]:
            raise ApiError(
                f"Explicit source title {source_override!r} in {source_language} "
                "is missing or a disambiguation page."
            )
    else:
        source, source_candidates = choose_search_result(client, source_language, topic)
        source_method = "source_search"

    links = language_links(client, source_language, source["title"])
    resolved: list[dict[str, Any]] = []
    warnings: list[str] = []
    source_qid = source.get("wikidata_id")

    for language in languages:
        override = title_overrides.get(language)
        candidates: list[str] = []
        if override:
            info = page_info(client, language, override)
            candidates = [override]
            method = "explicit_title"
            target_qid = info.get("wikidata_id")
            if language == source_language:
                confidence = "high"
                semantic_match = "source_article"
            elif source_qid and target_qid and source_qid == target_qid:
                confidence = "high"
                semantic_match = "wikidata_qid_match"
            elif source_qid and target_qid and source_qid != target_qid:
                confidence = "low"
                semantic_match = "wikidata_qid_mismatch"
                warnings.append(
                    f"{language}: explicit title {info.get('title', override)!r} has "
                    f"Wikidata item {target_qid}, which differs from source item "
                    f"{source_qid}. Verify that the mapping represents the same concept."
                )
            else:
                confidence = "medium"
                semantic_match = "qid_unavailable"
                warnings.append(
                    f"{language}: the explicit title could not be matched to the source "
                    "with Wikidata IDs. Verify that it represents the same concept."
                )
        elif language == source_language:
            info = dict(source)
            candidates = list(source_candidates)
            method = source_method
            confidence = "high" if source_override else "medium"
            semantic_match = "source_article"
        elif language in links:
            linked_title = links[language]
            info = page_info(client, language, linked_title)
            candidates = [linked_title]
            method = "interlanguage_link"
            confidence = "high"
            semantic_match = "interlanguage_link"
        elif allow_search_fallback:
            info, candidates = choose_search_result(client, language, topic)
            method = "local_search_fallback"
            confidence = "low"
            semantic_match = "unverified_search"
            warnings.append(
                f"{language}: no interlanguage link was available; a local search "
                "fallback was used. Verify the article meaning or pass --title."
            )
        else:
            raise ApiError(
                f"No interlanguage link from {source_language}:{source['title']} to "
                f"{language} Wikipedia. This is not evidence of zero interest. "
                f"Provide --title {language}=ARTICLE if an equivalent page exists, "
                "or pass --allow-search-fallback and verify the result manually."
            )

        if info["missing"]:
            raise ApiError(
                f"Resolved title {info.get('requested_title')!r} is missing in "
                f"{language} Wikipedia. Provide --title {language}=ARTICLE."
            )
        if info["is_disambiguation"]:
            raise ApiError(
                f"Resolved title {info['title']!r} is a disambiguation page in "
                f"{language} Wikipedia. Provide a specific --title mapping."
            )
        info.update(
            {
                "resolution_method": method,
                "resolution_confidence": confidence,
                "semantic_match": semantic_match,
                "candidate_titles": candidates,
            }
        )
        resolved.append(info)

    return {
        "topic": topic,
        "source_language": source_language,
        "source_article": source,
        "articles": resolved,
        "warnings": warnings,
    }
