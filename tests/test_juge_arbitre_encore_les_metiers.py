"""Le juge REÇOIT la liste des métiers — il ne la reconstitue plus.

🔴 CE FICHIER A ÉTÉ RETOURNÉ LE 2026-09-18, et son histoire est la leçon.

**Version du 2026-09-17.** Il exigeait que le prompt du juge RÉENSEIGNE chaque
règle du dictionnaire : la table libellé → famille, les quatre exclusions
(gazon en rouleau, clôture de piscine, punaises de gazon, nettoyage de toit),
les exigences de `toiture` et `piscine`. La raison était juste à l'époque :

    « le juge ne reçoit JAMAIS la sortie de `lib/metiers` ; il est le SEUL
      contrôle qui lit les métiers nommés ; le désarmer revenait à n'avoir
      plus rien. »

**Ce qui a changé.** Le bloc « Faits vérifiés » — déjà servi au rédacteur ET au
juge — porte désormais `- Métiers reconnus : …`, produit par
`lib/metiers.metiers_nommables`. La prémisse « il ne reçoit jamais la sortie du
dictionnaire » est tombée le jour même.

**Pourquoi le retournement était nécessaire, et pas seulement possible.**
Recopier le dictionnaire dans un prompt crée DEUX vérités. Entre le 15 et le
18 septembre, chaque phrase ajoutée a réglé un cas en en dérèglant un autre —
et trois faux positifs mesurés en sont sortis :

  · « tonte » refusée chez *Entretien V Boudreault* (« Entretien de gazon »),
    TROIS brouillons, trois refus, la même phrase, contact presque gelé ;
  · « pavage » refusée chez *Scellant Déneigement XTRA* (scellant de revêtement) ;
  · « ménage » refusée chez *Panorama services* (« Entretien ménager »).

Ce fichier garde maintenant l'inverse : **aucune règle du dictionnaire ne doit
revenir dans le prompt**, et la liste doit vraiment atteindre le juge.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.lib.avis import bloc_faits_verifies
from src.lib.metiers import EXCLUSIONS, EXIGE, metiers_nommables

PROMPT = (
    Path(__file__).resolve().parent.parent / "src" / "prompts" / "compliance.md"
).read_text(encoding="utf-8")


# ── Le prompt ne doit plus contenir le dictionnaire ─────────────────────────


@pytest.mark.parametrize("famille", sorted(EXCLUSIONS))
def test_aucune_exclusion_n_est_recopiee_dans_le_prompt(famille: str) -> None:
    """🔴 L'INVERSE EXACT DE CE QUE CE TEST EXIGEAIT HIER.

    Une exclusion recopiée dans le prompt est une seconde vérité : le jour où
    `lib/metiers` change, le prompt dit X et la liste dit Y — et c'est le
    prompt que le juge lit en premier.

    La protection n'est pas perdue, elle est déplacée : une famille exclue
    n'entre pas dans `metiers_nommables`, donc elle est hors liste, donc le
    juge la refuse mécaniquement. Vérifié plus bas.
    """
    for libelle in EXCLUSIONS[famille]:
        assert libelle not in PROMPT.lower(), (
            f"« {libelle} » est revenu dans le prompt du juge. Le dictionnaire "
            "ne se recopie pas : il se SERT, par le bloc « Faits vérifiés »."
        )


# ⚠️ CERTAINS MOTS D'EXIGENCE SONT DES MOTS FRANÇAIS COURANTS — « entretien »,
# « nettoyage », « installation », « réparation ». Leur simple présence dans le
# prompt ne prouve RIEN : « Entretien de gazon » y figure comme exemple
# historique, pas comme règle. Une première version de ce test les cherchait
# tous et accusait cet exemple.
#
# On ne teste donc que les mots DISTINCTIFS : ceux qu'on n'écrit pas par
# hasard, et dont la présence signale vraiment une règle recopiée.
# ⚠️ LES ACCENTS. `EXIGE` porte les mots SANS accents (il compare du texte
# deja normalise) ; le prompt, lui, s'ecrit en francais. Comparer les deux
# directement rendait « refection » et « elastomere » INCAPABLES de matcher
# « réfection » et « élastomère » : deux des huit mots ne pouvaient jamais
# rougir. Releve par un conseil de relecture le 2026-09-19.
def _sans_accents(t: str) -> str:
    import unicodedata

    plat = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in plat if not unicodedata.combining(c))


PROMPT_NU = _sans_accents(PROMPT)

_MOTS_DISTINCTIFS = frozenset({
    "bardeau", "membrane", "elastomere", "refection", "couvreur",
    "toiture neuve", "hebdomadaire", "saisonnier",
})


@pytest.mark.parametrize("famille", sorted(EXIGE))
def test_aucune_exigence_DISTINCTIVE_n_est_recopiee_dans_le_prompt(
    famille: str,
) -> None:
    """Les mots qui PROUVENT un métier (« bardeau », « membrane » pour
    `toiture`) n'ont rien à faire dans le prompt : ils décident de la liste, et
    la liste suffit au juge."""
    revenus = [
        m for m in EXIGE[famille]
        if m in _MOTS_DISTINCTIFS and _sans_accents(m) in PROMPT_NU
    ]
    assert not revenus, (
        f"les mots d'exigence de « {famille} » sont revenus dans le prompt : "
        f"{revenus}. Le dictionnaire ne se recopie pas, il se sert."
    )


def test_la_liste_des_mots_distinctifs_couvre_bien_les_exigences() -> None:
    """Le témoin du test ci-dessus : si `EXIGE` gagne une famille dont AUCUN
    mot n'est distinctif, le test du dessus devient vide et vert pour rien."""
    for famille, mots in EXIGE.items():
        assert set(mots) & _MOTS_DISTINCTIFS, (
            f"aucun mot distinctif pour « {famille} » : le test ne vérifie "
            "plus rien sur cette famille"
        )


