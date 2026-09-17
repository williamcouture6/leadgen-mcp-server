"""Ce qui ne peut pas ouvrir la fenêtre ne se mentionne pas non plus.

🔴 RÈGLE DE WILLIAM, 2026-09-16, née d'un refus de conformité mesuré la veille.

« Excavation sur mesure (fondations, drains français, piscines creusées) »
faisait écrire à Groupe Everest qu'il fait de la piscine. Il en CREUSE ; il n'en
vend pas l'entretien. Le juge a refusé le brouillon, à raison.

Le système savait déjà faire la différence — `EXIGE` dit que la famille
`piscine` ne compte que si un libellé porte un verbe d'entretien. Mais cette
exigence fermait la FENÊTRE sans empêcher la MENTION : l'entreprise n'était
jamais démarchée *pour* la piscine, et son courriel en parlait quand même.

⚠️ UNE AUTRE PISTE A ÉTÉ MESURÉE PUIS ÉCARTÉE : traiter comme incidente toute
famille qui n'apparaît qu'entre parenthèses. Sur les 27 fiches concernées, la
plupart étaient légitimes — « Aménagement paysager complet (excavation,
dallage, pavage) » vend vraiment du pavage. La parenthèse ÉNUMÈRE ce que le
service inclut. Le SENS distingue une mention incidente ; la position non.

📏 Coût mesuré sur les 526 fiches qui ont des services : 25 cessent de nommer
`piscine`, 0 ne perd tous ses métiers.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.tools.personalize import bloc_metiers_resolus, metiers_mentionnables

CREUSEUR = [
    "Excavation sur mesure (fondations, drains français, piscines creusées)",
    "Terrassement et pavé uni",
    "Déneigement commercial",
]
PAYSAGISTE = ["Aménagement paysager", "Contour de piscine", "Tonte de gazon"]
PISCINISTE = [
    "Entretien hebdomadaire de piscine",
    "Ouverture de piscine",
    "Fermeture de piscine",
]


class TestLaRegle:
    def test_creuser_une_piscine_ne_fait_pas_un_metier_de_piscine(self) -> None:
        assert "piscine" not in metiers_mentionnables(CREUSEUR, date(2026, 6, 15))

    def test_un_contour_de_piscine_non_plus(self) -> None:
        assert "piscine" not in metiers_mentionnables(PAYSAGISTE, date(2026, 6, 15))

    def test_un_vrai_pisciniste_garde_son_metier(self) -> None:
        """La contre-épreuve. Sans elle, « ne jamais nommer piscine » passerait
        aussi — et on aurait supprimé la famille au lieu de la qualifier."""
        assert "piscine" in metiers_mentionnables(PISCINISTE, date(2026, 6, 15))

    def test_les_autres_metiers_ne_sont_pas_touches(self) -> None:
        """`EXIGE` ne contient que `piscine`. Rien d'autre ne doit bouger."""
        nommables = metiers_mentionnables(CREUSEUR, date(2026, 6, 15))
        assert {"excavation", "pavage", "déneigement"} <= set(nommables)


@pytest.mark.parametrize("mois", [2, 6, 9, 11])
class TestLesTroisEndroits:
    """🔴 TROIS ENDROITS LISAIENT LE MÉTIER, ET IL A FALLU TROIS PASSES.

    Le premier jet ne filtrait que le 2ᵉ temps. Restaient :
      · la SCÈNE — hors saison elle retombe sur le dominant, qui ignorait
        l'exigence ; en juin, Groupe Everest n'a aucune fenêtre ouverte, donc
        l'ouvreur entier parlait de piscines ;
      · la consigne « nomme son métier dominant en premier », servie quand la
        scène pèse ≤ 25 % des libellés.

    D'où le test sur PLUSIEURS MOIS : chacun emprunte un chemin différent, et
    un seul mois testé aurait laissé les deux autres passer.
    """

    def test_le_bloc_du_redacteur_ne_nomme_jamais_la_piscine(self, mois: int) -> None:
        for services in (CREUSEUR, PAYSAGISTE):
            bloc = bloc_metiers_resolus(services, date(2026, mois, 15), "A", None)
            assert "piscine" not in bloc.lower(), f"mois {mois} : {bloc}"

    def test_le_vrai_pisciniste_la_nomme_tous_les_mois(self, mois: int) -> None:
        bloc = bloc_metiers_resolus(PISCINISTE, date(2026, mois, 15), "A", None)
        assert "piscine" in bloc.lower(), f"mois {mois}"
