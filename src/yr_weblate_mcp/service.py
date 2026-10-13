"""Translation workflow on top of the Weblate API.

Propagation follows Weblate (weblate/trans/models/unit.py, `Unit.propagated_units`):
saving a unit copies its target to every unit of the same project and language
with the same source and context, when the *receiving* component allows
translation propagation. The flag of the edited component plays no role.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from .client import WeblateClient, WeblateError, parse_translation_url, quote
from .config import Settings

STATE_EMPTY = 0
STATE_TRANSLATED = 20
STATE_READONLY = 100
STATE_LABELS = {
    0: "empty",
    10: "needs_editing",
    20: "translated",
    30: "approved",
    100: "read_only",
}

# printf (%s, %d, %(name)s) and brace ({name}, ${expr}) placeholders
_PLACEHOLDER = re.compile(
    r"%\([^)\s]+\)[#0+-]*\d*(?:\.\d+)?[sdifrx]|%[#0+-]*\d*(?:\.\d+)?[sdifrx]|\$?\{[^{}\s]*\}"
)
_TAG = re.compile(r"</?([a-zA-Z][\w:.-]*)")


def _plural(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _text(value: list[str]) -> str | list[str]:
    return value[0] if len(value) == 1 else value


def placeholder_problems(source: list[str], target: list[str]) -> list[str]:
    """Placeholders and markup of the source that the target does not keep."""
    problems = []
    if len(source) == 1 and len(target) == 1:
        pairs = [(source[0], target[0])]
    else:
        # plural forms: every target form must only use placeholders of the source
        allowed = "".join(source)
        pairs = [(allowed, form) for form in target]
    for src, tgt in pairs:
        for name, pattern in (("placeholder", _PLACEHOLDER), ("tag", _TAG)):
            expected = Counter(pattern.findall(src))
            found = Counter(pattern.findall(tgt))
            if len(pairs) > 1:
                extra = set(found) - set(expected)
                if extra:
                    problems.append(f"unknown {name}(s) {sorted(extra)} in {tgt!r}")
            elif expected != found:
                problems.append(
                    f"{name}s differ: source has {sorted(expected.elements())}, "
                    f"target has {sorted(found.elements())}"
                )
    return problems


def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?:s|es)?(?!\w)", re.IGNORECASE)


class WeblateService:
    def __init__(self, settings: Settings, client: WeblateClient | None = None):
        self.settings = settings
        self.client = client or WeblateClient(settings.url, settings.token, settings.timeout)

    # -- helpers

    def _check_project(self, project: str) -> None:
        if not self.settings.project_allowed(project):
            raise WeblateError(
                f"Project {project!r} is not allowed (WEBLATE_PROJECTS={','.join(self.settings.projects)})"
            )

    def _check_language(self, language: str) -> None:
        if not self.settings.language_allowed(language):
            raise WeblateError(
                f"Language {language!r} is not allowed (WEBLATE_LANGUAGES={','.join(self.settings.languages)})"
            )

    def _propagation_enabled(self, project: str, component: str) -> bool:
        info = self.client.project_components(project).get(component)
        if info is None:
            raise WeblateError(f"Component {project}/{component} not found")
        return bool(info.get("allow_translation_propagation"))

    def _summary(self, unit: dict[str, Any]) -> dict[str, Any]:
        project, component, language = parse_translation_url(unit["translation"])
        flags = ", ".join(f for f in (unit.get("flags"), unit.get("extra_flags")) if f)
        return {
            "id": unit["id"],
            "project": project,
            "component": component,
            "language": language,
            "source": _text(unit["source"]),
            "target": _text(unit["target"]),
            "state": STATE_LABELS.get(unit["state"], unit["state"]),
            "context": unit.get("context") or "",
            "locations": [loc.strip() for loc in (unit.get("location") or "").split(", ") if loc.strip()],
            "note": unit.get("note") or "",
            "explanation": unit.get("explanation") or "",
            "flags": flags,
            "has_failing_check": unit.get("has_failing_check", False),
            "web_url": unit.get("web_url"),
        }

    def _load_unit(self, unit_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        unit = self.client.unit(unit_id)
        summary = self._summary(unit)
        self._check_project(summary["project"])
        return unit, summary

    def _same_source_units(self, unit: dict[str, Any]) -> list[dict[str, Any]]:
        """The other units Weblate considers the same string (see `Unit.objects.same`)."""
        project, _, language = parse_translation_url(unit["translation"])
        query = f"project:{project} language:{language} source:={quote(unit['source'][0])}"
        _, units = self.client.search_units(query, limit=1000)
        return [
            other
            for other in units
            if other["id"] != unit["id"]
            and other["source"] == unit["source"]
            and (other.get("context") or "") == (unit.get("context") or "")
        ]

    def _impact(self, unit: dict[str, Any]) -> tuple[list[tuple[dict, bool]], str]:
        """Same-source units with their propagation flag, and a token of that state."""
        others = []
        for other in self._same_source_units(unit):
            project, component, _ = parse_translation_url(other["translation"])
            others.append((other, self._propagation_enabled(project, component)))
        state = [[unit["id"], unit["target"], unit["state"]]] + sorted(
            [o["id"], o["target"], o["state"], enabled] for o, enabled in others
        )
        token = hashlib.sha256(json.dumps(state).encode()).hexdigest()[:16]
        return others, token

    # -- read tools

    def find_units(
        self,
        query: str | None = None,
        project: str | None = None,
        component: str | None = None,
        language: str | None = None,
        source: str | None = None,
        source_exact: str | None = None,
        target: str | None = None,
        location: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        parts = []
        if project:
            self._check_project(project)
            parts.append(f"project:{project}")
        if component:
            parts.append(f"component:{component}")
        if language:
            parts.append(f"language:{language}")
        if source:
            parts.append(f"source:{quote(source)}")
        if source_exact:
            parts.append(f"source:={quote(source_exact)}")
        if target:
            parts.append(f"target:{quote(target)}")
        if location:
            parts.append(f"location:{quote(location)}")
        if query:
            parts.append(query)
        if not parts:
            raise WeblateError("Give at least one search criterion")
        q = " ".join(parts)
        total, units = self.client.search_units(q, limit=max(1, min(limit, 100)))
        summaries = [self._summary(unit) for unit in units]
        allowed = [s for s in summaries if self.settings.project_allowed(s["project"])]
        result: dict[str, Any] = {"query": q, "total": total, "units": allowed}
        if len(allowed) < len(summaries):
            result["hidden"] = f"{len(summaries) - len(allowed)} unit(s) of not allowed projects"
        if total > len(units):
            result["truncated"] = f"showing {len(units)} of {total}, refine the search"
        return result

    def get_unit(self, unit_id: int) -> dict[str, Any]:
        unit, summary = self._load_unit(unit_id)
        translation = self.client.translation(unit["translation"])
        summary["is_source_language"] = bool(translation.get("is_source"))
        summary["component_propagation_enabled"] = self._propagation_enabled(
            summary["project"], summary["component"]
        )
        return summary

    def propagation_impact(
        self, unit_id: int, new_target: str | list[str] | None = None
    ) -> dict[str, Any]:
        unit, summary = self._load_unit(unit_id)
        others, token = self._impact(unit)
        new = _plural(new_target) if new_target is not None else None

        will_change, unchanged = [], []
        variants: dict[str, list[str]] = {}
        variants.setdefault(json.dumps(summary["target"], ensure_ascii=False), []).append(
            f"{summary['component']} (this unit)"
        )
        for other, enabled in others:
            item = self._summary(other)
            variants.setdefault(json.dumps(item["target"], ensure_ascii=False), []).append(
                item["component"]
            )
            if not enabled:
                unchanged.append({**item, "reason": "propagation disabled on its component"})
            elif other["state"] == STATE_READONLY:
                unchanged.append({**item, "reason": "read-only"})
            elif new is not None and other["target"] == new and other["state"] == STATE_TRANSLATED:
                unchanged.append({**item, "reason": "already has this translation"})
            else:
                will_change.append(item)

        warnings = []
        if len(variants) > 1:
            warnings.append(
                "The same source is already translated differently: it may have several "
                "meanings (homonym). Check every location before translating."
            )
        if len(summary["locations"]) > 1:
            warnings.append(
                f"This unit is used at {len(summary['locations'])} locations of its component; "
                "one translation applies to all of them and cannot be split in Weblate."
            )
        return {
            "unit": summary,
            "will_also_change": will_change,
            "not_changed": unchanged,
            "existing_translations": [
                {"target": json.loads(target), "components": components}
                for target, components in variants.items()
            ],
            "warnings": warnings,
            "impact_token": token,
        }

    def glossary_components(self, project: str) -> list[tuple[str, str]]:
        if self.settings.glossaries:
            return [tuple(g.split("/", 1)) for g in self.settings.glossaries]  # type: ignore[misc]
        return self.client.glossaries_by_project().get(project, [])

    def _glossary_terms(self, glossary: tuple[str, str], language: str) -> list[dict[str, Any]]:
        terms = []
        for unit in self.client.translation_units(glossary[0], glossary[1], language):
            source = unit["source"][0] if unit["source"] else ""
            if not source.strip():
                continue
            target = unit["target"][0] if unit["target"] and unit["state"] >= STATE_TRANSLATED else None
            terms.append(
                {
                    "id": unit["id"],
                    "term": source,
                    "translation": target,
                    "explanation": unit.get("explanation") or "",
                    "flags": ", ".join(f for f in (unit.get("flags"), unit.get("extra_flags")) if f),
                    "glossary": "/".join(glossary),
                }
            )
        return terms

    def glossary_lookup(self, text: str, language: str, project: str) -> dict[str, Any]:
        self._check_project(project)
        glossaries = self.glossary_components(project)
        if not glossaries:
            return {"glossaries": [], "matches": [], "note": f"No glossary is linked to {project}"}
        matches = []
        for glossary in glossaries:
            for term in self._glossary_terms(glossary, language):
                if _term_pattern(term["term"]).search(text):
                    matches.append(term)
        matches.sort(key=lambda t: len(t["term"]), reverse=True)
        return {"glossaries": ["/".join(g) for g in glossaries], "language": language, "matches": matches}

    # -- write tools

    def translate_unit(
        self, unit_id: int, target: str | list[str], impact_token: str
    ) -> dict[str, Any]:
        unit, summary = self._load_unit(unit_id)
        self._check_language(summary["language"])
        if self.client.translation(unit["translation"]).get("is_source"):
            raise WeblateError("This unit belongs to the source language; only translations can be edited")
        if unit["state"] == STATE_READONLY:
            raise WeblateError("This unit is read-only")
        new = _plural(target)
        if not any(form.strip() for form in new):
            raise WeblateError("The translation is empty")
        if new == unit["target"] and unit["state"] >= STATE_TRANSLATED:
            raise WeblateError("The unit already has this translation")
        problems = placeholder_problems(unit["source"], new)
        if problems:
            raise WeblateError("Translation refused, it does not keep the source markup: " + "; ".join(problems))

        others, token = self._impact(unit)
        if token != impact_token:
            raise WeblateError(
                "The propagation impact changed since it was checked. "
                "Run propagation_impact again and review it before translating."
            )
        expected = [
            other
            for other, enabled in others
            if enabled and other["state"] != STATE_READONLY
            and not (other["target"] == new and other["state"] == STATE_TRANSLATED)
        ]

        self.client.patch_unit(unit_id, {"target": new, "state": STATE_TRANSLATED})

        after = self._summary(self.client.unit(unit_id))
        propagated, not_propagated = [], []
        for other in expected:
            current = self._summary(self.client.unit(other["id"]))
            entry = {
                "id": current["id"],
                "component": current["component"],
                "locations": current["locations"],
                "before": _text(other["target"]),
                "after": current["target"],
                "has_failing_check": current["has_failing_check"],
            }
            (propagated if current["target"] == after["target"] else not_propagated).append(entry)
        failing = [after["id"]] if after["has_failing_check"] else []
        failing += [e["id"] for e in propagated if e["has_failing_check"]]
        return {
            "unit": {**after, "before": _text(unit["target"])},
            "propagated_to": propagated,
            "not_propagated": not_propagated,
            "units_with_failing_checks": failing,
        }

    def glossary_add_term(
        self,
        term: str,
        translation: str,
        language: str,
        project: str,
        explanation: str | None = None,
        glossary: str | None = None,
    ) -> dict[str, Any]:
        self._check_project(project)
        self._check_language(language)
        if not term.strip() or not translation.strip():
            raise WeblateError("Both the term and its translation are required")
        glossaries = self.glossary_components(project)
        if glossary:
            chosen = tuple(glossary.split("/", 1))
            if chosen not in glossaries:
                raise WeblateError(f"{glossary} is not a glossary of {project}: {['/'.join(g) for g in glossaries]}")
        elif len(glossaries) == 1:
            chosen = glossaries[0]
        else:
            raise WeblateError(
                f"Project {project} has {len(glossaries)} glossaries, pick one with `glossary`: "
                f"{['/'.join(g) for g in glossaries]}"
            )

        existing = next(
            (t for t in self._glossary_terms(chosen, language) if t["term"].lower() == term.strip().lower()),
            None,
        )
        if existing and existing["translation"]:
            raise WeblateError(
                f"{existing['term']!r} is already in the glossary as {existing['translation']!r} "
                "(existing glossary terms are not changed by this tool)"
            )
        if existing:
            unit = self.client.patch_unit(existing["id"], {"target": [translation], "state": STATE_TRANSLATED})
            unit_id = existing["id"]
        else:
            unit = self.client.add_unit(
                chosen[0], chosen[1], language,
                {"source": [term.strip()], "target": [translation], "state": STATE_TRANSLATED},
            )
            unit_id = unit["id"]
        if explanation:
            self.client.patch_unit(unit_id, {"explanation": explanation})
        return {"added": self._summary(self.client.unit(unit_id)), "glossary": "/".join(chosen)}
