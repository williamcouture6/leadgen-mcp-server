"""`metiers_nommes` — quelles familles de metiers un TEXTE LIBRE nomme-t-il ?

🔴 CETTE PRIMITIVE EST NEE D'UN CONTROLE QUI N'EXISTE PAS, et l'histoire vaut
d'etre gardee : elle evite de refaire l'impasse.

Le 2026-09-15, premier passage de WF-5 sur la copie AC1 : **9 refus sur 20**, et
**8 des 9** portaient la meme faute apparente — le redacteur nommait un service
que la recherche ne confirmait pas (« deneigement », « excavation », « lavage de
vitres », « piscine », « pavage », « paysagement »). Aucun des 19 controles
deterministes ne regardait ca : `services_offered` n'apparaissait nulle part
dans `compliance_checks.py`. Le defaut le plus frequent du systeme ne reposait
que sur le juge LLM.

L'idee etait donc un controle deterministe : comparer les metiers NOMMES dans le
courriel a ceux que l'entreprise exerce VRAIMENT, et refuser l'ecart. D'ou cette
fonction, qui repond a la premiere moitie de la question.

⚠️ **LE CONTROLE A ETE CONSTRUIT, MESURE, ET ABANDONNE.** Sur les 20 brouillons
deja juges : **0 detection sur 9 refus**, 0 faux positif sur 11 approuves. Il ne
voyait rien, parce que les « vrais metiers » auxquels il comparait etaient
eux-memes faux — c'est le dictionnaire qui se trompait, pas le redacteur.
Verifier une traduction avec le dictionnaire qui a servi a la faire ne trouve
jamais rien.

Les deux causes reelles ont ete corrigees a leur source, dans `lib/metiers` :
« pression » seul ne vaut plus « lavage de vitres », et le mot-cle de sourcing
ne parle plus quand une saison est deja connue. Voir
`tests/test_pression_et_secteur.py`.

⚠️ **NE PAS RECONSTRUIRE LE CONTROLE SANS MESURER D'ABORD.** S'il redevient
souhaitable un jour, la mesure prealable est la meme : le passer sur les
brouillons deja juges et compter les accords. Un controle qui detecte 0 sur 9
est pire qu'absent — il rassure.

📏 Ce que la fonction garde d'utile : elle est la seule facon de demander « de
quoi ce texte parle-t-il ? » en reutilisant le dictionnaire au lieu d'en
recopier les mots, et c'est elle qui a servi a toutes les mesures du 2026-09-15.
"""
from __future__ import annotations

import pytest

from src.lib.metiers import metiers_nommes


class TestMetiersNommes:
    """La primitive : quelles familles de métiers un texte libre nomme-t-il ?"""

    def test_reconnait_un_metier_dans_une_phrase(self) -> None:
        assert "déneigement" in metiers_nommes(
            "je vois que tu fais du déneigement dans la région de Laval"
        )

    def test_insensible_aux_accents(self) -> None:
        """Le corps généré est formaté ; les accents ne doivent rien décider."""
        assert metiers_nommes("du deneigement") == metiers_nommes("du déneigement")

    def test_rend_TOUTES_les_familles_nommees_sans_dominance(self) -> None:
        """🔴 LA RAISON D'ÊTRE DE CETTE FONCTION, et pourquoi `classer_services`
        ne pouvait pas servir.

        `classer_services` applique `ECRASE`, la règle de DOMINANCE : quand deux
        métiers cohabitent, l'un efface l'autre pour décider du lexique. C'est
        juste pour choisir de quoi parler — et faux pour détecter ce qui est
        DIT. Un métier inventé que `ECRASE` ferait disparaître passerait au
        travers du contrôle sans laisser de trace.
        """
        nommes = metiers_nommes("du déneigement, de l'excavation pis du pavage")
        assert {"déneigement", "excavation", "pavage"} <= nommes

    def test_les_exclusions_du_dictionnaire_s_appliquent(self) -> None:
        """« clôture de piscine » ne fait pas de lui un pisciniste — la règle
        vit déjà dans EXCLUSIONS, on ne la réécrit pas ici."""
        assert "piscine" not in metiers_nommes("installation de clôtures de piscine")

    def test_un_texte_sans_metier_ne_rend_rien(self) -> None:
        assert metiers_nommes("bonjour, j'espère que tu vas bien") == frozenset()

    def test_texte_vide(self) -> None:
        assert metiers_nommes("") == frozenset()
        assert metiers_nommes(None) == frozenset()  # type: ignore[arg-type]
