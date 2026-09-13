"""L'alternance ne redémarre pas à zéro à chaque lot.

🔴 LE DÉFAUT, arithmétique et permanent. WF-4 tourne en DEUX lots de 10 par
jour — découpage posé le 2026-09-09 parce que Railway coupe une requête à
300 s. Or un lot de 10 ne se divise pas par 4 :

    rang  0 1 2 3 4 5 6 7 8 9
    bras  A B C D A B C D A B   ->  A=3  B=3  C=2  D=2

Chaque lot donnait donc 30 % / 30 % / 20 % / 20 %. Les taux restent justes —
une répartition inégale ne biaise pas une proportion — mais C et D
accumulaient leur base 1,5 fois plus lentement que A et B, soit environ six
semaines de retard sur le plancher de 400 livrés mûrs.

LA CORRECTION, en deux temps, et le second compte autant que le premier.

1. **Le rang repart d'où on en est**, pas de zéro. Deux lots de 10 se
   comportent alors comme un lot de 20, qui se divise exactement par quatre.

2. **Le compteur n'est PAS borné à la journée.** Premier jet : il l'était. Un
   jour qui écrit 18 brouillons au lieu de 20 laisse un reste de 2, et ce reste
   retombe TOUJOURS sur les deux premiers bras puisque le lendemain repart de
   zéro. À 18/jour régulier : 27,8/27,8/22,2/22,2, tous les jours, cumulatif —
   la même maladie en plus petit. En comptant depuis toujours, le reste se
   reporte et l'écart reste borné à 1 sur toute la durée du test.

⚠️ L'OFFSET SE MESURE, IL NE SE PASSE PAS EN PARAMÈTRE. Un `rang_depart` posé
dans le JSON n8n mentirait le jour où un lot rend 7 brouillons au lieu de 10
(contacts sautés, panne à mi-lot), et personne ne s'en apercevrait.

⚠️ ET LE RANG COMPTE LES ÉCRITURES, PAS LES CONTACTS PARCOURUS — voir
`test_un_contact_saute_ne_consomme_pas_de_bras`. Le premier jet utilisait
`enumerate(backlog)` : avec deux contacts sautés par lot, la répartition
tombait à 37,5/37,5/12,5/12,5, soit PIRE que le défaut qu'il corrigeait.
"""
from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _decor(monkeypatch, *, taille_lot: int, deja_ecrits: int) -> list[str]:
    """Fait tourner un lot et rend la suite des bras servis."""
    from src import http_api
    from src.tools import db as db_tools

    servis: list[str] = []

    async def _backlog(*, limit, max_per_company, track):
        return [
            {
                "contact": {"id": f"c{i}", "email": f"c{i}@test.invalid", "company_id": f"e{i}"},
                "company": {"id": f"e{i}", "name": f"Entreprise {i}", "track": track,
                            "research_json": {"services_offered": ["deneigement"]}},
            }
            for i in range(taille_lot)
        ]

    async def _bras_deja_servis(track: str) -> int:
        return deja_ecrits

    async def _personalize_one(contact_row, company_row, *, template_choice, **kw):
        servis.append(template_choice)
        # `message_id` non nul : c'est LUI qui fait avancer le rang, pas le
        # statut. Un stub qui l'omettrait testerait le cas « rien écrit ».
        return http_api.PersonalizeContactOut(
            contact_id=contact_row["id"], status="ok", message_id="m",
        )

    monkeypatch.setattr(db_tools, "list_contacts_to_personalize", _backlog)
    monkeypatch.setattr(http_api, "_personalize_one", _personalize_one)
    monkeypatch.setattr(http_api, "_bras_deja_servis", _bras_deja_servis)
    monkeypatch.setattr(http_api, "_tete_fixe_servable", lambda company: True)
    monkeypatch.setattr(http_api, "_tombe_sur_le_repli_du_lexique", lambda company: False)
    monkeypatch.setattr(http_api, "_load_client_references", lambda: [])
    return servis


async def test_un_compteur_a_zero_part_du_rang_zero(monkeypatch) -> None:
    from src import http_api

    servis = _decor(monkeypatch, taille_lot=10, deja_ecrits=0)
    await http_api._run_wf4(
        http_api.RunWf4In(limit=10, template_choice="ABCD", track="agence-ia", persist=True)
    )

    assert servis == list("ABCDABCDAB")


async def test_le_second_lot_reprend_ou_le_premier_sest_arrete(monkeypatch) -> None:
    """🔴 LE CŒUR. Le premier lot s'est arrêté au rang 10 ; le second doit donc
    commencer par C, pas par A. Les vingt brouillons du jour se répartissent
    alors 5/5/5/5 au lieu de 6/6/4/4."""
    from src import http_api

    servis = _decor(monkeypatch, taille_lot=10, deja_ecrits=10)
    await http_api._run_wf4(
        http_api.RunWf4In(limit=10, template_choice="ABCD", track="agence-ia", persist=True)
    )

    assert servis == list("CDABCDABCD")


