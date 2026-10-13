# yr-weblate-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent **fix translations in
[Weblate](https://weblate.org) safely**: find the string from its source code reference, see
everything a change would propagate to, follow the glossary, then translate.

It is built for setups where translation propagation is on (one project, many components with
shared strings — e.g. one component per Odoo module and per branch), where saving one string can
silently change dozens of others.

## Tools

| Tool | Kind | What it does |
|------|------|--------------|
| `find_units` | read | Search units by `location` (source code reference), exact or partial source, target, project, component, language, or a raw Weblate query. |
| `get_unit` | read | One unit, and whether its component accepts propagated translations. |
| `propagation_impact` | read | Every other unit a save would also change, the units left untouched (and why), the translations that already exist for that source, homonym warnings, and an `impact_token`. |
| `glossary_lookup` | read | Glossary terms contained in a text (own and shared glossaries of the project). |
| `translate_unit` | write | Save a translation. Requires the `impact_token` of `propagation_impact`. |
| `glossary_add_term` | write | Add a term to the glossary (never changes an existing translated term). |

Write tools are only exposed when `WEBLATE_WRITE_ENABLED=true`.

### Safety rules enforced by the server

- `translate_unit` is refused when the propagation impact changed since it was reviewed
  (the `impact_token` no longer matches).
- Placeholders (`%s`, `%(name)s`, `{name}`) and markup tags of the source must be kept.
- Source language units, read-only units, projects outside `WEBLATE_PROJECTS` and languages
  outside `WEBLATE_LANGUAGES` are refused.
- No tool changes component settings (propagation, addons...).

### How propagation is computed

Like Weblate itself (`Unit.propagated_units`): a saved translation is copied to every unit of
the **same project and language** with the **same source and context**, in every component whose
*Allow translation propagation* setting is on. That setting only controls whether a component
**receives** propagated translations: turning it off on the component you edit does not stop the
edit from spreading. To translate one occurrence in isolation, turn propagation off on the
components that would receive it.

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `WEBLATE_URL` | yes | Weblate root url, e.g. `https://hosted.weblate.org` |
| `WEBLATE_TOKEN` | yes | API token of the account used by the agent |
| `WEBLATE_PROJECTS` | no | Comma separated project slugs, `fnmatch` patterns allowed (`main,client-*`). Default: all |
| `WEBLATE_LANGUAGES` | no | Comma separated language codes the agent may write (`fr,nl`). Default: all |
| `WEBLATE_GLOSSARIES` | no | `project/component` of the glossaries to use. Default: auto-discovery (own + shared glossaries) |
| `WEBLATE_WRITE_ENABLED` | no | `true` to expose the write tools. Default: `false` |
| `WEBLATE_TIMEOUT` | no | HTTP timeout in seconds. Default: `30` |

### Weblate account

Give the agent its own Weblate user, so its edits are identified in the history, in a group
limited to:

- **Translate** (edit strings) and **Manage glossary**, on the allowed projects;
- nothing in *Administration* / *Component settings*.

The Weblate permissions are the real guardrail: even a misbehaving agent cannot change
settings it has no rights on.

## Run

```bash
# stdio (default), from git
WEBLATE_URL=... WEBLATE_TOKEN=... uvx --from git+https://github.com/netskillgroup/yr-weblate-mcp yr-weblate-mcp

# Streamable HTTP; put it behind an authenticating reverse proxy, the token can write
yr-weblate-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

### Hermes Agent

`~/.hermes/config.yaml`:

```yaml
mcp_servers:
  weblate:
    command: uvx
    args: ["--from", "git+https://github.com/netskillgroup/yr-weblate-mcp", "yr-weblate-mcp"]
    env:
      WEBLATE_URL: "https://translations.example.com"
      WEBLATE_TOKEN: "${WEBLATE_TOKEN}"        # from ~/.hermes/.env
      WEBLATE_PROJECTS: "main,client-*"
      WEBLATE_LANGUAGES: "fr,nl"
      WEBLATE_WRITE_ENABLED: "false"          # start read-only
```

Tools are then available as `mcp__weblate__find_units`, etc.

### Claude Code

```bash
claude mcp add weblate --env WEBLATE_URL=... --env WEBLATE_TOKEN=... \
  -- uvx --from git+https://github.com/netskillgroup/yr-weblate-mcp yr-weblate-mcp
```

## Development

```bash
uv sync
uv run pytest
```

The tests run against an in-memory fake of the Weblate API (`tests/fake_weblate.py`) that
reproduces Weblate's propagation rules.
