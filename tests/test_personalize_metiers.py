"""Le rédacteur reçoit-il les métiers RÉSOLUS, et rend-il les TROIS corps ?

C'est la tâche qui branche tout : la résolution saisonnière (tâche 4), le
lexique (tâche 5) et les avis (tâche 7) n'existaient jusqu'ici que dans des
fonctions que personne n'appelait.

Le principe : **le modèle ne classe rien.** Il reçoit le métier de la scène,
ses autres métiers, la formulation à employer et les trois questions de
qualification, tous décidés par du code déterministe.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from src.tools import personalize as perso

# Paysagiste + déneigeur, le cas le plus courant (44 % de la liste).
SERVICES_MIXTE = ["aménagement paysager", "plates-bandes", "déneigement résidentiel"]
OCTOBRE = date(2026, 10, 15)
FEVRIER = date(2027, 2, 10)


def _message(
    services: list[str] | None = None,
    *,
    track: str = "agence-ia",
    aujourdhui: date = OCTOBRE,
    slots: list[dict[str, Any]] | None = None,
    **company: Any,
) -> str:
    from src.lib.calcom import format_slots_for_prompt

    return perso._format_input_for_llm(
        research={"services_offered": services if services is not None else SERVICES_MIXTE},
        company={"name": "MV Paysagiste", "website": "https://ex.ca", **company},
        contact=None,
        social_proof=[],
        template_choice="A",
        slots_block=format_slots_for_prompt(slots or []),
        track=track,
        aujourdhui=aujourdhui,
    )


# ---------------- Les metiers resolus arrivent au redacteur ----------------

def test_le_message_porte_le_metier_de_la_scene() -> None:
    msg = _message()
    assert "Métiers résolus" in msg
    assert "déneigement" in msg, "en octobre, seule sa fenêtre déneigement est ouverte"


def test_le_message_porte_les_autres_metiers() -> None:
    msg = _message()
    assert "paysagement" in msg


def test_le_message_dicte_la_FORMULATION_du_deuxieme_temps() -> None:
    """Le modèle ne doit pas avoir à décider entre « Pour le reste de l'année »
    et « Tu fais aussi » : la première affirme un contraste temporel qui devient
    un mensonge si on se trompe."""
    msg_hiver_ete = _message(SERVICES_MIXTE, aujourdhui=OCTOBRE)
    assert "Pour le reste de l'année" in msg_hiver_ete

    msg_meme_saison = _message(["tonte de pelouse", "aménagement paysager"], aujourdhui=FEVRIER)
    assert "Tu fais" in msg_meme_saison
    assert "Pour le reste de l'année" not in msg_meme_saison


def test_le_message_porte_le_lexique_du_metier_DOMINANT() -> None:
    """🔴 Le lexique suit le dominant, la scène suit la saison. Un laveur de
    vitres démarché en août à propos de la neige doit se faire demander le
    nombre d'étages, pas la grandeur de son entrée."""
    msg = _message(
        ["lavage de vitres commercial", "nettoyage de fenêtres", "lavage de vitres résidentiel", "déneigement"],
        aujourdhui=date(2026, 8, 20),
    )
    assert "le nombre d'étages" in msg
    assert "la grandeur de l'entrée" not in msg


def test_un_mono_metier_na_pas_de_deuxieme_temps() -> None:
    msg = _message(["déneigement"])
    assert "aucun" in msg.lower() or "mono-métier" in msg.lower()


def test_un_metier_inconnu_donne_un_ouvreur_generique() -> None:
    """Défaut inversé : le lead n'est jamais filtré, il reçoit un ouvreur sans
    métier nommé plutôt que rien."""
    msg = _message(["réparation de clôtures"])
    assert "générique" in msg.lower()


def test_le_message_signale_quand_aucune_fenetre_nest_ouverte() -> None:
    """Herbofleurs en octobre : deux métiers d'été, deux fenêtres fermées.
    ⚠️ Sans AC1c rien ne l'exclut mécaniquement, donc le rédacteur doit au
    moins pouvoir le signaler."""
    msg = _message(["tonte de pelouse", "aménagement paysager"], aujourdhui=OCTOBRE)
    assert "hors saison" in msg.lower() or "fenêtre" in msg.lower()


# ---------------- Cal.com sort du chemin ----------------

def test_aucun_bloc_calcom_pour_agence_ia() -> None:
    """🔴 Le bloc de créneaux ORDONNE un CTA de rendez-vous, en contradiction
    frontale avec la règle nº11 du prompt (« aucun rendez-vous proposé, aucun
    créneau, aucune heure »).

    Et le vider ne suffisait PAS : sur liste vide il dit encore « utilise un CTA
    générique type "15 minutes cette semaine ?" ». Le tour utilisateur étant
    plus récent que le système, c'est lui que le modèle suit.
    """
    msg = _message(track="agence-ia")
    assert "Créneaux disponibles" not in msg
    assert "15 minutes" not in msg
    assert "un appel rapide" not in msg