async def test_deux_lots_consecutifs_donnent_une_repartition_egale(monkeypatch) -> None:
    from src import http_api
    from collections import Counter

    premier = _decor(monkeypatch, taille_lot=10, deja_ecrits=0)
    await http_api._run_wf4(
        http_api.RunWf4In(limit=10, template_choice="ABCD", track="agence-ia", persist=True)
    )
    second = _decor(monkeypatch, taille_lot=10, deja_ecrits=len(premier))
    await http_api._run_wf4(
        http_api.RunWf4In(limit=10, template_choice="ABCD", track="agence-ia", persist=True)
    )

    compte = Counter(premier + second)
    assert compte == {"A": 5, "B": 5, "C": 5, "D": 5}, (
        f"repartition {dict(compte)} au lieu de 5/5/5/5 — "
        "le rang est reparti de zero au second lot"
    )


async def test_un_premier_lot_incomplet_decale_le_second_de_ce_qui_a_ete_ecrit(monkeypatch) -> None:
    """La panne à mi-lot : le premier n'a écrit que 7 brouillons. Le second
    reprend au rang 7, pas au rang 10 — c'est tout l'intérêt de MESURER
    l'offset au lieu de le poser en paramètre.

    ⚠️ Le nom de ce test disait « ne décale pas » alors que son corps affirme
    un décalage de 7. Corrigé le 2026-09-10 : le décalage EST le comportement
    voulu, il suit simplement ce qui a été écrit et non la taille du lot."""
    from src import http_api

    servis = _decor(monkeypatch, taille_lot=5, deja_ecrits=7)
    await http_api._run_wf4(
        http_api.RunWf4In(limit=5, template_choice="ABCD", track="agence-ia", persist=True)
    )

    # rang 7 -> D, puis A, B, C, D
    assert servis == list("DABCD")


async def test_un_bras_force_ignore_le_rang(monkeypatch) -> None:
    """Le rejeu manuel d'un gabarit précis ne doit pas se mettre à alterner
    parce que la journée a déjà servi des brouillons."""
    from src import http_api

    servis = _decor(monkeypatch, taille_lot=4, deja_ecrits=13)
    await http_api._run_wf4(
        http_api.RunWf4In(limit=4, template_choice="C", track="agence-ia", persist=True)
    )

    assert servis == ["C", "C", "C", "C"]



async def test_un_contact_saute_ne_consomme_pas_de_bras(monkeypatch) -> None:
    """🔴 LE CAS QUE LE PREMIER JET RATAIT, et qui rendait le correctif PIRE
    que le défaut.

    Avec `enumerate(backlog)`, un contact sauté (pas de courriel, pas de
    recherche, erreur du rédacteur) faisait avancer le rang sans rien écrire.
    Le compteur en base, lui, ne comptait que les écritures : les deux
    dérivaient, et les rangs de fin de lot étaient REJOUÉS au lot suivant.
    Mesuré sur deux sauts par lot de 10 : A=6 B=6 C=2 D=2, soit 12,5 % pour C
    et D — moins bien que les 20 % que le correctif venait remplacer.
    """
    from src import http_api
    from src.tools import db as db_tools

    servis: list[str] = []

    async def _backlog(*, limit, max_per_company, track):
        return [
            {"contact": {"id": f"c{i}", "email": f"c{i}@test.invalid", "company_id": f"e{i}"},
             "company": {"id": f"e{i}", "name": f"E{i}", "track": track, "research_json": {}}}
            for i in range(6)
        ]

    async def _compteur(track: str) -> int:
        return 0

    async def _personalize_one(contact_row, company_row, *, template_choice, **kw):
        # Les contacts 1 et 3 sont sautes : ils ne doivent consommer aucun bras.
        if contact_row["id"] in ("c1", "c3"):
            return http_api.PersonalizeContactOut(
                contact_id=contact_row["id"], status="skipped_no_email",
            )
        servis.append(template_choice)
        return http_api.PersonalizeContactOut(
            contact_id=contact_row["id"], status="ok", message_id="m",
        )

    monkeypatch.setattr(db_tools, "list_contacts_to_personalize", _backlog)
    monkeypatch.setattr(http_api, "_personalize_one", _personalize_one)
    monkeypatch.setattr(http_api, "_bras_deja_servis", _compteur)
    monkeypatch.setattr(http_api, "_tete_fixe_servable", lambda company: True)
    monkeypatch.setattr(http_api, "_tombe_sur_le_repli_du_lexique", lambda company: False)
    monkeypatch.setattr(http_api, "_load_client_references", lambda: [])

    await http_api._run_wf4(
        http_api.RunWf4In(limit=6, template_choice="ABCD", track="agence-ia", persist=True)
    )

    # Quatre brouillons ecrits sur six contacts : les quatre bras, une fois
    # chacun. Avec l ancien `enumerate`, on aurait eu A, C, A, C.
    assert servis == ["A", "B", "C", "D"]
