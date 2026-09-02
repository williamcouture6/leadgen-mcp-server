"""Les invariants de FORME des corps de référence.

Ces tests ne jugent pas la copie — William la tranche. Ils empêchent qu'une
édition future réintroduise en silence quelque chose que la spec interdit :
une signature dans le corps, un tiret cadratin, un lien, un chiffre d'avis
là où le repli devait le retirer.

Un corps ajouté à `TOUS_LES_CORPS` y est automatiquement soumis.
"""
from __future__ import annotations

import re

import pytest

from src.lib import compliance_checks as cc
from tests.fixtures import corps_ac1 as f


CORPS = sorted(f.TOUS_LES_CORPS.items())


@pytest.mark.parametrize("nom,corps", CORPS)
def test_aucun_corps_ne_porte_de_signature(nom: str, corps: str) -> None:
    """Décision William 2026-08-30 : les mentions vivent dans la signature du
    compte Instantly, pas dans le corps. Un corps qui en porte une remettrait
    l'adresse postale dans le texte et fausserait le compte de mots."""
    assert "193 rue de l'Anse" not in corps, nom
    assert "Politique de confidentialité" not in corps, nom
    assert "Pour te désabonner" not in corps, nom
    assert "\n---\n" not in corps, nom


@pytest.mark.parametrize("nom,corps", CORPS)
def test_aucun_tiret_cadratin(nom: str, corps: str) -> None:
    """Règle de rédaction nº1. Les relances de la spec du 26 en portaient un
    (« Je te charge rien — tu veux… ») : c'est le signe le plus visible d'un
    courriel écrit par une machine."""
    assert "—" not in corps, f"{nom} porte un tiret cadratin"


@pytest.mark.parametrize("nom,corps", CORPS)
def test_aucun_lien_dans_le_corps(nom: str, corps: str) -> None:
    """Pivot tri du 2026-08-20 : le courriel froid ne porte AUCUN lien, et
    surtout plus de jeton {{DEMO_URL}}. Le lien de l'aperçu part dans la
    réponse de William au « oui », à la main."""
    assert "http" not in corps, nom
    assert "{{" not in corps, nom


@pytest.mark.parametrize("nom,corps", CORPS)
def test_le_registre_est_le_tutoiement(nom: str, corps: str) -> None:
    assert cc.check_registre(corps, track="agence-ia").passed, nom


@pytest.mark.parametrize("nom,corps", CORPS)
def test_le_budget_de_pis_est_respecte(nom: str, corps: str) -> None:
    assert cc.check_tics_de_langage(corps).passed, nom


# Les corps qui ferment en douceur, sans demander de geste.
#
# Textes de William, remarque `cta_present` signalée et assumée le 2026-09-01.
# La relance 1 finit sur « J'espère pouvoir t'en parler un peu plus! » et la
# relance 3 est un ADIEU : lui coller un « dis-moi juste si » contredirait la
# phrase « je ne vais plus t'écrire » qui la précède.
FERMETURES_DOUCES = frozenset({"RELANCE_1", "RELANCE_3"})


@pytest.mark.parametrize("nom,corps", CORPS)
def test_chaque_corps_demande_explicitement_une_reponse(nom: str, corps: str) -> None:
    """`check_cta_present` a été mesuré FAUX VERT à deux moitiés sur CORPS_A.
    Les relances doivent porter une invitation explicite, elles aussi — c'est
    le test qui manquait à la spec du 26 et qui aurait révélé le défaut.

    🔧 Deux exceptions NOMMÉES depuis le 2026-09-01, et vérifiées dans les deux
    sens : si l'une d'elles se met à porter un vrai CTA, le test échoue et
    demande de la retirer de la liste. Une exception qu'on n'entretient pas
    devient une permission oubliée."""
    resultat = cc.check_cta_present(corps)
    if nom in FERMETURES_DOUCES:
        assert not resultat.passed, (
            f"{nom} porte maintenant un CTA explicite — le retirer de "
            f"FERMETURES_DOUCES dans le même commit"
        )
    else:
        assert resultat.passed, nom


@pytest.mark.parametrize("nom", ["CORPS_A_REPLI_AVIS", "CORPS_B_REPLI_AVIS"])
def test_le_repli_retire_le_chiffre_mais_garde_le_paragraphe(nom: str) -> None:
    """Le repli du bloc 2 retire la CITATION, pas le paragraphe. La v3 faisait
    sauter le bloc entier, ce qui amputait ~25 mots et laissait le corps à un
    mot de la borne basse."""
    corps = f.TOUS_LES_CORPS[nom]
    assert not re.search(r"\d[,.]\d\s*étoiles?", corps), "la citation aurait dû sauter"
    assert not re.search(r"\d+\s*avis", corps), "la citation aurait dû sauter"
    if nom.startswith("CORPS_A"):
        assert "Du monde qui te cherche, t'en as." in corps
    else:
        assert "c'est probablement pas parce que le monde t'aime pas" in corps


