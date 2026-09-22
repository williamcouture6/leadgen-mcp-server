"""Le juge ne doit pas refuser « on comprend que tu en couvres beaucoup! »
quand l'entreprise n'a qu'UN métier mais plusieurs services.

🔴 LE DÉFAUT, MESURÉ SUR LA PRODUCTION DU 2026-09-21.

Trois brouillons sur treize sont sortis `needs_revision` ce matin-là. Deux
d'entre eux — Déneigement RTM et Déneigement Papineau — portaient un seul
métier (`déneigement`) et plusieurs services réels :

    RTM       : résidentiel, commercial, manuel, épandage d'abrasifs,
                transport de neige, saisonnier complet, ponctuel  (7)
    Papineau  : ruelles, stationnements, urgence 24h, contrats saisonniers,
                sur appel                                          (5)

Le juge a écrit, mot pour mot : « La chute "on comprend que tu en couvres
beaucoup" est inexacte pour une entreprise à métier unique (déneigement) — le
reste du courriel est conforme. » Il appliquait §1 (« toute affirmation
factuelle sur l'ENTREPRISE doit être ancrée ») à une phrase FIXE du gabarit,
et il jugeait la largeur en MÉTIERS pendant que le rédacteur la juge en
SERVICES (`avis.py:_consigne_de_repli`, bascule `nb_services >= 2`).

🔴 DÉCISION DE WILLIAM, 2026-09-22 : la phrase est VRAIE pour eux. Couvrir le
résidentiel, le commercial, le manuel, l'épandage et le transport, c'est
couvrir beaucoup — même si tout ça s'appelle « déneigement ». C'est donc le
juge qui se détend, pas le rédacteur qui change de version.

⚠️ CE QUE CETTE DÉROGATION NE COUVRE PAS, et c'est la moitié qui compte. Elle
autorise la CONCLUSION (« tu en couvres beaucoup ») sur une liste de services
d'un seul métier. Elle n'autorise RIEN sur le contenu de la liste : un service
qui ne figure pas dans le `research_json` reste une invention, et une
énumération qui rattache le métier Y aux « sortes de X » reste une erreur de
fait. C'est exactement le troisième refus du même matin — Quinn, 5 métiers,
dont la phrase accrochait la mini-excavation et l'aménagement paysager aux
« sortes de déneigement ». Celui-là devait être refusé, et doit continuer à
l'être.

📏 Pourquoi ça pressait : 7 des 18 entreprises vivantes et recherchées (39 %)
sont dans ce cas. Et jusqu'en janvier la seule saison ouverte est le
déneigement, dont les entreprises sont par nature mono-métier à sous-services
multiples — la part ne pouvait que monter.

Ces tests ne valent que ce que vaut l'obéissance d'un modèle. Ce qu'ils
protègent, c'est que le prompt cesse de se contredire : une permission
abstraite perd toujours contre un exemple littéral qui dit l'inverse.
"""
from __future__ import annotations

from src.tools.compliance import _PROMPT_PATH

PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_ENTETE_LEGITIME = "## LÉGITIME — ne JAMAIS flagger ça"
_ENTETE_A_CHERCHER = "## Ce que tu dois chercher (jugement sémantique uniquement)"


def _section_legitime() -> str:
    """Le bloc des permissions, celui que le juge lit comme « pas ton travail »."""
    assert _ENTETE_LEGITIME in PROMPT, "l'en-tête de la liste LÉGITIME a changé de nom"
    return PROMPT.split(_ENTETE_LEGITIME, 1)[1].split(_ENTETE_A_CHERCHER, 1)[0]


def _section_a_chercher() -> str:
    """Le bloc des violations, celui d'où venait le refus."""
    assert _ENTETE_A_CHERCHER in PROMPT, "l'en-tête des violations a changé de nom"
    return PROMPT.split(_ENTETE_A_CHERCHER, 1)[1]


