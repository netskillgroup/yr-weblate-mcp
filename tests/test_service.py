import asyncio

import pytest
from mcp import Client

from yr_weblate_mcp.client import WeblateClient, WeblateError, parse_translation_url, quote
from yr_weblate_mcp.config import Settings
from yr_weblate_mcp.server import build_server
from yr_weblate_mcp.service import WeblateService, placeholder_problems

from .fake_weblate import BASE, FakeWeblate


@pytest.fixture
def weblate():
    fake = FakeWeblate()
    for slug in ("sale-17_0", "sale-18_0", "stock-17_0"):
        fake.component("main", slug)
    fake.component("main", "crm-17_0", propagation=False)
    fake.component("main", "glossary", glossary=True)
    fake.component("client", "custom-17_0")
    fake.glossary_links[("main", "glossary")] = ["client"]
    fake.unit("main", "sale-17_0", "fr", "Open", "Ouvert", location="model:sale.selection__state__open")
    fake.unit("main", "sale-18_0", "fr", "Open", "Ouvert", location="model:sale.selection__state__open")
    fake.unit("main", "stock-17_0", "fr", "Open", "Ouvert", location="model_terms:ir.ui.view,arch_db:stock.view_form")
    fake.unit("main", "crm-17_0", "fr", "Open", "Ouvrir", location="model_terms:ir.ui.view,arch_db:crm.view_form")
    fake.unit("main", "sale-17_0", "nl", "Open", "Open")
    fake.unit("main", "sale-17_0", "en", "Open", "Open")
    fake.unit("main", "sale-17_0", "fr", "Lease %(name)s", "Bail %(name)s")
    fake.unit("client", "custom-17_0", "fr", "Open", "Ouvert")
    fake.unit("main", "glossary", "fr", "Lease", "Bail")
    fake.unit("main", "glossary", "fr", "Tenant", "")
    return fake


def make_service(fake, **overrides):
    settings = Settings(url=BASE, token="t", write_enabled=True, **overrides)
    client = WeblateClient(BASE, "t", transport=fake.transport)
    return WeblateService(settings, client)


def unit_id(fake, component, language="fr", source="Open", project="main"):
    return next(
        u["id"] for u in fake.units.values()
        if u["_key"] == (project, component, language) and u["source"] == [source]
    )


def test_quote_escapes_quotes_and_backslashes():
    assert quote('say "hi" \\o/') == '"say \\"hi\\" \\\\o/"'


def test_parse_translation_url_with_category():
    url = "https://w/api/translations/proj/cat%252Fcomp/fr_BE/"
    assert parse_translation_url(url) == ("proj", "cat/comp", "fr_BE")


@pytest.mark.parametrize(
    "source,target,ok",
    [
        ("Hello %s", "Bonjour %s", True),
        ("Hello %s", "Bonjour", False),
        ("%(count)s items", "%(count)s éléments", True),
        ("%(count)s items", "%(nb)s éléments", False),
        ("<b>Due</b> date", "Date <b>d'échéance</b>", True),
        ("<b>Due</b> date", "Date d'échéance", False),
        ("Ratio 100% sure", "Ratio sûr à 100% près", True),
    ],
)
def test_placeholder_problems(source, target, ok):
    assert (placeholder_problems([source], [target]) == []) is ok


def test_find_units_by_location(weblate):
    service = make_service(weblate)
    result = service.find_units(project="main", language="fr", location="arch_db:crm.view_form")
    assert [u["component"] for u in result["units"]] == ["crm-17_0"]
    assert result["units"][0]["locations"] == ["model_terms:ir.ui.view,arch_db:crm.view_form"]


def test_find_units_hides_projects_not_allowed(weblate):
    service = make_service(weblate, projects=("main",))
    result = service.find_units(source_exact="Open", language="fr")
    assert {u["project"] for u in result["units"]} == {"main"}
    assert "hidden" in result
    with pytest.raises(WeblateError):
        service.find_units(project="client", source_exact="Open")


