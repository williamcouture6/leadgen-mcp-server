"""Le secteur classe les métiers PARTOUT, pas seulement à la porte de contact.

🔴 CE QUI ÉTAIT CASSÉ, mesuré le 2026-09-14. `industry` complète le classement
depuis le commit du matin même, mais il n'était branché que dans
`fenetre_saisonniere_ouverte` — la porte qui décide À QUI on écrit. Les cinq
autres endroits qui classent des métiers ne le voyaient pas.

« Entreprises Mobile » (0 service listé, secteur « entrepreneur en
déneigement ») était donc, dans le même passage du pipeline :

  · **démarchable**, dit la porte de contact — elle lit le secteur ;
  · **sans métier**, dit le bloc du rédacteur → ouvreur générique ;
  · **privée de C et D**, dit `tete_fixe_servable` → deux bras sur quatre ;
  · **« aucun métier »**, dit le compteur → un reproche à WF-3 qui n'a pas lieu.

Deux vérités sur la même entreprise, au même instant. Aucune n'était fausse
isolément : c'est le CÂBLAGE qui manquait.

⚠️ La cause est écrite noir sur blanc dans l'ancienne docstring de
`resoudre_metiers` : « `industry` est ACCEPTÉ MAIS IGNORÉ ». Elle était vraie
la veille. On lit ça, on conclut « paramètre inerte », on ne le branche pas —
et le commentaire survit au changement qu'il décrit.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

import src.http_api as http_api
from src.lib.gabarits import tete_fixe_servable_pour_entreprise
from src.tools.db import fenetre_saisonniere_ouverte
from src.tools.personalize import bloc_metiers_resolus, metier_de_la_scene

SEPTEMBRE = date(2026, 9, 14)

# Entreprises Mobile, telle qu'en base : aucun service, un secteur qui parle.
SECTEUR_SEUL = {
    "id": "00000000-0000-0000-0000-000000000001",
    "name": "Entreprises Mobile",
    "city": "Montréal",
    "industry": "entrepreneur en déneigement",
    "research_json": {"services_offered": []},
    "google_rating": 5.0,
    "google_reviews_count": 1,
    "track": "agence-ia",
}
MUET = {**SECTEUR_SEUL, "industry": "services de consultation"}


def test_la_porte_de_contact_l_accepte() -> None:
    """Le point de départ : c'est CETTE réponse que les autres devaient suivre."""
    assert fenetre_saisonniere_ouverte(SECTEUR_SEUL, track="agence-ia") is True


def test_le_redacteur_connait_le_metier() -> None:
    """Avant : « Aucun métier reconnu dans `services_offered` » — donc un
    ouvreur générique à une entreprise dont on sait parfaitement le métier."""
    bloc = bloc_metiers_resolus(
        SECTEUR_SEUL["research_json"]["services_offered"],
        SEPTEMBRE,
        gabarit="C",
        industry=SECTEUR_SEUL["industry"],
    )
    assert "déneigement" in bloc
    assert "Aucun métier reconnu" not in bloc


def test_c_et_d_lui_sont_ouverts() -> None:
    """Avant : deux bras sur quatre, pour un métier pourtant connu — et le
    test A/B amputé d'un côté sans que rien ne le dise."""
    assert tete_fixe_servable_pour_entreprise(SECTEUR_SEUL) is True


def test_le_message_enregistre_le_metier() -> None:
    """`messages.metier_scene` dit de quoi le courriel a parlé. Vide, il aurait
    fait compter cette entreprise comme « sans métier » dans la mesure."""
    assert metier_de_la_scene(
        SECTEUR_SEUL["research_json"]["services_offered"],
        SEPTEMBRE,
        SECTEUR_SEUL["industry"],
    ) == "déneigement"


def test_le_compteur_ne_l_accuse_plus() -> None:
    """Ce compteur sert à dire « WF-3 n'a pas assez creusé ». Sur une fiche dont
    le secteur nomme le métier, c'est un reproche sans objet."""
    assert http_api._tombe_sur_le_repli_du_lexique(SECTEUR_SEUL) is False


# ------------------------------------------------- les contrôles négatifs --


