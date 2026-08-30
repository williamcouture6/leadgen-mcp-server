"""Le prompt du rédacteur dicte-t-il EXACTEMENT le texte mesuré ?

Le gabarit vit à deux endroits : dans les fixtures (où il est mesuré) et dans
le prompt (où il est dicté au modèle). S'ils divergent, le modèle écrit des
phrases que personne n'a comptées, et les bornes de longueur ne veulent plus
rien dire — sans qu'aucun test métier ne bronche.

Ce fichier est le pont entre les deux.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.fixtures import corps_ac1 as f

PROMPT = (
    Path(__file__).resolve().parents[1] / "src" / "prompts" / "reacti" / "personalize.md"
).read_text(encoding="utf-8")


def _mots(texte: str) -> str:
    """Effondre les blancs. Le gabarit du prompt porte des trous
    (`{QUESTION_1}`) qui deplacent les retours de ligne : comparer la mise en
    page ferait echouer le test sur du vent. Ce sont les MOTS qui doivent etre
    identiques des deux cotes."""
    return re.sub(r"\s+", " ", texte)


PROMPT_MOTS = _mots(PROMPT)
CORPS_MOTS = {nom: _mots(c) for nom, c in f.TOUS_LES_CORPS.items()}


# ---------------- Le texte fixe est le même des deux côtés ----------------

PHRASES_FIXES = [
    # Le bloc central, écrit par William.
    "Moi c'est William, et je fais en sorte de régler ces problèmes-là.",
    "système qui répond à tout ce qui rentre en moins de 60 secondes",
    # Les deux chutes, une par angle.
    "Le système\nreste actif 24/7, le soir, la fin de semaine, quand t'es pas disponible.",
    "Avec ce\nsystème tu réponds plus vite au client, et ça t'évite de le rappeler à 18h",
    # Les deux ancres, avec et sans citation.
    "Du monde qui te cherche,\nt'en as. La question c'est combien tu en échappes dans une semaine.",
    "c'est probablement pas parce que le monde t'aime pas",
    # Le CTA et la ligne de renvoi.
    "Dis-moi juste si tu veux le voir.",
    "Si c'est pas toi qui gères ça, tu peux-tu me pointer la bonne personne?",
    # Les fermetures de relances.
    "Pour le site, l'offre tient toujours. Juste à me dire.",
    "Tu veux-tu la voir? T'as juste à me dire.",
]


@pytest.mark.parametrize("phrase", PHRASES_FIXES)
def test_le_prompt_dicte_la_phrase_fixe(phrase: str) -> None:
    """Une phrase du gabarit absente du prompt est une phrase que le modèle
    va réinventer — donc une phrase jamais mesurée."""
    assert _mots(phrase) in PROMPT_MOTS, f"absente du prompt : {phrase[:60]!r}"


@pytest.mark.parametrize("phrase", PHRASES_FIXES)
def test_la_phrase_fixe_est_bien_dans_un_corps_mesure(phrase: str) -> None:
    """Le contrôle symétrique : une phrase dictée par le prompt mais absente
    de toute fixture n'a jamais été comptée non plus."""
    assert any(_mots(phrase) in corps for corps in CORPS_MOTS.values()), (
        f"dictee par le prompt mais dans aucun corps mesure : {phrase[:60]!r}"
    )


# ---------------- Ce que le prompt ne doit PLUS contenir ----------------

@pytest.mark.parametrize(
    "vestige,pourquoi",
    [
        ("{{DEMO_URL}}", "le pivot tri du 2026-08-20 a retiré tout lien du courriel froid"),
        ("réactivation", "l'offre REACTI est dissoute depuis le pivot du 2026-06-07"),
        ("commission", "on ne vend plus à la commission"),
        ("Vouvoiement", "la piste agence-ia tutoie"),
        ("Cal.com", "le courriel de tri ne propose plus de créneau"),
    ],
)
def test_le_prompt_na_plus_le_vestige(vestige: str, pourquoi: str) -> None:
    assert vestige not in PROMPT, f"{vestige!r} traîne encore : {pourquoi}"


@pytest.mark.parametrize(
    "interdiction",
    [
        "AUCUNE SIGNATURE dans le corps",
        "AUCUN LIEN",
        "Le mot « IA » n'apparaît jamais",
        "Aucune preuve sociale",
        "Aucun tiret cadratin",
        "Le CTA demande UNIQUEMENT le oui",
        # « au goût du jour » ne doit exister dans le prompt QUE comme
        # interdiction : la formule présume que son site est démodé (jamais
        # regardé) et ment aux 97 entreprises qui n'en ont pas.
        "Ne JAMAIS écrire « ton site au goût du jour »",
    ],
)
def test_le_prompt_porte_linterdiction(interdiction: str) -> None:
    """Ces six-là sont celles qu'un modèle enfreint tout seul s'il ne les lit
    pas : il signe, il met un lien, il flatte, il propose un rendez-vous."""
    assert interdiction in PROMPT, f"interdiction absente : {interdiction!r}"


def test_le_prompt_exige_A_ou_B_jamais_AB() -> None:
    """`template_choice="AB"` fait générer les deux variantes, mais la colonne
    `messages.template_choice` stockerait « AB » sur 100 % des lignes : pas de
    test, juste deux textes et aucune trace de qui a reçu quoi."""
    assert 'jamais `"AB"`' in PROMPT


def test_le_prompt_interdit_darrondir_la_note() -> None:
    """Un contrôle déterministe compare le chiffre à la colonne et bloque au
    moindre écart. Le modèle doit le savoir, sinon il « améliore »."""
    assert "recopient au caractère près" in PROMPT
    assert "plus de 40 avis" in PROMPT


def test_le_prompt_dicte_les_deux_formulations_du_deuxieme_temps() -> None:
    """« Pour le reste de l'année » est FAUX quand les autres métiers sont dans
    la même saison. Les deux formes doivent être données, avec leur condition."""
    assert "Pour le reste de l'année" in PROMPT
    assert "aussi. »" in PROMPT or "aussi.»" in PROMPT
    assert "meme_saison" in PROMPT


def test_le_prompt_a_les_cinq_suppositions() -> None:
    for numero in ("1 · ", "2 · ", "3 · ", "4 · ", "5 · "):
        assert numero in PROMPT, f"supposition {numero!r} absente du catalogue"
