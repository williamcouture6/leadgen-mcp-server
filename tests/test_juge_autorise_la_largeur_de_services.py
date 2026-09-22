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

from pathlib import Path

from src.tools.compliance import _PROMPT_PATH

PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# Le gabarit du rédacteur — la SOURCE de la forme que le juge doit accepter.
GABARIT = (
    Path(__file__).resolve().parent.parent
    / "src" / "prompts" / "reacti" / "personalize.md"
).read_text(encoding="utf-8")

# Les mots par lesquels ce prompt condamne quelque chose. S'ils apparaissent sur
# la même ligne qu'une forme imposée par le gabarit, le juge lira la
# condamnation — c'est le plus littéral qui gagne.
_CONDAMNATIONS = (
    "c'est faux",
    "est faux",
    "erreur de fait",
    "reste une erreur",
    "violation",
    "signale-le",
    "à bloquer",
)

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
    """Le bloc §1terdecies ENTIER, borné par ses voisins réels.

    ⚠️ ASSERTION D'ANCRE OBLIGATOIRE. Une première version calculait la fenêtre
    avec `section.find(...)` sans vérifier la présence : `find` rend -1 quand
    l'ancre manque, `max(0, -1 - 2000)` rend 0, et la « fenêtre » devenait la
    section ENTIÈRE — le test de la réserve passait au vert AVANT que la
    permission existe.

    ⚠️ ET LES BORNES SONT RÉELLES, PLUS UN COMPTE DE CARACTÈRES. La deuxième
    version prenait « l'ancre + 2000 caractères ». Un conseil l'a mesuré le
    2026-09-22 : la réserve la plus importante tombait **10 caractères** hors
    fenêtre, et n'importe quel ajout de ~350 caractères plus haut faisait
    rougir le test sans que la réserve ait bougé. Un bloc se borne par ce qui
    le suit, jamais par sa longueur supposée.
    """
    section = _section_legitime()
    depart = section.find("1terdecies.")
    assert depart != -1, (
        "la permission §1terdecies n'est plus dans la liste LÉGITIME : les "
        "tests qui suivent n'ont aucun ancrage et ne prouveraient rien"
    )
    fin = section.find("\n2. ", depart)
    assert fin != -1, (
        "la permission suivante (« 2. ») a disparu : le bloc n'est plus borné, "
        "la fenêtre déborderait sur le reste du prompt"
    )
    return section[depart:fin]


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
    # 🔴 `services_offered`, PAS `research_json`. Un conseil l'a mesuré le
    # 2026-09-22 : le `research_json` est le blob ENTIER — il porte
    # `personalization_hooks` (dont l'exemple canonique du prompt de recherche
    # est « mentionne le service d'urgence 24/7 »), les citations d'avis, les
    # `pain_points`. Une réserve ancrée dessus valide littéralement tout ce que
    # le rédacteur y pioche : elle ne filtre RIEN. L'énumération doit venir des
    # libellés de `services_offered`, et de rien d'autre.
    assert "services_offered" in fenetre, (
        "la permission n'ancre pas l'énumération sur `services_offered` — "
        "ancrée sur le `research_json` entier, elle autorise un service tiré "
        "d'un hook ou d'une citation d'avis, donc elle ne filtre rien"
    )
    assert "égalité littérale" in fenetre, (
        "la permission n'interdit pas d'exiger l'égalité littérale : le juge "
        "signalerait alors « épandage d'abrasifs » écrit pour un libellé "
        "« sablage », alors que le rédacteur a consigne d'écrire dans SES mots"
    )


def test_la_forme_mixte_du_gabarit_n_est_jamais_declaree_fausse() -> None:
    """🔴 LE DÉFAUT QUE CE FICHIER A LUI-MÊME INTRODUIT, LE 2026-09-22.

    La première version de §1terdecies « réservait » le cas de Quinn en citant
    « autant de sortes de déneigement […] que de la mini-excavation » comme une
    erreur de fait. Or c'est l'exemple **✅ de William**, au mot près
    (`personalize.md`, section de l'énumération courte), la forme que
    `personalize.py` FABRIQUE, et elle était déjà permise en toutes lettres
    dans la section « NE PAS RE-CHECKER » du juge.

    C'était la troisième récidive du même défaut dans ce dépôt — citer notre
    propre copie comme exemple canonique à refuser — après le « 78 % » du
    2026-09-01 et le pied de page du site du 2026-09-08. Voir
    `test_juge_autorise_le_bloc_site.py`, qui existe pour ça.

    ⚠️ Ce test lit la forme depuis le GABARIT, pas depuis un souvenir d'elle :
    si William change son exemple, c'est la nouvelle forme qui sera protégée.
    """
    assert "autant de sortes de" in GABARIT, (
        "la forme de l'énumération courte a disparu du gabarit — ce test "
        "protège désormais du vide, vérifier ce qui l'a remplacée"
    )
    for ligne in PROMPT.splitlines():
        if "autant de sortes de" not in ligne:
            continue
        basse = ligne.lower()
        coupable = [mot for mot in _CONDAMNATIONS if mot in basse]
        assert not coupable, (
            "le prompt du juge condamne une forme IMPOSÉE par le gabarit "
            f"(mots trouvés : {coupable}). Le juge refusera alors les "
            f"brouillons conformes : {ligne.strip()[:160]}"
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
