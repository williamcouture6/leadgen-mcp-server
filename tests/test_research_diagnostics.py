"""Tests du diagnostic d'extraction de courriels (Research Agent, WF-3).

Contexte terrain (2026-08) : 145 companies sur 816 portent « recherche faite, aucun
courriel trouvé ». Les 145 ont un site, les 145 ont été scrapées, le scraper a rendu
zéro adresse à chaque fois. Vérification manuelle sur 12 d'entre elles : 10 publient
pourtant une adresse.

Le défaut de fond n'est PAS un filtre trop strict en particulier — c'est que rien ne
comptait les rejets. 145 zéros n'ont déclenché aucune alarme parce qu'aucun chiffre,
nulle part, ne disait « on a vu 47 adresses et on les a toutes jetées ». Ces tests
verrouillent ce compteur, et surtout le fait que l'instrumentation ne change AUCUNE
décision d'extraction.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from src.tools import research

# hex réel capturé sur famillelajoie.com/contactez-nous → info@fermehorticolelajoie.com
_CFEMAIL = "3f565159507f595a4d525a57504d4b565c50535a535e5550565a115c5052"


def _diag(url: str = "https://maboite.com/", statut: str = "http_200") -> dict[str, Any]:
    return research._diag_page_neuve(url, statut)


# --- compteurs au niveau adresse -------------------------------------------

def test_diag_compte_les_candidats_par_source() -> None:
    # Trois sources d'un coup : mailto DANS un <nav> (widget de menu), texte libre,
    # et obfuscation Cloudflare pointant sur un domaine-frère.
    html = (
        '<nav class="menu"><a href="mailto:info@famillelajoie.com">Écrivez-nous</a></nav>'
        "<p>Ou bien direction@famillelajoie.com pour la direction.</p>"
        f'<span class="__cf_email__" data-cfemail="{_CFEMAIL}">[email&#160;protected]</span>'
    )
    d = _diag("https://famillelajoie.com/")
    emails = research._extract_emails_from_html(html, "https://famillelajoie.com/", diag=d)

    # EMAIL_REGEX balaie le HTML BRUT : elle voit aussi l'adresse du href mailto.
    assert d["candidats"] == {"texte": 2, "mailto": 1, "cloudflare": 1, "total": 4}
    assert d["acceptes"] == 3
    assert {e["email"] for e in emails} == {
        "info@famillelajoie.com",
        "direction@famillelajoie.com",
        "info@fermehorticolelajoie.com",  # domaine-frère, décodé depuis le cfemail
    }
    assert d["rejets"] == {"local_bloque": 0, "domaine_bloque": 0, "hors_domaine": 0}


def test_diag_compte_le_rejet_local_bloque() -> None:
    html = '<a href="mailto:noreply@maboite.com">nous joindre</a>'
    d = _diag()
    assert research._extract_emails_from_html(html, "https://maboite.com/", diag=d) == []
    # 2 candidats (texte + mailto) mais UNE seule adresse rejetée : on compte des
    # adresses distinctes, pas des occurrences.
    assert d["candidats"]["total"] == 2
    assert d["rejets"]["local_bloque"] == 1


def test_diag_compte_le_rejet_domaine_bloque() -> None:
    html = "<p>Erreurs remontées à bug@sentry.io</p>"
    d = _diag()
    assert research._extract_emails_from_html(html, "https://maboite.com/", diag=d) == []
    assert d["rejets"]["domaine_bloque"] == 1


def test_diag_enregistre_les_adresses_rejetees_hors_domaine() -> None:
    # Suspect n°1 : la règle de domaine. On veut les ADRESSES, pas juste un compte —
    # c'est la seule façon de savoir a posteriori si le filtre jetait de vrais
    # courriels de proprio (cas SET Jardin → unionmd.ca, un vrai tiers, bien rejeté).
    html = '<a href="mailto:mtlestinfo@unionmd.ca">écrivez-nous</a>'
    d = _diag("https://www.setjardin.ca/")
    assert research._extract_emails_from_html(html, "https://www.setjardin.ca/", diag=d) == []
    assert d["rejets"]["hors_domaine"] == 1
    assert d["rejets_hors_domaine"] == ["mtlestinfo@unionmd.ca"]


def test_diag_plafonne_la_liste_des_adresses_rejetees() -> None:
    # Le diagnostic atterrit dans un jsonb sur CHAQUE company : une page pathologique
    # (annuaire, footer géant) ne doit pas faire enfler la row.
    html = "".join(f'<a href="mailto:info{i}@zzq{i}.net">x</a>' for i in range(25))
    d = _diag("https://plomberiedupont.ca/")
    assert research._extract_emails_from_html(html, "https://plomberiedupont.ca/", diag=d) == []
    assert d["rejets"]["hors_domaine"] == 25
    assert len(d["rejets_hors_domaine"]) == research._DIAG_REJETS_MAX


def test_diag_domaine_etranger_reste_rejete_avec_instrumentation() -> None:
    # Le partenaire cité en prose sur un autre domaine reste jeté — le diagnostic
    # le VOIT, il ne le laisse pas passer.
    html = "<p>Réalisé avec partenaire@autreboite.net pour le design.</p>"
    d = _diag()
    assert research._extract_emails_from_html(html, "https://maboite.com/", diag=d) == []
    assert d["rejets"]["hors_domaine"] == 1


def test_diag_absent_ne_change_rien() -> None:
    html = '<a href="mailto:info@maboite.com">contact</a>'
    assert research._extract_emails_from_html(html, "https://maboite.com/") == [
        {"email": "info@maboite.com", "local": "info", "domain": "maboite.com", "kind": "generic"}
    ]


# --- garde-fou : l'instrumentation ne change PAS l'extraction ---------------

_CAS_EXTRACTION = [
    ('<a href="mailto:info@maboite.com">c</a>', "https://maboite.com/contact"),
    ("<p>Écrivez à jean.tremblay@gmail.com</p>", "https://maboite.com/"),
    ("<p>partenaire@autreboite.com fait le design.</p>", "https://maboite.com/"),
    ('<a href="mailto:mtlestinfo@unionmd.ca">x</a>', "https://www.setjardin.ca/"),
    (f'<span data-cfemail="{_CFEMAIL}">x</span>', "https://famillelajoie.com/"),
    ('<a href="mailto:noreply@maboite.com">x</a>', "https://maboite.com/"),
    ("<p>bug@sentry.io</p>", "https://maboite.com/"),
    ('<div id="root"></div>', "https://spa.ca/"),
    (
        '<nav><a href="mailto:info@maboite.com">a</a></nav><p>info@maboite.com '
        "et direction@maboite.com</p>",
        "https://maboite.com/",
    ),
]


@pytest.mark.parametrize("html,url", _CAS_EXTRACTION)
def test_extraction_identique_avec_ou_sans_diag(html: str, url: str) -> None:
    sans = research._extract_emails_from_html(html, url)
    avec = research._extract_emails_from_html(html, url, diag=_diag(url))
    assert sans == avec  # même contenu ET même ordre


# --- coquille SPA -----------------------------------------------------------

def test_coquille_vide_detecte_le_shell_spa() -> None:
    html = (
        "<html><head><title>Plomberie X</title><script src='/bundle.js'></script></head>"
        '<body><div id="root"></div></body></html>'
    )
    assert research._est_coquille_vide(html) is True


def test_coquille_vide_faux_si_un_lien_existe() -> None:
    assert research._est_coquille_vide('<body><a href="/contact">Contact</a></body>') is False


def test_coquille_vide_faux_si_une_nav_existe() -> None:
    assert research._est_coquille_vide("<body><nav><span>Menu</span></nav></body>") is False


# --- diagnostic au niveau de la passe (fetch_site) --------------------------

@respx.mock
async def test_fetch_site_diagnostic_de_passe() -> None:
    home = (
        '<nav><a href="/contact/">Contact</a></nav>'
        '<a href="/a-propos/">À propos</a><p>Ferme Lajoie</p>'
    )
    contact = (
        '<a href="/cdn-cgi/l/email-protection">'
        f'<span data-cfemail="{_CFEMAIL}">[email&#160;protected]</span></a>'
    )
    respx.get("https://famillelajoie.com/").mock(return_value=httpx.Response(200, html=home))
    respx.get("https://famillelajoie.com/contact/").mock(
        return_value=httpx.Response(200, html=contact)
    )
    respx.get("https://famillelajoie.com/a-propos/").mock(return_value=httpx.Response(404, html=""))

    site = await research.fetch_site("https://famillelajoie.com/")
    d = site[research.DIAGNOSTIC_KEY]

    assert d["version"] == 1
    assert d["statut_site"] == "http_200"
    assert [p["url"] for p in d["pages"]] == [
        "https://famillelajoie.com/",
        "https://famillelajoie.com/contact/",
    ]
    assert [p["statut"] for p in d["pages"]] == ["http_200", "http_200"]
    assert d["pages_en_echec"] == [
        {"url": "https://famillelajoie.com/a-propos/", "statut": "http_404"}
    ]
    assert d["totaux"]["pages_visitees"] == 3
    assert d["totaux"]["pages_en_echec"] == 1
    assert d["totaux"]["coquilles_vides"] == 0
    # l'adresse acceptée est tracée avec LA PAGE où on l'a vue (ici : /contact/)
    assert d["adresses_retenues"] == [
        {"email": "info@fermehorticolelajoie.com", "url": "https://famillelajoie.com/contact/"}
    ]
    assert d["totaux"]["acceptes"] == 1
    assert d["totaux"]["candidats"]["cloudflare"] == 1
    # l'extraction elle-même est inchangée
    assert [e["email"] for e in site["emails_found"]] == ["info@fermehorticolelajoie.com"]


@respx.mock
async def test_fetch_site_signale_la_coquille_spa() -> None:
    respx.get("https://spa.ca/").mock(
        return_value=httpx.Response(
            200, html='<div id="root"></div><script src="/app.js"></script>'
        )
    )
    site = await research.fetch_site("https://spa.ca/")
    d = site[research.DIAGNOSTIC_KEY]
    assert d["pages"][0]["coquille_vide"] is True
    assert d["totaux"]["coquilles_vides"] == 1
    assert d["totaux"]["candidats"]["total"] == 0
    assert site["emails_found"] == []


@respx.mock
async def test_fetch_site_home_injoignable() -> None:
    respx.get("https://mort.ca/").mock(side_effect=httpx.ConnectError("boom"))
    site = await research.fetch_site("https://mort.ca/")
    d = site[research.DIAGNOSTIC_KEY]
    assert d["statut_site"] == "error: ConnectError"
    assert d["pages"] == []
    assert d["pages_en_echec"] == [{"url": "https://mort.ca/", "statut": "error: ConnectError"}]
    assert d["totaux"]["pages_visitees"] == 1


@respx.mock
async def test_fetch_site_home_4xx_est_un_echec_compte() -> None:
    respx.get("https://ferme.ca/").mock(return_value=httpx.Response(403, html=""))
    site = await research.fetch_site("https://ferme.ca/")
    d = site[research.DIAGNOSTIC_KEY]
    assert d["statut_site"] == "http_403"
    assert d["pages_en_echec"] == [{"url": "https://ferme.ca/", "statut": "http_403"}]


@respx.mock
async def test_fetch_site_agrege_les_rejets_hors_domaine_de_toutes_les_pages() -> None:
    home = '<a href="/contact/">Contact</a><a href="mailto:un@tierstotal.net">x</a>'
    contact = '<a href="mailto:deux@autrechose.org">y</a>'
    respx.get("https://plomberiedupont.ca/").mock(return_value=httpx.Response(200, html=home))
    respx.get("https://plomberiedupont.ca/contact/").mock(
        return_value=httpx.Response(200, html=contact)
    )
    site = await research.fetch_site("https://plomberiedupont.ca/")
    d = site[research.DIAGNOSTIC_KEY]
    assert d["totaux"]["rejets"]["hors_domaine"] == 2
    assert set(d["rejets_hors_domaine"]) == {"un@tierstotal.net", "deux@autrechose.org"}
    assert site["emails_found"] == []


# --- persistance dans research_json -----------------------------------------

async def test_research_company_pose_le_diagnostic_dans_research_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _place(_pid: str) -> dict[str, Any]:
        return {"websiteUri": "https://maboite.com/"}

    async def _site(_url: str) -> dict[str, Any]:
        return {
            "status": "http_200",
            "pages": [],
            "tech_keyword_hits": [],
            "emails_found": [],
            research.DIAGNOSTIC_KEY: {"version": 1, "statut_site": "http_200"},
        }

    def _llm(*_a: Any, **_k: Any) -> research.LLMResult:
        return research.LLMResult(
            research_json={"company_summary": "Plomberie"},
            model="m",
            usage=research.LLMUsage(),
        )

    monkeypatch.setattr(research, "fetch_place_details", _place)
    monkeypatch.setattr(research, "fetch_site", _site)
    monkeypatch.setattr(research, "_call_llm", _llm)

    out = await research.research_company(research.ResearchCompanyIn(google_place_id="p1"))
    assert out.research_json["company_summary"] == "Plomberie"
    assert out.research_json[research.DIAGNOSTIC_KEY]["statut_site"] == "http_200"


async def test_research_company_sans_site_pose_quand_meme_un_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _place(_pid: str) -> dict[str, Any]:
        return {}  # aucun websiteUri

    def _llm(*_a: Any, **_k: Any) -> research.LLMResult:
        return research.LLMResult(research_json={}, model="m", usage=research.LLMUsage())

    monkeypatch.setattr(research, "fetch_place_details", _place)
    monkeypatch.setattr(research, "_call_llm", _llm)

    out = await research.research_company(research.ResearchCompanyIn(google_place_id="p1"))
    assert out.research_json[research.DIAGNOSTIC_KEY]["statut_site"] == "no_website"
    assert out.research_json[research.DIAGNOSTIC_KEY]["totaux"]["pages_visitees"] == 0


# --- le diagnostic ne fuit pas dans les prompts LLM aval --------------------

def test_sans_diagnostic_retire_la_cle_sans_muter_l_original() -> None:
    rj = {"company_summary": "x", research.DIAGNOSTIC_KEY: {"version": 1}}
    propre = research.sans_diagnostic(rj)
    assert propre == {"company_summary": "x"}
    assert research.DIAGNOSTIC_KEY in rj  # l'original n'est pas touché


def test_sans_diagnostic_tolere_none_et_absence() -> None:
    assert research.sans_diagnostic(None) == {}
    assert research.sans_diagnostic({"a": 1}) == {"a": 1}


_RJ_AVEC_DIAG = {
    "company_summary": "Plomberie Dupont, 6 camions",
    research.DIAGNOSTIC_KEY: {
        "version": 1,
        "rejets_hors_domaine": ["fuite@tiers-a-ne-pas-voir.net"],
    },
}


def test_personalize_ne_met_pas_le_diagnostic_dans_le_prompt() -> None:
    # `research_json` est dumpé tel quel dans le prompt de personnalisation :
    # sans nettoyage, poser le diagnostic changerait l'input du LLM pour CHAQUE
    # company (et lui donnerait des adresses tierces à recopier).
    from src.tools import personalize

    msg = personalize._format_input_for_llm(
        research=_RJ_AVEC_DIAG,
        company={"name": "Plomberie Dupont", "website": "https://plomberiedupont.ca"},
        contact=None,
        social_proof=[],
        template_choice="A",
        slots_block="(aucun)",
    )
    assert "Plomberie Dupont, 6 camions" in msg
    assert research.DIAGNOSTIC_KEY not in msg
    assert "fuite@tiers-a-ne-pas-voir.net" not in msg


class _Bloc:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Msgs:
    """Faux `client.messages` : capture les kwargs de l'appel Anthropic."""

    def __init__(self, reponse: str) -> None:
        self.kwargs: dict[str, Any] = {}
        self._reponse = reponse

    def create(self, **kw: Any) -> Any:
        self.kwargs = kw
        return type(
            "R",
            (),
            {"content": [_Bloc(self._reponse)], "usage": type("U", (), {})()},
        )()


