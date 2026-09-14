"""La règle « C et D sont-ils servables à cette entreprise ? », lue depuis une FICHE.

🔴 POURQUOI CETTE FONCTION EXISTE, ET POURQUOI ELLE EST DANS `lib/`.

`tete_fixe_servable` prend trois booléens déjà calculés. Le calcul de ces trois
booléens depuis une fiche `companies` — résoudre les métiers, autoriser la
citation d'avis, compter les services — vivait dans `http_api._tete_fixe_servable`,
donc inaccessible à autre chose qu'une route HTTP.

Le rattrapage de `messages.bras_eligibles` (2026-09-13) a besoin EXACTEMENT de ce
calcul : il reconstitue, pour un brouillon déjà écrit, l'ensemble des bras qui
étaient réellement en jeu. Le recopier dans un script aurait fabriqué une
deuxième vérité — et elle aurait divergé précisément sur les cas limites (avis
sous le plancher, un seul service), c'est-à-dire sur les lignes que le rattrapage
existe pour étiqueter correctement.

🔴 LA PROPRIÉTÉ DONT DÉPEND LE RATTRAPAGE : le résultat NE DÉPEND PAS DE LA DATE.
Un backfill rejoué un autre jour doit rendre la même chose, sinon il réécrit
l'histoire au lieu de la reconstituer. `classer_services` est sans calendrier
depuis AC1c·A, et cette fonction l'appelle directement plutôt que de passer par
`resoudre_metiers(services, date.today())` dont le nom laisse croire l'inverse.
Le dernier test de ce module tient cette propriété.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest

from src.lib.gabarits import tete_fixe_servable_pour_entreprise

# Les planchers de `lib/avis` : 10 avis ET 4,0 de note.
AVIS_OK: dict[str, Any] = {"google_rating": 4.8, "google_reviews_count": 40}
AVIS_REFUSE: dict[str, Any] = {"google_rating": 3.2, "google_reviews_count": 4}


def _fiche(services: list[str] | None, **avis: Any) -> dict[str, Any]:
    return {
        "name": "ZZ Fiche",
        "research_json": None if services is None else {"services_offered": services},
        **avis,
    }


def test_metier_reconnu_et_citation_autorisee() -> None:
    """Le cas courant : C et D peuvent nommer le métier et citer les avis."""
    fiche = _fiche(["Déneigement résidentiel"], **AVIS_OK)
    assert tete_fixe_servable_pour_entreprise(fiche) is True


def test_metier_reconnu_citation_refusee_un_seul_service() -> None:
    """🔴 LE CAS QUI A CHANGÉ DE RÉPONSE le 2026-09-14.

    Mesuré le 2026-09-07 sur 6 entreprises, 7 le 2026-09-14 : sans citation
    d'avis, le repli de `{ANCRE_CD}` écrit « autant X que Y pis Z » puis « tu en
    couvres beaucoup! ». Avec UN libellé, la forme est impossible.

    On refusait le GABARIT faute de PHRASE, et ces entreprises ne pouvaient
    tirer que A ou B — ~2 % du test A/B amputé. William a écrit la phrase
    manquante (3ᵉ version du bloc 2, sur une supposition), donc le refus tombe.

    ⚠️ Ce test garde la trace du renversement au lieu d'être supprimé : le jour
    où quelqu'un relit `bras_eligibles` et voit 4 lignes en `AB`, la raison est
    ici.
    """
    fiche = _fiche(["Déneigement résidentiel"], **AVIS_REFUSE)
    assert tete_fixe_servable_pour_entreprise(fiche) is True


def test_metier_reconnu_citation_refusee_deux_services() -> None:
    """Contrôle négatif du précédent : à deux libellés, l énumération tient."""
    fiche = _fiche(["Déneigement résidentiel", "Tonte de pelouse"], **AVIS_REFUSE)
    assert tete_fixe_servable_pour_entreprise(fiche) is True


def test_aucun_metier_reconnu_ferme_c_et_d() -> None:
    """🔴 La garde principale. Les avis et le nombre de services ne rachètent rien :
    `{METIER}` n a rien à recevoir, et le rédacteur inventerait la première ligne."""
    fiche = _fiche(["Consultation stratégique", "Coaching d affaires"], **AVIS_OK)
    assert tete_fixe_servable_pour_entreprise(fiche) is False


@pytest.mark.parametrize("fiche", [
    {},
    {"research_json": None},
    {"research_json": {}},
    {"research_json": {"services_offered": None}},
    {"research_json": {"services_offered": []}},
])
def test_une_fiche_sans_recherche_ferme_c_et_d(fiche: dict[str, Any]) -> None:
    """Pas de matière = pas de métier = pas de tête fixe. Aucune de ces formes ne
    doit lever : le rattrapage les rencontre sur les fiches jamais researchées."""
    assert tete_fixe_servable_pour_entreprise(fiche) is False


def test_le_resultat_ne_depend_pas_de_la_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 LA PROPRIÉTÉ DONT DÉPEND LE RATTRAPAGE.

    `bras_eligibles` décrit un tirage qui a EU LIEU. Si ce calcul dépendait du
    jour où on le rejoue, le backfill n étiquetterait pas le passé, il en
    écrirait un autre — et il le ferait en silence, puisque rien ne compare.

    Le déneigement a une fenêtre saisonnière étroite : en juillet elle est
    fermée, en décembre ouverte. La SCÈNE du courriel change donc avec la date ;
    l APPARIEMENT du métier, non. C est l appariement seul qui décide de C et D.
    """
    from src.lib import metiers as _metiers

    fiche = _fiche(["Déneigement résidentiel"], **AVIS_OK)
    resultats = []
    for jour in (_dt.date(2026, 7, 15), _dt.date(2026, 12, 15)):
        class _Fige(_dt.date):
            @classmethod
            def today(cls):
                return jour

        monkeypatch.setattr(_metiers, "date", _Fige)
        resultats.append(tete_fixe_servable_pour_entreprise(fiche))

    assert resultats == [True, True], (
        f"le calcul a change avec la date : {resultats} — un rattrapage rejoue "
        "un autre jour reecrirait l histoire"
    )


def test_http_api_delegue_a_cette_fonction() -> None:
    """🔴 UNE SEULE VÉRITÉ. Si `http_api._tete_fixe_servable` se remettait à
    calculer de son côté, le rattrapage étiquetterait selon une règle et la
    production en servirait une autre — le biais que `bras_eligibles` existe
    pour rendre visible, redevenu invisible."""
    from src import http_api

    for fiche in (
        _fiche(["Déneigement résidentiel"], **AVIS_OK),
        _fiche(["Déneigement résidentiel"], **AVIS_REFUSE),
        _fiche(["Consultation stratégique"], **AVIS_OK),
        _fiche(None),
    ):
        assert http_api._tete_fixe_servable(fiche) is tete_fixe_servable_pour_entreprise(fiche)