def test_avec_des_creneaux_reels_le_bloc_reste_absent_pour_agence_ia() -> None:
    slots = [{"day_fr": "mardi", "date_fr": "27 mai", "date_iso": "2026-05-27", "times": ["14h"]}]
    msg = _message(track="agence-ia", slots=slots)
    assert "Créneaux disponibles" not in msg
    assert "27 mai" not in msg


def test_la_piste_OPT_garde_son_bloc_calcom() -> None:
    """Rétrocompatibilité : la piste OPT est gelée mais intacte, et son prompt
    exige toujours deux créneaux réels."""
    slots = [{"day_fr": "mardi", "date_fr": "27 mai", "date_iso": "2026-05-27", "times": ["14h"]}]
    msg = _message(track="OPT", slots=slots)
    assert "Créneaux disponibles" in msg
    assert "27 mai" in msg


# ---------------- La sortie a trois corps ----------------

@pytest.mark.asyncio
async def test_les_relances_sont_injectees_pas_generees(monkeypatch: pytest.MonkeyPatch) -> None:
    capture: dict[str, Any] = {}

    def faux_llm(user_message: str, model: str, max_tokens: int = 2500, track: str = "OPT"):
        capture["max_tokens"] = max_tokens
        capture["message"] = user_message
        return (
            {
                "template_used": "A", "subject": "s", "body_text": "corps",
                "relance_1": "r1", "relance_2": "r2", "warnings": [],
            },
            perso.LLMUsage(),
        )

    monkeypatch.setattr(perso, "_call_llm", faux_llm)

    out = await perso.personalize(
        perso.PersonalizeIn(
            research_json={"services_offered": SERVICES_MIXTE},
            company={"name": "MV", "website": "https://ex.ca"},
            track="agence-ia",
        )
    )

    # 🔴 Le modèle a produit « r1 » et « r2 ». Ils sont ÉCRASÉS : depuis le
    # 2026-09-01 les relances sont des constantes, identiques pour les quatre
    # gabarits. Fusionner « seulement si absent » laisserait passer exactement
    # le texte dérivé qu'on veut rendre impossible.
    from src.lib.relances import CLES_RELANCES, CORPS_RELANCES

    assert out.email["relance_1"] == CORPS_RELANCES["relance_1"]
    assert out.email["relance_1"] != "r1", "la version du modèle a survécu"
    assert out.email["relance_2"] == CORPS_RELANCES["relance_2"]
    # La troisième n'a JAMAIS été produite par le modèle et doit quand même
    # être là — c'est toute la différence entre injecter et compléter.
    assert out.email["relance_3"] == CORPS_RELANCES["relance_3"]
    assert set(CLES_RELANCES) <= set(out.email)


@pytest.mark.asyncio
async def test_template_used_vaut_A_ou_B_jamais_AB(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 `template_choice="AB"` fait générer les deux variantes, mais la
    colonne stockerait « AB » sur 100 % des lignes : pas de test A/B, juste
    deux textes et aucune trace de qui a reçu quoi."""

    def faux_llm(user_message: str, model: str, max_tokens: int = 2500, track: str = "OPT"):
        return (
            {"template_used": "B", "subject": "s", "body_text": "c", "relance_1": "r1", "relance_2": "r2"},
            perso.LLMUsage(),
        )

    monkeypatch.setattr(perso, "_call_llm", faux_llm)

    out = await perso.personalize(
        perso.PersonalizeIn(
            research_json={"services_offered": SERVICES_MIXTE},
            company={"name": "MV"},
            template_choice="AB",
            track="agence-ia",
        )
    )

    assert out.template_used == "B", "la variante RÉELLEMENT écrite, pas le paramètre"


@pytest.mark.asyncio
async def test_une_relance_vide_du_modele_est_remplacee(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ce test gardait un WARNING ; il garde maintenant une GARANTIE.

    Avant le 2026-09-01, une troncature du modèle rendait une relance vide et
    le brouillon était refusé au push, trois étapes plus loin — d'où un
    avertissement à la génération pour qu'on sache pourquoi.

    L'avertissement n'a plus d'objet : les relances ne sont plus générées. Une
    constante ne se tronque pas. Ce qui reste à vérifier est plus fort — même
    quand le modèle rend une chaîne vide, c'est le texte décidé qui part."""

    def faux_llm(user_message: str, model: str, max_tokens: int = 2500, track: str = "OPT"):
        return (
            {"template_used": "A", "subject": "s", "body_text": "c", "relance_1": "r1", "relance_2": ""},
            perso.LLMUsage(),
        )

    monkeypatch.setattr(perso, "_call_llm", faux_llm)

    out = await perso.personalize(
        perso.PersonalizeIn(
            research_json={"services_offered": SERVICES_MIXTE},
            company={"name": "MV"},
            track="agence-ia",
        )
    )

    from src.lib.relances import CORPS_RELANCES

    assert out.email["relance_2"] == CORPS_RELANCES["relance_2"]
    assert out.email["relance_2"].strip(), "une relance vide est partie quand même"