def test_le_prompt_renvoie_le_juge_au_BLOC_pas_a_sa_memoire() -> None:
    """La contre-épreuve des deux tests du dessus : sans elle, SUPPRIMER toute
    mention des métiers les rendrait verts."""
    assert "Métiers reconnus" in PROMPT, (
        "le prompt ne dit plus au juge où trouver la liste"
    )
    assert "NE REFAIS PAS LE CLASSEMENT" in PROMPT
    assert "n'est **JAMAIS** une invention" in PROMPT
    assert "hors de cette liste" in PROMPT


def test_une_famille_de_trop_se_corrige_en_reecrivant() -> None:
    """⚠️ Un métier mal nommé n'est pas un mensonge sur un fait : il se corrige
    à la plume. En `blocked`, il coûterait le contact sans réécriture
    (`_patch_verdict_conformite` ne renvoie que les `needs_revision`)."""
    assert "`needs_revision`, jamais en `blocked`" in PROMPT


# ── La protection est bien passée dans le dictionnaire ──────────────────────


@pytest.mark.parametrize(
    ("cas", "services", "famille_interdite"),
    [
        # Les quatre cas que le prompt énumérait hier, un par un.
        ("poser de la tourbe n'est pas tondre",
         ["Plantations (arbres, arbustes, gazon en rouleau)"], "tonte"),
        ("poser une clôture n'est pas entretenir une piscine",
         ["Installation de clôtures de piscine"], "piscine"),
        ("les punaises de gazon ne sont pas de l'extermination",
         ["Tonte de gazon", "Traitement des punaises de gazon"], "extermination"),
        ("nettoyer un toit n'est pas de la toiture",
         ["Lavage de vitres", "Nettoyage de toitures"], "toiture"),
    ],
)
def test_la_famille_interdite_n_entre_pas_dans_la_liste(
    cas: str, services: list[str], famille_interdite: str
) -> None:
    """🔴 C'EST ICI QUE LA PROTECTION VIT MAINTENANT.

    Le prompt ne dit plus ces règles — le dictionnaire les applique, et la
    liste en est le résultat. Hors liste = le juge refuse. Ce test prouve que
    le retrait du prompt n'a rien ouvert.
    """
    assert famille_interdite not in metiers_nommables(services), cas