@pytest.mark.parametrize("fonction,attendu", [
    (lambda c: fenetre_saisonniere_ouverte(c, track="agence-ia"), False),
    (tete_fixe_servable_pour_entreprise, False),
    (http_api._tombe_sur_le_repli_du_lexique, True),
])
def test_un_secteur_muet_ne_rachete_rien(fonction, attendu) -> None:
    """🔴 LE CONTRÔLE QUI COMPTE : brancher le secteur ne doit pas rendre tout
    le monde classable. « services de consultation » n'est aucun métier du
    catalogue, et la fiche reste exactement où elle était."""
    assert fonction(MUET) is attendu


def test_le_secteur_ne_vole_pas_le_lexique() -> None:
    """« Niwa Paysagiste » : sourcée sur `paysagiste`, elle ne vend que du pavé.

    Son secteur lui ouvre la fenêtre du paysagement, mais le courriel doit
    continuer de parler de pavage — sinon on écrit à un pavageur qu'on veut lui
    parler de plates-bandes. Le poids du secteur (rang de dernier arrivé) est ce
    qui l'empêche, et rien dans ce fichier ne doit le défaire.
    """
    bloc = bloc_metiers_resolus(
        ["Pavé uni", "Pavage de stationnements"],
        SEPTEMBRE,
        gabarit="C",
        industry="entrepreneur paysagiste",
    )
    assert "Métier de la scène** (l'ouvreur) : pavage" in bloc


# ------------------------------------- la garde contre le prochain oubli ---

# Les fonctions qui classent, et le rang de leur argument `industry`.
ATTENDUS = {
    "classer_services": 2,
    "resoudre_metiers": 3,
    "metier_de_la_scene": 3,
}

# Les appels qui n'ont PAS à recevoir `industry`, avec leur raison. Un appel
# ajouté ici doit l'être en connaissance de cause, pas pour faire taire le test.
DISPENSES = {
    # `variantes_du_meme_metier` résout UN LIBELLÉ à la fois pour savoir s'il
    # est une variante : le secteur est une propriété de l'ENTREPRISE, le
    # passer ici rattacherait chaque libellé au secteur et le regroupement
    # dirait n'importe quoi.
    ("src/tools/personalize.py", "resoudre_metiers", "[libelle]"),
}


def _appels_sans_industry() -> list[str]:
    racine = Path(__file__).parent.parent / "src"
    fautifs: list[str] = []
    for fichier in sorted(racine.rglob("*.py")):
        if fichier.name == "metiers.py":
            continue  # le module qui DÉFINIT le classement
        arbre = ast.parse(fichier.read_text(encoding="utf-8"), str(fichier))
        for noeud in ast.walk(arbre):
            if not isinstance(noeud, ast.Call):
                continue
            nom = getattr(noeud.func, "id", None) or getattr(noeud.func, "attr", None)
            # 🔴 `metier_de_la_scene` EST DANS LA LISTE, et son absence est
            # l'histoire de ce test. Écrit avec les deux premiers noms
            # seulement, il est passé au vert alors qu'un sixième appel —
            # celui-là — classait encore sans le secteur, et écrasait le
            # résultat des cinq autres trois lignes plus bas. Une garde
            # structurelle ne vaut que par l'exhaustivité de sa liste.
            if nom not in ATTENDUS:
                continue
            a_industry = (
                len(noeud.args) >= ATTENDUS[nom]
                or any(kw.arg == "industry" for kw in noeud.keywords)
            )
            if a_industry:
                continue
            chemin = fichier.relative_to(racine.parent).as_posix()
            premier = ast.unparse(noeud.args[0]) if noeud.args else ""
            if (chemin, nom, premier) in DISPENSES:
                continue
            fautifs.append(f"{chemin}:{noeud.lineno} — {nom}({premier})")
    return fautifs


def test_aucun_appel_ne_classe_sans_le_secteur() -> None:
    """🔴 LA GARDE STRUCTURELLE, et c'est elle qui vaut le fichier.

    Les cinq oublis n'avaient rien en commun sauf leur forme : un appel à
    `classer_services` / `resoudre_metiers` sans son troisième argument. Les
    tester un par un laisse passer le sixième, écrit demain. Celui-ci lit le
    code source et les trouve tous.
    """
    fautifs = _appels_sans_industry()
    assert not fautifs, (
        "ces appels classent des métiers sans le secteur — soit ils doivent le "
        "recevoir, soit leur raison de s'en passer va dans DISPENSES :\n  "
        + "\n  ".join(fautifs)
    )