def _paragraphe_de_la_permission() -> str:
    """Le paragraphe des permissions qui parle de la phrase fixe.

    ⚠️ ASSERTION D'ANCRE OBLIGATOIRE. Une première version de ce fichier
    calculait la fenêtre avec `section.find(...)` sans vérifier la présence :
    `find` rend -1 quand l'ancre manque, `max(0, -1 - 2000)` rend 0, et la
    « fenêtre » devenait la section ENTIÈRE. Le test de la réserve passait donc
    au vert AVANT que la permission existe — un test vide, vert pour toujours.
    """
    section = _section_legitime()
    depart = section.find("couvres beaucoup")
    assert depart != -1, (
        "la phrase fixe n'est pas dans la liste des permissions : les tests de "
        "fenêtre qui suivent n'ont aucun ancrage et ne prouveraient rien"
    )
    # Du début de son paragraphe jusqu'à la fin du suivant : une permission de
    # ce fichier tient en un bloc, réserves comprises.
    avant = section.rfind("\n\n", 0, depart)
    return section[avant if avant != -1 else 0:depart + 2000]


def test_la_permission_est_dans_la_liste_legitime() -> None:
    """La phrase fixe doit être nommée parmi les permissions, pas ailleurs.

    Elle existait déjà §2 de la section des violations (« ne confonds pas avec
    "On comprend que…" »), mais là elle sert à écarter une AUTRE faute (la
    preuve sociale). Le refus du 2026-09-21 venait de §1, les faits non
    ancrés — que rien ne couvrait.
    """
    section = _section_legitime()
    assert "couvres beaucoup" in section, (
        "la phrase « on comprend que tu en couvres beaucoup! » n'est nommée "
        "nulle part dans la liste des permissions : le juge n'a aucune raison "
        "de cesser de la refuser au titre des faits non ancrés (§1)"
    )


def test_la_permission_dit_explicitement_le_cas_du_metier_unique() -> None:
    """Une permission qui ne nomme pas le cas refusé ne le débloque pas.

    C'est le cœur : le juge n'a pas refusé la phrase en général, il l'a refusée
    « pour une entreprise à métier unique ». Une permission muette sur ce point
    le laisse recommencer demain matin.
    """
    fenetre = _paragraphe_de_la_permission()
    assert "métier" in fenetre and "unique" in fenetre.lower(), (
        "la permission ne dit pas que le cas du MÉTIER UNIQUE est couvert — "
        "or c'est le motif exact des refus de RTM et Papineau"
    )


def test_la_permission_reserve_l_exactitude_de_la_liste() -> None:
    """La dérogation doit s'arrêter à la conclusion, pas couvrir l'énumération.

    Sans cette réserve, on n'a pas détendu le juge : on l'a désarmé. Le refus
    de Quinn (mini-excavation rattachée aux « sortes de déneigement ») doit
    rester possible.
    """
    fenetre = _paragraphe_de_la_permission()
    assert "research_json" in fenetre, (
        "la permission ne rappelle pas que les services énumérés doivent "
        "rester ancrés dans le research_json — elle autoriserait alors une "
        "liste inventée, ce que le refus de Quinn attrapait à raison"
    )


def test_aucun_contre_exemple_ne_cite_la_phrase_comme_violation() -> None:
    """Un exemple littéral bat une permission abstraite.

    C'est la leçon écrite dans `test_juge_autorise_le_bloc_site.py` : le matin
    du 2026-09-08, une permission avait été ajoutée pour le bloc du site
    pendant que §1ter continuait de le donner en exemple de mensonge. C'est
    l'exemple qui avait gagné.
    """
    # Les tournures par lesquelles ce prompt disculpe une phrase fixe. « ne
    # confonds pas » et « ne prétend rien » sont celles qu'emploie §2, qui
    # écarte déjà la phrase sur l'axe de la preuve sociale — une permission
    # véritable, pas un contre-exemple. Les exiger toutes aurait fait rougir ce
    # test sur du texte correct.
    DISCULPATIONS = (
        "ne confonds pas",
        "ne prétend rien",
        "formulation fixe",
        "phrase fixe",
        "ne la signale pas",
        "légitime",
    )
    for ligne in _section_a_chercher().splitlines():
        if "couvres beaucoup" not in ligne:
            continue
        basse = ligne.lower()
        assert any(marque in basse for marque in DISCULPATIONS), (
            "la section des violations cite la phrase fixe sans dire qu'elle "
            f"est permise : {ligne.strip()[:160]}"
        )
