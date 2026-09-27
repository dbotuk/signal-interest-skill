"""Small, cache-aware JSON client for Wikimedia APIs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import random
import tempfile
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    """Raised when a Wikimedia request cannot be completed safely."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SourceRecord:
    url: str
    retrieved_at: str
    from_cache: bool
    status: int


class JsonClient:
    """Fetch JSON sequentially, with bounded retries and an on-disk cache."""

    def __init__(
        self,
        cache_dir: Path,
        user_agent: str,
        *,
        timeout: float = 30.0,
        retries: int = 4,
        refresh: bool = False,
        min_interval: float = 0.35,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("A descriptive Wikimedia User-Agent is required")
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.refresh = refresh
        self.min_interval = min_interval
        self._last_request_at = 0.0
        self._sources: list[SourceRecord] = []

    @property
    def sources(self) -> list[dict[str, Any]]:
        seen: set[tuple[str, bool]] = set()
        records: list[dict[str, Any]] = []
        for source in self._sources:
            key = (source.url, source.from_cache)
            if key in seen:
                continue
            seen.add(key)
            records.append(asdict(source))
        return records

    def get_json(
        self,
        base_url: str,
        params: dict[str, Any] | Iterable[tuple[str, Any]] | None = None,
    ) -> dict[str, Any]:
        url = self._build_url(base_url, params)
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cache_path = self.cache_dir / f"{digest}.json"

        if cache_path.exists() and not self.refresh:
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("cached root is not an object")
                retrieved = datetime.fromtimestamp(
                    cache_path.stat().st_mtime, tz=UTC
                ).isoformat()
                self._sources.append(SourceRecord(url, retrieved, True, 200))
                return payload
            except (OSError, ValueError, json.JSONDecodeError):
                # A corrupt cache entry is ignored and replaced by a fresh response.
                pass

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._respect_rate_limit()
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": self.user_agent,
                },
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    status = int(getattr(response, "status", 200))
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ApiError(f"Expected a JSON object from {url}")
                api_error = payload.get("error")
                if isinstance(api_error, dict):
                    code = str(api_error.get("code", "unknown"))
                    message = str(api_error.get("info", api_error.get("message", code)))
                    if code == "maxlag" and attempt < self.retries:
                        time.sleep(self._retry_delay(attempt, None))
                        continue
                    raise ApiError(f"Wikimedia API error {code}: {message}")
                self._write_cache(cache_path, payload)
                now = datetime.now(UTC).isoformat()
                self._sources.append(SourceRecord(url, now, False, status))
                return payload
            except HTTPError as error:
                last_error = error
                retryable = error.code == 429 or 500 <= error.code < 600
                if not retryable or attempt >= self.retries:
                    detail = self._http_error_detail(error)
                    raise ApiError(
                        f"Wikimedia request failed with HTTP {error.code}: {detail}",
                        status_code=error.code,
                    ) from error
                retry_after = (
                    error.headers.get("Retry-After") if error.headers else None
                )
                delay = self._retry_delay(attempt, retry_after)
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
                last_error = error
                if attempt >= self.retries:
                    raise ApiError(f"Wikimedia request failed: {error}") from error
                delay = self._retry_delay(attempt, None)
            time.sleep(delay)

        raise ApiError(f"Wikimedia request failed: {last_error}")

    @staticmethod
    def _build_url(
        base_url: str,
        params: dict[str, Any] | Iterable[tuple[str, Any]] | None,
    ) -> str:
        if not params:
            return base_url
        query = urlencode(params, doseq=True)
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}{query}"

    def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(60.0, max(0.0, float(retry_after)))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    seconds = (retry_at - datetime.now(UTC)).total_seconds()
                    return min(60.0, max(0.0, seconds))
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(20.0, (2**attempt) + random.random() * 0.25)

    @staticmethod
    def _http_error_detail(error: HTTPError) -> str:
        try:
            body = error.read(800).decode("utf-8", errors="replace").strip()
            return body or error.reason
        except Exception:
            return str(error.reason)

    @staticmethod
    def _write_cache(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        tmp_name = handle.name
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass
