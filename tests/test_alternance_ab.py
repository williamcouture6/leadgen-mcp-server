"""L'alternance A/B, et la mine qu'elle désamorce.

`template_choice="AB"` fait générer les deux variantes, mais rien n'attribuait
de bras : la colonne aurait porté « AB » sur 100 % des lignes, et il n'y aurait
pas eu de test — juste deux textes et aucune trace de qui a reçu quoi.

🔴 Et le défaut était PIRE que ça, mesuré le 2026-08-30. La route de conformité
lisait `input_payload.template_choice` — donc « AB » — et `check_length`
cherchait les bornes de `("agence-ia", "AB")`. Absentes, il retombait sur le
repli de la piste OPT (60-95 mots) et refusait un corps de 217 mots. Poser
« AB » dans n8n (tâche 18) aurait refusé 100 % des brouillons, chacun sorti du
lot pour toujours, chaque contact gelé à vie.

Trois ceintures indépendantes ferment ce trou. Chacune a son test ici.
"""
from __future__ import annotations

from typing import Any

import pytest

from src import http_api
from src.lib import compliance_checks as cc
from tests.fixtures.corps_ac1 import CORPS_A


# ---------------- Ceinture 1 : le repli de bornes ne change pas de piste ----------------

@pytest.mark.parametrize("gabarit", ["AB", "", "Z", "gabarit-inconnu", None])
def test_un_gabarit_inconnu_reste_dans_les_bornes_de_SA_piste(gabarit) -> None:
    """Mesuré : ('agence-ia', 'AB') retombait sur les bornes OPT (60-95) et
    refusait un corps de 217 mots. Une piste dont on connaît les bornes doit
    rester dans SES bornes, même quand le gabarit est méconnaissable.

    ⚠️ « RELANCE » n'est PAS un inconnu : c'est un gabarit déclaré, à 40-120
    mots. Un corps de tri jugé sous ce gabarit doit échouer — le repli ne doit
    pas non plus effacer les gabarits réels."""
    r = cc.check_length(CORPS_A, template=gabarit, track="agence-ia")
    assert r.passed, f"gabarit={gabarit!r} : {r.message}"


def test_le_repli_neffacce_pas_les_gabarits_reels() -> None:
    """Contrôle négatif du test précédent : si le repli s'appliquait à TOUT,
    une relance de 217 mots passerait et la borne des relances ne servirait
    plus à rien."""
    assert not cc.check_length(CORPS_A, template="RELANCE", track="agence-ia").passed


def test_la_piste_OPT_garde_ses_propres_bornes() -> None:
    """Contrôle négatif : le repli par piste ne doit pas élargir OPT."""
    corps_opt = "Bonjour,\n\n" + " ".join(["mot"] * 75)
    assert cc.check_length(corps_opt, template="Z", track="OPT").passed
    assert not cc.check_length(CORPS_A, template="Z", track="OPT").passed


def test_sans_piste_le_repli_historique_tient() -> None:
    corps = "Bonjour,\n\n" + " ".join(["mot"] * 75)
    assert cc.check_length(corps, template="A", track=None).passed


# ---------------- Ceinture 2 : l'alternance attribue un bras ----------------

def test_AB_alterne_par_rang_dans_le_lot() -> None:
    bras = [http_api._bras_ab("AB", i) for i in range(10)]
    assert bras == ["A", "B"] * 5


def test_AB_donne_autant_de_A_que_de_B_sur_un_lot_pair() -> None:
    bras = [http_api._bras_ab("AB", i) for i in range(20)]
    assert bras.count("A") == bras.count("B") == 10


@pytest.mark.parametrize("force", ["A", "B"])
def test_un_bras_force_nest_jamais_alterne(force: str) -> None:
    """Le mode manuel (rejeu d'un lead précis) doit rester déterministe."""
    assert {http_api._bras_ab(force, i) for i in range(10)} == {force}


def test_lalternance_ne_derive_pas_du_contact() -> None:
    """🔴 La spec l'exige : « sans ça, A part sur les contacts les plus anciens
    et B sur les plus récents, et le test mesure l'ordre de la file au lieu du
    courriel ». La file est triée `created_at.asc`, donc toute répartition
    dérivée du contact serait corrélée à son ancienneté.

    `_bras_ab` ne prend QUE le rang : c'est structurellement impossible.
    """
    import inspect

    params = set(inspect.signature(http_api._bras_ab).parameters)
    assert params == {"template_choice", "rang"}, (
        f"la fonction voit {params} — tout ce qui vient du contact rouvrirait "
        "la corrélation avec l'ancienneté"
    )


# ---------------- Ceinture 3 : la colonne porte la variante ECRITE ----------------

@pytest.mark.asyncio
async def test_la_colonne_porte_la_variante_reellement_ecrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le rédacteur a choisi B alors qu'on demandait « AB » : c'est B qui doit
    se retrouver en base, jamais « AB »."""
    capture: dict[str, Any] = {}

    async def faux_personalize(payload):
        from src.tools.personalize import LLMUsage, PersonalizeOut

        return PersonalizeOut(
            email={
                "template_used": "B", "subject": "s", "body_text": "corps",
                "relance_1": "r1", "relance_2": "r2",
            },
            template_used="B", contact_used=False, social_proof_count=0,
            available_slots_at_generation=[], duration_ms=1, model="m",
            usage=LLMUsage(),
        )

    async def faux_insert(payload):
        capture["draft"] = payload
        return {"message_id": "msg-1"}

    async def faux_agent_run(payload):
        capture["agent_run"] = payload
        return {"agent_run_id": "ar-1"}

    monkeypatch.setattr(http_api.personalize_tools, "personalize", faux_personalize)
    monkeypatch.setattr(http_api.db_tools, "insert_message_draft", faux_insert)
    monkeypatch.setattr(http_api.db_tools, "record_agent_run", faux_agent_run)

    await http_api._personalize_one(
        {"id": "ct-1", "email": "a@ex.ca"},
        {"id": "co-1", "name": "Ex", "research_json": {"x": 1}, "track": "agence-ia"},
        template_choice="AB", model="m", persist=True,
        available_slots=[], social_proof=[],
    )

    assert capture["draft"].template_choice == "B"
    assert capture["draft"].followups == {"relance_1": "r1", "relance_2": "r2"}
    assert capture["agent_run"].input_payload["template_choice"] == "B", (
        "c'est CE champ que la conformité lit pour choisir les bornes"
    )
    assert capture["agent_run"].input_payload["template_demande"] == "AB", (
        "le paramètre demandé reste tracé, séparément"
    )


@pytest.mark.asyncio
async def test_une_variante_illisible_nest_jamais_ecrite_en_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La contrainte de la colonne refuse tout ce qui n'est ni A ni B. Plutôt
    que de faire échouer l'insert du courriel entier, on écrit NULL."""
    capture: dict[str, Any] = {}

    async def faux_personalize(payload):
        from src.tools.personalize import LLMUsage, PersonalizeOut

        return PersonalizeOut(
            email={"subject": "s", "body_text": "corps"},
            template_used="AB", contact_used=False, social_proof_count=0,
            available_slots_at_generation=[], duration_ms=1, model="m",
            usage=LLMUsage(),
        )

    async def faux_insert(payload):
        capture["draft"] = payload
        return {"message_id": "msg-1"}

    monkeypatch.setattr(http_api.personalize_tools, "personalize", faux_personalize)
    monkeypatch.setattr(http_api.db_tools, "insert_message_draft", faux_insert)
    monkeypatch.setattr(
        http_api.db_tools, "record_agent_run",
        lambda payload: _async_ret({"agent_run_id": "ar-1"}),
    )

    await http_api._personalize_one(
        {"id": "ct-1", "email": "a@ex.ca"},
        {"id": "co-1", "name": "Ex", "research_json": {"x": 1}, "track": "agence-ia"},
        template_choice="AB", model="m", persist=True,
        available_slots=[], social_proof=[],
    )

    assert capture["draft"].template_choice is None


async def _async_ret(valeur: Any) -> Any:
    return valeur