@pytest.mark.parametrize(
    ("cas", "note", "avis"),
    [
        # 🔴 LA BRANCHE A SORTIE ANTICIPEE EST LA PLUS IMPORTANTE : une ligne
        # ajoutee au `return` final ne l'atteint pas. C'est le piege qui a ete
        # trouve en cablant les metiers, et un conseil de relecture a releve
        # que le test d'origine ne l'exercait PAS — retirer les metiers de
        # cette branche laissait tout vert.
        ("aucun avis (sortie anticipée)", None, None),
        ("sous le plancher de qualité", 3.2, 4),
        ("note citable", 4.8, 47),
        ("note sans compte", 4.8, None),
        ("compte sans note", None, 47),
    ],
)
def test_la_liste_arrive_dans_TOUTES_les_branches_du_bloc(
    cas: str, note: float | None, avis: int | None
) -> None:
    """Le dernier maillon : la liste doit vraiment arriver dans le texte,
    quelle que soit la situation d'avis de l'entreprise."""
    bloc = bloc_faits_verifies(
        note, avis, metiers_nommables=metiers_nommables(
            ["Entretien de gazon", "Déneigement résidentiel"]
        ),
    )
    assert "Métiers reconnus" in bloc, cas
    assert "tonte" in bloc and "déneigement" in bloc, cas
    assert "n'est JAMAIS une" in bloc, cas


def test_le_bloc_dit_explicitement_quand_il_n_y_a_AUCUN_metier() -> None:
    """⚠️ Le silence se lirait « pas encore cherché », et le juge comblerait —
    même raison que pour « aucune note en base »."""
    bloc = bloc_faits_verifies(4.8, 47, metiers_nommables=())
    assert "AUCUN" in bloc
    assert "Tout métier" in bloc


