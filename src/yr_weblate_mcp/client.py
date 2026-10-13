"""Thin Weblate REST API client (https://docs.weblate.org/en/latest/api.html)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import unquote, urlparse

import httpx


class WeblateError(Exception):
    """An error the model should read (bad input, refused action, API error)."""


def quote(value: str) -> str:
    """Quote a value for the Weblate search language."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _path_parts(url: str, kind: str) -> list[str]:
    # A component nested in a category is addressed with a double encoded slash.
    parts = unquote(unquote(urlparse(url).path)).strip("/").split("/")
    return parts[parts.index(kind) + 1 :]


def parse_translation_url(url: str) -> tuple[str, str, str]:
    """Return (project, component, language) from a translation API url."""
    parts = _path_parts(url, "translations")
    return parts[0], "/".join(parts[1:-1]), parts[-1]


def parse_component_url(url: str) -> tuple[str, str]:
    """Return (project, component) from a component API url."""
    parts = _path_parts(url, "components")
    return parts[0], "/".join(parts[1:])


class _TTLCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any, compute: Callable[[], Any]) -> Any:
        with self._lock:
            hit = self._data.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        value = compute()
        with self._lock:
            self._data[key] = (time.monotonic(), value)
        return value

    def drop(self, key: Any) -> None:
        with self._lock:
            self._data.pop(key, None)


class WeblateClient:
    def __init__(
        self,
        url: str,
        token: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._http = httpx.Client(
            base_url=f"{url}/api/",
            headers={"Authorization": f"Token {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )
        self._translations = _TTLCache(ttl=3600)
        self._components = _TTLCache(ttl=300)
        self._glossaries = _TTLCache(ttl=3600)

    # -- low level

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise WeblateError(f"{method} {path}: {exc}") from exc
        if response.is_error:
            raise WeblateError(
                f"{method} {path}: HTTP {response.status_code} {response.text[:500]}"
            )
        return response.json()

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def iterate(
        self, path: str, params: dict[str, Any] | None = None, limit: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """Iterate over a paginated list endpoint."""
        data = self.get(path, **(params or {}))
        count = 0
        while True:
            for item in data["results"]:
                yield item
                count += 1
                if limit is not None and count >= limit:
                    return
            if not data.get("next"):
                return
            data = self.request("GET", data["next"])

    # -- units

    def search_units(self, query: str, limit: int) -> tuple[int, list[dict[str, Any]]]:
        """Search units instance wide. Return (total count, first `limit` units)."""
        page_size = min(max(limit, 1), 1000)
        first = self.get("units/", q=query, page_size=page_size)
        units = list(first["results"][:limit])
        next_url = first.get("next")
        while next_url and len(units) < limit:
            page = self.request("GET", next_url)
            units.extend(page["results"][: limit - len(units)])
            next_url = page.get("next")
        return first["count"], units

    def unit(self, unit_id: int) -> dict[str, Any]:
        return self.get(f"units/{unit_id}/")

    def patch_unit(self, unit_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"units/{unit_id}/", json=data)

    def add_unit(
        self, project: str, component: str, language: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        component = component.replace("/", "%252F")
        return self.request(
            "POST", f"translations/{project}/{component}/{language}/units/", json=data
        )

    def translation_units(
        self, project: str, component: str, language: str
    ) -> list[dict[str, Any]]:
        component = component.replace("/", "%252F")
        return list(
            self.iterate(
                f"translations/{project}/{component}/{language}/units/",
                {"page_size": 1000},
            )
        )

    # -- cached metadata

    def translation(self, url: str) -> dict[str, Any]:
        return self._translations.get(url, lambda: self.request("GET", url))

    def project_components(self, project: str) -> dict[str, dict[str, Any]]:
        """Components of a project, keyed by component path."""

        def compute() -> dict[str, dict[str, Any]]:
            out = {}
            for component in self.iterate(
                f"projects/{project}/components/", {"page_size": 1000}
            ):
                out[parse_component_url(component["url"])[1]] = component
            return out

        return self._components.get(project, compute)

    def glossaries_by_project(self) -> dict[str, list[tuple[str, str]]]:
        """Map each project to the glossary components it uses (own and shared)."""

        def compute() -> dict[str, list[tuple[str, str]]]:
            out: dict[str, list[tuple[str, str]]] = {}
            for component in self.iterate("components/", {"page_size": 1000}):
                if not component.get("is_glossary"):
                    continue
                key = parse_component_url(component["url"])
                out.setdefault(key[0], []).append(key)
                links_url = component.get("links_url") or f"{component['url']}links/"
                for project in self.iterate(links_url):
                    out.setdefault(project["slug"], []).append(key)
            return out

        return self._glossaries.get("all", compute)
