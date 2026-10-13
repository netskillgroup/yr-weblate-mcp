"""In-memory Weblate API, just enough for the tests (propagation included)."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

import httpx

BASE = "https://weblate.test"
API = f"{BASE}/api/"
_TOKEN = re.compile(r'(\w+):(=?)("(?:[^"\\]|\\.)*"|\S+)')


def _unquote(value: str) -> str:
    if value.startswith('"'):
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value


class FakeWeblate:
    def __init__(self):
        self.units: dict[int, dict] = {}
        # (project, component) -> {"allow_translation_propagation": bool, "is_glossary": bool}
        self.components: dict[tuple[str, str], dict] = {}
        self.glossary_links: dict[tuple[str, str], list[str]] = {}
        self.source_language = "en"
        self.requests: list[httpx.Request] = []
        self._next_id = 1

    # -- setup

    def component(self, project, slug, propagation=True, glossary=False):
        self.components[(project, slug)] = {
            "allow_translation_propagation": propagation,
            "is_glossary": glossary,
        }

    def unit(self, project, component, language, source, target="", state=20, location="", context=""):
        unit_id = self._next_id
        self._next_id += 1
        self.units[unit_id] = {
            "id": unit_id,
            "translation": f"{API}translations/{project}/{component.replace('/', '%252F')}/{language}/",
            "source": [source],
            "target": [target],
            "state": state if target else 0,
            "context": context,
            "location": location,
            "note": "",
            "explanation": "",
            "flags": "",
            "extra_flags": "",
            "has_failing_check": False,
            "web_url": f"{BASE}/translate/{project}/{component}/{language}/?checksum={unit_id}",
            "_key": (project, component, language),
        }
        return unit_id

    # -- transport

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _public(self, unit):
        return {k: v for k, v in unit.items() if not k.startswith("_")}

    def _page(self, items):
        return httpx.Response(200, json={"count": len(items), "next": None, "results": items})

    def _match(self, unit, query):
        project, component, language = unit["_key"]
        for field, exact, raw in _TOKEN.findall(query):
            value = _unquote(raw)
            if field == "project" and project != value:
                return False
            if field == "component" and component != value:
                return False
            if field == "language" and language != value:
                return False
            if field == "source":
                if exact and unit["source"][0] != value:
                    return False
                if not exact and value.lower() not in unit["source"][0].lower():
                    return False
            if field == "target" and value.lower() not in unit["target"][0].lower():
                return False
            if field == "location" and value not in unit["location"]:
                return False
        return True

    def _translate(self, unit, target, state):
        unit["target"], unit["state"] = target, state
        project = unit["_key"][0]
        for other in self.units.values():
            if (
                other is not unit
                and other["_key"][0] == project
                and other["_key"][2] == unit["_key"][2]
                and other["source"] == unit["source"]
                and other["context"] == unit["context"]
                and self.components[(project, other["_key"][1])]["allow_translation_propagation"]
                and other["state"] != 100
            ):
                other["target"], other["state"] = target, state

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = urlparse(str(request.url)).path.removeprefix("/api/")
        params = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        parts = [p for p in path.split("/") if p]
        method = request.method

        if parts == ["units"]:
            found = [self._public(u) for u in self.units.values() if self._match(u, params.get("q", ""))]
            return self._page(found)
        if parts[0] == "units":
            unit = self.units.get(int(parts[1]))
            if unit is None:
                return httpx.Response(404, json={"detail": "Not found"})
            if method == "PATCH":
                data = json.loads(request.content)
                if "explanation" in data:
                    unit["explanation"] = data["explanation"]
                if "target" in data:
                    self._translate(unit, data["target"], data.get("state", 20))
            return httpx.Response(200, json=self._public(unit))
        if parts[0] == "translations":
            project, component, language = parts[1], "/".join(parts[2:-1] if parts[-1] != "units" else parts[2:-2]), (
                parts[-1] if parts[-1] != "units" else parts[-2]
            )
            component = component.replace("%2F", "/")
            if parts[-1] != "units":
                return httpx.Response(200, json={"is_source": language == self.source_language})
            if method == "POST":
                data = json.loads(request.content)
                unit_id = self.unit(project, component, language, data["source"][0], data["target"][0])
                return httpx.Response(200, json=self._public(self.units[unit_id]))
            found = [self._public(u) for u in self.units.values() if u["_key"] == (project, component, language)]
            return self._page(found)
        if parts[0] == "projects" and parts[2] == "components":
            found = [
                {"url": f"{API}components/{p}/{c}/", "slug": c, **info}
                for (p, c), info in self.components.items()
                if p == parts[1]
            ]
            return self._page(found)
        if parts == ["components"]:
            return self._page(
                [
                    {"url": f"{API}components/{p}/{c}/", "links_url": f"{API}components/{p}/{c}/links/", **info}
                    for (p, c), info in self.components.items()
                ]
            )
        if parts[0] == "components" and parts[-1] == "links":
            return self._page([{"slug": s} for s in self.glossary_links.get((parts[1], parts[2]), [])])
        return httpx.Response(404, json={"detail": f"unhandled {method} {path}"})