def test_propagation_impact(weblate):
    service = make_service(weblate)
    impact = service.propagation_impact(unit_id(weblate, "sale-17_0"), "Ouvrir")
    assert sorted(u["component"] for u in impact["will_also_change"]) == ["sale-18_0", "stock-17_0"]
    # receive-side flag: crm has propagation off, other project and languages are ignored
    assert [(u["component"], u["reason"]) for u in impact["not_changed"]] == [
        ("crm-17_0", "propagation disabled on its component")
    ]
    assert {t["target"] for t in impact["existing_translations"]} == {"Ouvert", "Ouvrir"}
    assert any("homonym" in w for w in impact["warnings"])


def test_translate_unit_requires_fresh_token(weblate):
    service = make_service(weblate)
    uid = unit_id(weblate, "stock-17_0")
    token = service.propagation_impact(uid)["impact_token"]
    weblate.units[unit_id(weblate, "sale-18_0")]["target"] = ["Ouvert(e)"]
    with pytest.raises(WeblateError, match="impact changed"):
        service.translate_unit(uid, "Ouvrir", token)
    assert weblate.units[uid]["target"] == ["Ouvert"]


def test_translate_unit_propagates_and_reports(weblate):
    service = make_service(weblate)
    uid = unit_id(weblate, "stock-17_0")
    token = service.propagation_impact(uid, "Ouvrir")["impact_token"]
    result = service.translate_unit(uid, "Ouvrir", token)
    assert result["unit"]["before"] == "Ouvert" and result["unit"]["target"] == "Ouvrir"
    assert sorted(u["component"] for u in result["propagated_to"]) == ["sale-17_0", "sale-18_0"]
    assert result["not_propagated"] == []
    # other project untouched
    assert weblate.units[unit_id(weblate, "custom-17_0", project="client")]["target"] == ["Ouvert"]


@pytest.mark.parametrize(
    "component,language,settings,error",
    [
        ("sale-17_0", "en", {}, "source language"),
        ("sale-17_0", "nl", {"languages": ("fr",)}, "not allowed"),
    ],
)
def test_translate_unit_refusals(weblate, component, language, settings, error):
    service = make_service(weblate, **settings)
    uid = unit_id(weblate, component, language)
    token = service.propagation_impact(uid)["impact_token"]
    with pytest.raises(WeblateError, match=error):
        service.translate_unit(uid, "Openen", token)


def test_translate_unit_refuses_broken_placeholders(weblate):
    service = make_service(weblate)
    uid = unit_id(weblate, "sale-17_0", source="Lease %(name)s")
    token = service.propagation_impact(uid)["impact_token"]
    with pytest.raises(WeblateError, match="placeholders differ"):
        service.translate_unit(uid, "Bail %s", token)


def test_glossary_lookup_uses_shared_glossary(weblate):
    service = make_service(weblate)
    result = service.glossary_lookup("Leases of the tenant", "fr", "client")
    assert result["glossaries"] == ["main/glossary"]
    assert {(t["term"], t["translation"]) for t in result["matches"]} == {("Lease", "Bail"), ("Tenant", None)}


def test_glossary_add_term(weblate):
    service = make_service(weblate)
    with pytest.raises(WeblateError, match="already in the glossary"):
        service.glossary_add_term("lease", "Contrat", "fr", "main")
    filled = service.glossary_add_term("Tenant", "Locataire", "fr", "main")
    assert filled["added"]["target"] == "Locataire"
    added = service.glossary_add_term("Landlord", "Bailleur", "fr", "main", explanation="Owner renting out")
    assert added["added"]["source"] == "Landlord"
    assert added["added"]["explanation"] == "Owner renting out"


def test_write_tools_hidden_unless_enabled(weblate):
    async def tool_names(write_enabled):
        settings = Settings(url=BASE, token="t", write_enabled=write_enabled)
        service = WeblateService(settings, WeblateClient(BASE, "t", transport=weblate.transport))
        async with Client(build_server(settings, service)) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    read = {"find_units", "get_unit", "propagation_impact", "glossary_lookup"}
    assert asyncio.run(tool_names(False)) == read
    assert asyncio.run(tool_names(True)) == read | {"translate_unit", "glossary_add_term"}