def _patch_anthropic(monkeypatch: pytest.MonkeyPatch, module: Any, reponse: str) -> _Msgs:
    msgs = _Msgs(reponse)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(module, "Anthropic", lambda api_key: type("C", (), {"messages": msgs})())
    return msgs


def test_juge_compliance_ne_voit_pas_le_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools import compliance

    msgs = _patch_anthropic(monkeypatch, compliance, '{"verdict": "approved"}')
    compliance._llm_judge("corps", "sujet", _RJ_AVEC_DIAG, [])
    user = msgs.kwargs["messages"][0]["content"]
    assert "Plomberie Dupont, 6 camions" in user
    assert research.DIAGNOSTIC_KEY not in user
    assert "fuite@tiers-a-ne-pas-voir.net" not in user


def test_composeur_de_reponse_ne_voit_pas_le_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools import reply

    msgs = _patch_anthropic(monkeypatch, reply, '{"body_text": "ok"}')
    monkeypatch.setattr(reply, "estimated_cost_usd", lambda *a, **k: 0.0)
    reply._call_composer(
        original_email_text="cold",
        lead_reply_text="intéressé",
        research_json=_RJ_AVEC_DIAG,
        available_slots=[],
        booking_url="https://cal.com/x",
        model="m",
    )
    user = msgs.kwargs["messages"][0]["content"]
    assert "Plomberie Dupont, 6 camions" in user
    assert research.DIAGNOSTIC_KEY not in user
    assert "fuite@tiers-a-ne-pas-voir.net" not in user