def test_la_liste_ne_depend_pas_de_la_DATE() -> None:
    """🔴 CE QUI REND LE RECALCUL CÔTÉ JUGE SÛR — et le test l'a prouvé par
    accident pendant une heure.

    Le juge relit parfois un brouillon écrit six jours plus tôt. Si la liste
    dépendait de la date, il lirait une liste que le rédacteur n'avait pas —
    exactement le défaut du nom d'entreprise, corrigé la veille.

    ⚠️ LA PREMIÈRE VERSION DE CE TEST ÉTAIT TAUTOLOGIQUE, relevée par un
    conseil de relecture. Elle appelait l'ALIAS avec trois dates — or l'alias
    jette son paramètre `aujourdhui` (c'est tout son propos). Les trois appels
    étaient le même appel : `metiers_mentionnables(services, "PAS UNE DATE")`
    rendait le résultat normal. Il ne pouvait plus rougir, et il gardait le
    fait le plus important du design.

    On vérifie donc la PROPRIÉTÉ à la source : aucune date n'entre dans le
    chemin de calcul.
    """
    import ast
    import inspect
    from pathlib import Path

    from src.lib import metiers as mod

    # 1. la signature ne prend pas de date
    params = inspect.signature(metiers_nommables).parameters
    assert "aujourdhui" not in params and "date" not in params, (
        f"une date est entrée dans la signature : {list(params)}"
    )

    # 2. le corps n'appelle rien qui lise l'horloge, ni directement ni par
    #    `resoudre_metiers` (qui, lui, prend une date).
    source = Path(mod.__file__).read_text(encoding="utf-8")
    arbre = ast.parse(source)
    corps = next(
        n for n in ast.walk(arbre)
        if isinstance(n, ast.FunctionDef) and n.name == "metiers_nommables"
    )
    appels = {
        n.func.id for n in ast.walk(corps)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "resoudre_metiers" not in appels, (
        "`metiers_nommables` appelle `resoudre_metiers`, qui prend une date : "
        "la liste peut désormais changer entre l'écriture et le jugement"
    )
    assert not (appels & {"today", "now", "date", "datetime"}), appels

    # 3. et la preuve par l'usage, sur des saisons opposées
    services = ["Entretien de gazon", "Déneigement résidentiel", "Lavage de vitres"]
    assert metiers_nommables(services) == metiers_nommables(services)


def test_l_alias_du_redacteur_rend_la_MEME_liste_que_lib() -> None:
    """Les deux acteurs doivent lire la même chose. L'alias existe pour ne pas
    casser quatre appelants ; s'il divergeait, le rédacteur et le juge
    verraient deux listes."""
    from src.tools.personalize import metiers_mentionnables

    for services, industry in (
        (["Lavage de vitres", "Nettoyage de toitures"], None),
        (["Excavation sur mesure (fondations, piscines creusées)"], None),
        (["Entretien de gazon", "Déneigement"], None),
        ([], "entrepreneur en déneigement"),
    ):
        assert (
            metiers_mentionnables(services, date(2026, 9, 18), industry)
            == metiers_nommables(services, industry)
        ), services


# ── Le maillon sans filet, relevé par un conseil de relecture ───────────────


def test_le_juge_VOIT_VRAIMENT_la_liste_dans_son_message() -> None:
    """🔴 LE TROU QUE TOUS LES AUTRES TESTS LAISSAIENT.

    Ils assertent sur `bloc_faits_verifies` directement. Mais entre le bloc et
    le juge il y a `_message_utilisateur_juge`, qui doit lui passer la liste —
    et **rien ne le vérifiait**. Si le kwarg `metiers_nommables=` disparaissait
    de `tools/compliance.py`, les 2250 tests restaient verts et le juge
    redevenait aveugle : exactement l'état qu'on vient de corriger.

    C'est la même classe de trou que `test_le_juge_recoit_VRAIMENT_le_nom`
    avait fermée pour le nom d'entreprise, la veille. On la ferme ici aussi.
    """
    from src.tools.compliance import _message_utilisateur_juge

    msg = _message_utilisateur_juge(
        body="corps",
        subject="objet",
        research_json={
            "services_offered": ["Entretien de gazon", "Déneigement résidentiel"]
        },
        social_proof=[],
        metier_scene="déneigement",
    )
    assert "Métiers reconnus" in msg, (
        "le juge ne reçoit PAS la liste : le câblage de "
        "`_message_utilisateur_juge` est rompu"
    )
    assert "tonte" in msg, "la liste arrive vide ou fausse"
    assert "Métier de la première ligne : **déneigement**" in msg, (
        "la scène n'arrive pas au juge : il reprochera un « cadrage inversé »"
    )
    # Le témoin : sans lui, un message vide passerait les assertions du dessus
    # si elles étaient écrites autrement.
    assert "Faits vérifiés" in msg and "corps" in msg


def test_le_juge_recoit_le_marquage_du_metier_du_SECTEUR() -> None:
    """📏 *S.O.S Mini Excavation* — services d'excavation, mot-clé Google
    « entrepreneur en déneigement ». Le code ajoute `déneigement` pour qu'elle
    reste joignable (décision William, 2026-09-02) ; le juge l'avait BLOQUÉE le
    2026-09-17 : « aucun service de déneigement dans le research_json ».

    Décision William, 2026-09-18 : on le MARQUE et on l'accepte.
    """
    from src.tools.compliance import _message_utilisateur_juge

    msg = _message_utilisateur_juge(
        body="corps",
        subject="objet",
        research_json={"services_offered": ["Mini-excavation", "Terrassement"]},
        social_proof=[],
        industry="entrepreneur en déneigement",
    )
    assert "mot-clé de sourcing" in msg, (
        "le marquage du métier venu du secteur n'arrive pas : le juge "
        "re-bloquera S.O.S Mini Excavation"
    )


def test_sans_service_le_juge_est_prevenu_qu_il_n_y_a_AUCUN_metier() -> None:
    """⚠️ Le silence se lirait « pas encore cherché », et le juge comblerait —
    même raison que pour « aucune note en base »."""
    from src.tools.compliance import _message_utilisateur_juge

    msg = _message_utilisateur_juge(
        body="corps", subject="objet",
        research_json={"services_offered": []}, social_proof=[],
    )
    assert "Métiers reconnus : **AUCUN**" in msg
