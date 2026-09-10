"""« deux X à Y » ne doit bloquer que devant un NOM PROPRE.

Le motif vise « deux cliniques à Montréal » — un claim de plusieurs clients
dans un lieu. Avant le 2026-09-01 il attrapait n'importe quel « à » dans les
30 caractères suivants, en sévérité `block` : brouillon mort, contact gelé à
vie. Sept tournures honnêtes sur sept y passaient, dont « il pose deux ou
trois questions à ton client » — la façon naturelle de décrire la
qualification, donc le cœur du gabarit D.

🔴 CE QUE CE FICHIER PROTÈGE VRAIMENT, ET POURQUOI IL EXISTE.

La première version du resserrage a été mesurée avec `re.search` sur le texte
brut. Elle montrait 5/5 et 0/7 : parfait. Elle était FAUSSE. Le vrai chemin
passe par `_find_matches`, qui fait `body.lower()` ET `re.IGNORECASE` — deux
raisons indépendantes pour qu'une classe `[A-ZÀ-Ü]` n'y matche jamais. Le
motif « resserré » y aurait été mort et la garde entièrement désarmée, en
silence, avec une mesure verte à l'appui.

D'où les deux règles que ce fichier applique :

1. **Tout passe par la fonction de check**, jamais par un `re.search` local.
   Un test qui court-circuite le chemin réel ne teste pas la garde.
2. **Deux contrôles de mutation** vérifient que les assertions ont des dents :
   relâcher le motif DOIT refaire échouer les innocentes, et router les motifs
   par `_find_matches` DOIT les refaire échouer aussi. Sans eux, un test vert
   ne distingue pas « la garde marche » de « la garde ne fait plus rien ».
"""
from __future__ import annotations

import pytest

from src.lib import compliance_checks as cc

# De vraies preuves sociales : un nombre, un nom commun, un LIEU.
VRAIES_PREUVES = [
    "j'ai fait ça pour deux entreprises à Laval",
    "trois contracteurs à Montréal l'utilisent",
    "deux clients à Sherbrooke m'ont dit la même chose",
    "deux cliniques à Trois-Rivières",
    "trois shops à Gatineau sont déjà dessus",
    # 🔴 Majuscule de DÉBUT DE PHRASE, venue d'un test préexistant de
    # `test_compliance_checks.py`. Le motif sensible à la casse ne
    # reconnaissait plus « Deux » au premier jet — d'où le `(?i:...)`.
    "Deux cliniques à Montréal utilisent déjà",
]

# Des tournures honnêtes. Aucune ne prétend quoi que ce soit sur des clients.
TOURNURES_HONNETES = [
    "il pose deux ou trois questions à ton client",
    "ça change deux choses à ton entreprise",
    "deux minutes à répondre au lieu de deux heures",
    "t'as deux façons à considérer",
    "trois affaires à régler avant l'hiver",
    "il te reste deux semaines à attendre",
    "deux ou trois clics à faire",
]


def _bloque(phrase: str) -> bool:
    """Le VRAI chemin. Pas un `re.search` — c'est le piège qu'on a failli poser."""
    resultat = cc.check_fake_social_proof(f"Bonjour,\n\n{phrase}", social_proof_count=0)
    return not resultat.passed


@pytest.mark.parametrize("phrase", VRAIES_PREUVES)
def test_une_vraie_preuve_sociale_reste_bloquee(phrase: str) -> None:
    assert _bloque(phrase), f"preuve sociale laissée passer : {phrase!r}"


@pytest.mark.parametrize("phrase", TOURNURES_HONNETES)
def test_une_tournure_honnete_passe(phrase: str) -> None:
    assert not _bloque(phrase), (
        f"tournure honnête bloquée : {phrase!r} — sévérité `block`, donc contact gelé à vie"
    )


def test_la_severite_reste_bloquante() -> None:
    """Le resserrage NE DOIT PAS être une dégradation en `info`.

    On a rétréci ce que la garde attrape ; on n'a pas touché à ce qu'elle fait
    quand elle attrape. Une preuve sociale inventée reste un mensonge
    vérifiable, donc elle tue le brouillon.
    """
    r = cc.check_fake_social_proof(
        "Bonjour,\n\nj'ai fait ça pour deux entreprises à Laval", social_proof_count=0
    )
    assert not r.passed
    assert r.severity == "block"


def test_mutation_le_motif_relache_refait_echouer_les_honnetes(monkeypatch) -> None:
    """Contrôle négatif nº1 : sans la classe majuscule, les 7 retombent.

    Si ce test passait avec un motif relâché, c'est que les assertions
    au-dessus ne mesurent rien.
    """
    monkeypatch.setattr(
        cc,
        "SOCIAL_PROOF_PATTERNS_CASSE",
        {r"\bdeux .{0,30} à": "muté", r"\btrois .{0,30} à": "muté"},
    )
    assert all(_bloque(p) for p in TOURNURES_HONNETES), (
        "le motif relâché ne rebloque pas les tournures honnêtes — "
        "le test ne prouve donc rien sur le motif resserré"
    )


def test_mutation_find_matches_detruirait_la_garde() -> None:
    """Contrôle négatif nº2 : la raison d'être de `_find_matches_casse`.

    `_find_matches` baisse la casse et passe `re.IGNORECASE`. Router les motifs
    sensibles à la casse par lui les rend équivalents à la version relâchée :
    les 7 tournures honnêtes redeviennent bloquantes. C'est la preuve que le
    dictionnaire séparé n'est pas une coquetterie.
    """
    honnetes_bloquees = sum(
        1 for p in TOURNURES_HONNETES if cc._find_matches(p, cc.SOCIAL_PROOF_PATTERNS_CASSE)
    )
    assert honnetes_bloquees == len(TOURNURES_HONNETES), (
        "si `_find_matches` respectait la casse, le dictionnaire séparé serait "
        "inutile — revérifier `_find_matches_casse` avant de le supprimer"
    )


def test_find_matches_casse_ne_baisse_pas_la_casse() -> None:
    """La garde tient à ça, autant l'affirmer directement."""
    assert cc._find_matches_casse("deux entreprises à Laval", cc.SOCIAL_PROOF_PATTERNS_CASSE)
    assert not cc._find_matches_casse("deux entreprises à laval", cc.SOCIAL_PROOF_PATTERNS_CASSE)


def test_l_apostrophe_courbe_reste_normalisee() -> None:
    """`_find_matches_casse` perd le `.lower()`, PAS la normalisation d'apostrophe.

    C'est le défaut que la docstring de `_find_matches` raconte : un U+2019
    venu d'un copier-coller désarme un motif sans que rien ne le signale.
    """
    courbe = "comme la boîte que j’accompagne"
    assert cc._find_matches(courbe, cc.SOCIAL_PROOF_PATTERNS), "apostrophe courbe non normalisée"
    assert cc._find_matches_casse(
        "j’ai vu deux entreprises à Laval", cc.SOCIAL_PROOF_PATTERNS_CASSE
    )


def test_le_sujet_est_couvert_aussi() -> None:
    """`check_subject_fake_social_proof` doit câbler le même dictionnaire.

    Il a sa propre fonction et son propre appel : l'oublier laisserait la garde
    active sur le corps et morte sur le sujet.
    """
    assert not cc.check_subject_fake_social_proof("deux entreprises à Laval", 0).passed
    assert cc.check_subject_fake_social_proof("deux questions à ton client", 0).passed
