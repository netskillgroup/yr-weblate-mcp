"""Server settings, read from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase


def _list(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    url: str
    """Weblate root url, eg. https://hosted.weblate.org (without /api)."""
    token: str
    projects: tuple[str, ...] = ("*",)
    """Project slugs (fnmatch patterns) the server may read and write."""
    languages: tuple[str, ...] = ()
    """Language codes the server may write. Empty means any target language."""
    glossaries: tuple[str, ...] = ()
    """Glossary components ("project/component"). Empty means auto-discovery."""
    write_enabled: bool = False
    """Expose the tools that change Weblate (translate_unit, glossary_add_term)."""
    timeout: float = 30.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        url = env.get("WEBLATE_URL", "").strip().rstrip("/")
        token = env.get("WEBLATE_TOKEN", "").strip()
        if not url or not token:
            raise ValueError("WEBLATE_URL and WEBLATE_TOKEN must be set")
        if url.endswith("/api"):
            url = url[: -len("/api")]
        return cls(
            url=url,
            token=token,
            projects=_list(env.get("WEBLATE_PROJECTS")) or ("*",),
            languages=_list(env.get("WEBLATE_LANGUAGES")),
            glossaries=_list(env.get("WEBLATE_GLOSSARIES")),
            write_enabled=_bool(env.get("WEBLATE_WRITE_ENABLED")),
            timeout=float(env.get("WEBLATE_TIMEOUT") or 30),
        )

    def project_allowed(self, slug: str) -> bool:
        return any(fnmatchcase(slug, pattern) for pattern in self.projects)

    def language_allowed(self, code: str) -> bool:
        return not self.languages or code in self.languages
