"""`classer_services` : le classement SANS le calendrier.

🔴 POURQUOI CETTE FONCTION EXISTE. Le classement sert deux lecteurs : la copie du
courriel (qui a besoin de la date, pour choisir la scène) et les colonnes
dérivées de `companies` (qui n'en ont pas le droit — une colonne qui dépend du
jour de son calcul est fausse le lendemain, en silence).
"""
from __future__ import annotations

import dis
import inspect
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from src.lib import metiers as mod
from src.lib.metiers import classer_services, resoudre_metiers


def test_la_signature_ne_porte_aucune_date():
    """Un test « deux dates, mêmes colonnes » passerait AUSSI avec une
    implémentation fautive : les métiers sont date-indépendants dans les deux
    cas. Seule l'absence du paramètre prouve qu'aucune date ne peut entrer."""
    params = set(inspect.signature(classer_services).parameters)
    assert "aujourdhui" not in params
    assert not any("date" in p for p in params)


def test_resoudre_metiers_APPELLE_vraiment_classer_services():
    """🔴 Le test de CÂBLAGE, et il est plus important que les autres.

    Un test « mêmes sorties » reste vert si on ajoute `classer_services` SANS
    vider le corps de `resoudre_metiers` — et on se retrouve avec DEUX
    classifications concurrentes, qui divergeront le jour où l'une est corrigée.
    C'est le motif qui a déjà coûté trois gardes décoratives à ce dépôt : la
    fonction est bonne, le chemin ne passe plus par elle.
    """
    noms = {
        i.argval
        for i in dis.get_instructions(mod.resoudre_metiers.__code__)
        if i.opname in ("LOAD_GLOBAL", "LOAD_DEREF") and isinstance(i.argval, str)
    }
    assert "classer_services" in noms, (
        "resoudre_metiers ne référence pas classer_services : le corps d'origine "
        "est resté en place, il existe maintenant DEUX classifications"
    )
    # 🔴 LE CONTRÔLE NÉGATIF DOIT COUVRIR LES QUATRE OUTILS D'APPARIEMENT.
    # N'interdire que `_RACINES_RE` laisse passer une `resoudre_metiers` qui
    # appelle `classer_services` ET garde une seconde passe sur `EXCLUSIONS`,
    # `ECRASE` ou `_sans_accents` : la classification serait de nouveau double,
    # et le test resterait vert. C'est exactement le trou que ce test existe
    # pour fermer.
    #
    # ⚠️ `EXIGE` n'est PAS dans la liste, et c'est voulu : `resoudre_metiers` a
    # le droit de le lire, elle s'en sert pour décider quel métier peut OUVRIR
    # une fenêtre (`m not in EXIGE or m in exigence_satisfaite`). L'y ajouter
    # ferait rougir le test sur du code correct.
    for interdit in ("_RACINES_RE", "EXCLUSIONS", "ECRASE", "_sans_accents"):
        assert interdit not in noms, (
            f"resoudre_metiers touche encore `{interdit}` : elle apparie les "
            "libellés elle-même, en plus d'appeler classer_services"
        )


def test_le_classement_est_celui_de_resoudre_metiers():
    services = ["Déneigement résidentiel", "Aménagement paysager", "Plantations"]
    classement = classer_services(services)
    resolus = resoudre_metiers(services, date(2026, 9, 15))
    assert classement.metiers == resolus.metiers
    assert classement.source == resolus.source


def test_aucun_metier_reconnu_rend_la_source_inconnu():
    """Le retour anticipé porte sur `compte` vide, pas sur `services` vide.

    Mesuré le 2026-09-10 : 2 fiches ont `services_offered` vide, mais 6 tombent
    en défaut inversé — les 4 autres ont des libellés qu'aucune racine n'apparie.
    """
    assert classer_services([]).source == "inconnu"
    assert classer_services(["Consultation stratégique", "Audit"]).source == "inconnu"
    assert classer_services(["Consultation stratégique"]).metiers == ()