@pytest.mark.parametrize(
    # RELANCE_2_SANS_SITE a disparu le 2026-09-01 : la relance 2 réécrite ne
    # parle plus du site, donc il n'y a plus de bascule à tester.
    "nom", ["CORPS_A_SANS_SITE", "CORPS_B_SANS_SITE",
            "CORPS_C_SANS_SITE", "CORPS_D_SANS_SITE",
            "CORPS_C_REPLI_SANS_SITE", "CORPS_D_REPLI_SANS_SITE"]
)
def test_la_variante_sans_site_ne_promet_jamais_de_rafraichir(nom: str) -> None:
    """97 boîtes sur 255 n'ont pas de site. Leur promettre une « version
    rafraîchie » est un mensonge immédiatement visible pour le seul
    destinataire capable de le détecter.

    Et le site reste TOUJOURS à faire : « je pourrais », jamais « je te
    l'envoie ». Il se fabrique à la main APRÈS le oui — c'est la dette
    d'honnêteté refermée le 2026-08-26."""
    corps = f.TOUS_LES_CORPS[nom]

    # 🔴 L'INVARIANT QUI VAUT POUR LES QUATRE GABARITS : on ne REFAIT jamais un
    # site qui n'existe pas. C'est le mensonge que le destinataire repère en une
    # seconde, et il tient quelle que soit la décision sur le reste.
    assert "rafraîchie" not in corps, nom
    assert "refaire" not in corps.lower(), nom

    # 🔧 Ce qui suit ne vaut QUE pour A et B. C et D disent le site déjà fait
    # (« j'en ai aussi profité pour te faire un site web au goût du jour ») —
    # décision William du 2026-08-31. Leur appliquer la règle du conditionnel
    # reviendrait à tester une copie qui n'existe plus ; la sauter en silence
    # laisserait A et B se dégrader sans bruit. On nomme donc les deux mondes.
    if nom in ("CORPS_A_SANS_SITE", "CORPS_B_SANS_SITE"):
        assert "au goût du jour" not in corps, nom
        # Casse insensible : la phrase peut commencer par « Je pourrais ».
        assert "je pourrais" in corps.lower(), nom
    else:
        assert "au goût du jour" in corps, (
            f"{nom} ne dit plus le site fait — si la copie a changé, ce test et "
            f"CORPS_QUI_DISENT_LE_SITE_FAIT doivent bouger dans le même commit"
        )


@pytest.mark.parametrize("nom", ["CORPS_A_SANS_SITE", "CORPS_B_SANS_SITE"])
def test_le_bloc_sans_site_du_courriel_1_porte_la_supposition(nom: str) -> None:
    """Formulation validée par William le 2026-08-30, après que le conseil ait
    relevé que l'ancienne (« j'ai vu que t'as pas de site web ») violait la
    règle nº4 du prompt lui-même.

    « Je pense que t'en as pas » est sa tournure de supposition, et elle est
    honnête : on le déduit d'une colonne `website` vide, on n'a rien vérifié.

    ⚠️ Ce test est ce qui autorise `j'ai vu` dans FIRST_PERSON_ACTION_PATTERNS.
    Si la phrase revenait, les 97 courriels sans site seraient bloqués — un
    refus `block` que `rejuger_a_relire` ne reprend jamais.
    """
    corps = f.TOUS_LES_CORPS[nom]
    assert "créer un site" in corps, nom
    assert "je pense que t'en as pas" in corps, nom
    assert "j'ai vu" not in corps, nom
    assert cc.check_first_person_actions(corps).passed, nom


def test_la_relance_1_sert_aux_deux_cas() -> None:
    """« Pour le site, l'offre tient toujours » ne dit rien de l'état du site :
    une seule version pour les 255, une variante de moins à maintenir."""
    assert "rafraîchie" not in f.RELANCE_1
    assert "Pour le site, l'offre tient toujours." in f.RELANCE_1


@pytest.mark.parametrize("nom,corps", CORPS)
def test_chaque_corps_garde_15_mots_de_marge(nom: str, corps: str) -> None:
    """La règle que la spec impose et que deux versions ont enfreinte : toute
    édition d'un corps oblige à remesurer DANS LE MÊME COMMIT, et on ne laisse
    jamais une marge d'un mot.

    Historique des récidives : la v1 annonçait 243/251 mots quand les vraies
    valeurs étaient 246/272 ; la v3 annonçait 211/229 pour 206/224. Ce test
    remplace la promesse de remesurer par une mesure qui casse toute seule.
    """
    gabarit = f.GABARIT[nom]
    borne_min, borne_max = cc._BORNES_LONGUEUR[("agence-ia", gabarit)]
    r = cc.check_length(corps, template=gabarit, track="agence-ia")
    n = len(cc._body_without_signature(corps).split())
    assert r.passed, f"{nom} : {n} mots, hors bornes {borne_min}-{borne_max}"
    assert borne_max - n >= 15, f"{nom} : {n} mots, seulement {borne_max - n} de marge haute"
    assert n - borne_min >= 15, f"{nom} : {n} mots, seulement {n - borne_min} de marge basse"


def test_les_deux_variantes_de_cta_sont_bien_amputees() -> None:
    """Garde-fou sur la garde : `_sans` doit lever si sa cible bouge, sinon la
    variante redevient identique à l'original en silence."""
    assert f.CORPS_A_SANS_CTA != f.CORPS_A
    assert f.CORPS_A_SANS_RENVOI != f.CORPS_A
    assert not cc.check_cta_present(f.CORPS_A_SANS_CTA).passed
    assert cc.check_cta_present(f.CORPS_A_SANS_RENVOI).passed
