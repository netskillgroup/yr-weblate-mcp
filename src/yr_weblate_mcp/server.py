"""MCP server exposing the Weblate translation workflow."""

from __future__ import annotations

import argparse
import functools
import logging
import sys
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import WeblateError
from .config import Settings
from .service import WeblateService

INSTRUCTIONS = """\
Tools to fix translations in Weblate.

Saving a translation PROPAGATES it: Weblate copies it to every unit of the same
project and language with the same source string and context, in every component
that allows propagation. Always run propagation_impact before translate_unit and
check that the translation is right for every unit it would also change.

Typical flow:
1. find_units: locate the unit, by `location` (source code reference) when known,
   else by `source_exact`, filtered on project, component and language.
2. glossary_lookup: get the glossary terms of the source text and use them.
3. propagation_impact: review `will_also_change`, `existing_translations`, `warnings`.
4. translate_unit: pass the `impact_token` returned by step 3, then check the result.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

UnitId = Annotated[int, Field(description="Weblate unit id (from find_units)")]
Target = Annotated[
    str | list[str],
    Field(description="Translation; a list with one entry per plural form for plural strings"),
]


def _tool_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except WeblateError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def build_server(settings: Settings, service: WeblateService | None = None) -> MCPServer:
    service = service or WeblateService(settings)
    mcp = MCPServer("weblate", instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ_ONLY)
    @_tool_errors
    def find_units(
        project: Annotated[str | None, Field(description="Project slug")] = None,
        component: Annotated[str | None, Field(description="Component slug")] = None,
        language: Annotated[str | None, Field(description="Language code, eg. fr")] = None,
        location: Annotated[
            str | None,
            Field(description="Source code reference (substring), eg. a `#:` reference of a .po/.pot file"),
        ] = None,
        source_exact: Annotated[str | None, Field(description="Exact source string (case sensitive)")] = None,
        source: Annotated[str | None, Field(description="Substring of the source string")] = None,
        target: Annotated[str | None, Field(description="Substring of the translation")] = None,
        query: Annotated[
            str | None,
            Field(description="Extra raw Weblate search query, eg. `state:<translated` or `note:\"module: x\"`"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Search translation units. Criteria are combined with AND.

        Each unit comes with its id, component, language, source, target, state,
        locations (source code references), note, explanation and web url.
        """
        return service.find_units(
            query=query, project=project, component=component, language=language,
            source=source, source_exact=source_exact, target=target, location=location,
            limit=limit,
        )

    @mcp.tool(annotations=READ_ONLY)
    @_tool_errors
    def get_unit(unit_id: UnitId) -> dict[str, Any]:
        """Get one unit, with whether its component accepts propagated translations."""
        return service.get_unit(unit_id)

    @mcp.tool(annotations=READ_ONLY)
    @_tool_errors
    def propagation_impact(
        unit_id: UnitId,
        new_target: Annotated[
            str | list[str] | None,
            Field(description="The translation you intend to save (optional)"),
        ] = None,
    ) -> dict[str, Any]:
        """List every other unit a translation of this unit would also change.

        `will_also_change`: units that would receive the new translation.
        `not_changed`: same source, but not touched (propagation off, read-only...).
        `existing_translations`: current translations of this source across the
        project; more than one usually means the source has several meanings.
        `impact_token`: pass it to translate_unit.
        """
        return service.propagation_impact(unit_id, new_target)

    @mcp.tool(annotations=READ_ONLY)
    @_tool_errors
    def glossary_lookup(
        text: Annotated[str, Field(description="Source text to look up, usually a unit source")],
        language: Annotated[str, Field(description="Target language code")],
        project: Annotated[str, Field(description="Project slug; its own and shared glossaries are used")],
    ) -> dict[str, Any]:
        """Glossary terms contained in a text, with their translation and explanation."""
        return service.glossary_lookup(text, language, project)

    if settings.write_enabled:

        @mcp.tool(annotations=WRITE)
        @_tool_errors
        def translate_unit(
            unit_id: UnitId,
            target: Target,
            impact_token: Annotated[str, Field(description="impact_token from propagation_impact")],
        ) -> dict[str, Any]:
            """Save a translation (state: translated). It propagates like in Weblate.

            Refused when the impact changed since propagation_impact, when the
            placeholders or markup of the source are not kept, for read-only or
            source language units, and for languages or projects not allowed.
            Returns the saved unit, the units it propagated to, and failing checks.
            """
            return service.translate_unit(unit_id, target, impact_token)

        @mcp.tool(annotations=WRITE)
        @_tool_errors
        def glossary_add_term(
            term: Annotated[str, Field(description="Term in the source language")],
            translation: Annotated[str, Field(description="Translation of the term")],
            language: Annotated[str, Field(description="Target language code")],
            project: Annotated[str, Field(description="Project slug the glossary is used by")],
            explanation: Annotated[str | None, Field(description="Short definition / usage note")] = None,
            glossary: Annotated[
                str | None,
                Field(description="project/component of the glossary, when the project has several"),
            ] = None,
        ) -> dict[str, Any]:
            """Add a term to a glossary. Existing translated terms are never changed."""
            return service.glossary_add_term(term, translation, language, project, explanation, glossary)

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    # one log line per API call is noise for an MCP client
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        sys.exit(f"yr-weblate-mcp: {exc}")
    server = build_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