def test_lexigence_de_la_piscine_est_rendue():
    """`EXIGE` doit sortir de la fonction : la colonne en a besoin.

    `piscine` reste RECONNUE dans les deux cas — c'est `ECRASE["piscine"]` qui a
    besoin de cette reconnaissance pour empêcher « piscine creusée » de devenir
    de l'excavation. Ce qui change, c'est le droit d'OUVRIR une fenêtre.
    """
    sans = classer_services(["Installation de piscine creusée"])
    avec = classer_services(["Entretien de piscine hebdomadaire"])
    assert "piscine" in sans.metiers
    assert "piscine" not in sans.exigence_satisfaite
    assert "piscine" in avec.exigence_satisfaite


@pytest.mark.parametrize(
    "services",
    [
        # 🔴 CHAQUE CAS PORTE DEUX MÉTIERS DANS UN SEUL LIBELLÉ, DONC À ÉGALITÉ
        # PARFAITE — c'est la seule configuration où l'ordre d'itération du set
        # décide, faute de quoi `ordre_apparition` tranche avant lui et le bug
        # devient invisible.
        #
        # ⚠️ UN CORPUS NE SE CHOISIT PAS À L'INTUITION, IL SE MESURE. Chaque cas
        # ci-dessous a été validé en réimplémentant la boucle buggée
        # (`for metier in apparies:`) et en la rejouant sur 25 graines de hash.
        # Nombre de sorties distinctes AVEC le bug, et répartition :
        #   déneigement + paysagement       → 2 sorties, 13/12
        #   piscine + paysagement           → 2 sorties, 16/9
        #   lavage de vitres + déneigement  → 2 sorties, 14/11
        # SANS le bug : 1 seule sortie pour les trois, sur les 25 graines.
        # Et vérifié sur les 5 graines que ce test utilise réellement (1 à 5) :
        # les trois s'y scindent, donc les trois mordent pour de vrai.
        #
        # 🔴 DEUX CORPUS ÉCARTÉS, POUR MÉMOIRE — tous deux passaient au vert
        # AVEC le bug dedans, ce qui est le pire défaut possible ici :
        #   ['Excavation','Installation de piscine creusée','Terrassement',
        #    'Entretien de piscine'] : 25 graines, 25 fois le même dominant.
        #    `excavation` est seule dans le libellé nº0, donc `ordre_apparition`
        #    tranche l'égalité avant l'ordre du set.
        #   ['Installation de piscines creusées'] : 25 graines, 25 fois
        #    `('piscine',)`. Il n'apparie qu'UN métier — « creusées » ne
        #    déclenche pas `excavation`. Un seul métier, aucune égalité, rien à
        #    départager. Il a vécu un commit dans ce fichier en affirmant le
        #    contraire ; remplacé ici par le cas vitres+déneigement, mesuré.
        ["déneigement et aménagement paysager"],
        ["lavage de vitres et déneigement de toiture"],
        ["entretien de piscine et aménagement paysager"],
    ],
)
def test_le_classement_est_stable_entre_processus(services: list[str]) -> None:
    """Deux PYTHONHASHSEED différents doivent rendre le MÊME classement.

    ⚠️ Un test dans un seul processus ne peut pas voir ça : le hash est tiré au
    démarrage. D'où les sous-processus. ⚠️ Et pas de graine `0` : elle DÉSACTIVE
    la randomisation, donc elle ne prouve rien.
    """
    code = (
        "import json, sys; sys.path.insert(0, '.');"
        "from src.lib.metiers import classer_services;"
        "c = classer_services(json.loads(sys.argv[1]));"
        "print(json.dumps([list(c.metiers), sorted(c.compte.items())]))"
    )
    sorties = set()
    for seed in ("1", "2", "3", "4", "5"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", code, json.dumps(services)],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parents[1]), env=env, check=True,
        )
        assert out.stdout.strip(), f"sortie VIDE sous seed={seed} : {out.stderr}"
        sorties.add(out.stdout.strip())
    assert len(sorties) == 1, f"classement instable selon la graine : {sorties}"
