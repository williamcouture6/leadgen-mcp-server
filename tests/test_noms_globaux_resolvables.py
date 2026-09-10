"""Tout nom global référencé par le code doit exister au module.

🔴 POURQUOI CE FICHIER EXISTE — panne totale évitée de justesse le 2026-09-07.

`src/http_api.py` référençait `resoudre_metiers` dans la boucle de `/wf4/run`
alors que le seul import était **local à une autre fonction**. Python ne s'en
plaint qu'à l'exécution, et l'appel vit dans un `try / except Exception` : la
boucle attrapait donc le `NameError` contact par contact et rendait un lot de
**0 brouillon sur 20, sans erreur visible**. Le cron aurait tourné tous les
jours en écrivant zéro, et le résumé du soir aurait affiché un lot vide — ce qui
ressemble exactement à une file épuisée.

**Les 1556 tests passaient.** Aucun n'exerçait cette ligne, et il n'y a pas de
raison d'espérer qu'un test futur l'exerce : le chemin demande un lot réel, une
base, et un appel au modèle.

Ce test ne teste donc pas un comportement — il vérifie une **propriété du
code** : chaque `LOAD_GLOBAL` doit se résoudre. C'est bon marché, ça couvre tout
le paquet d'un coup, et ça attrape la classe entière : import oublié, import
resté local, renommage partiel, symbole supprimé d'un module.
"""

from __future__ import annotations

import builtins
import dis
import importlib
import pkgutil
import types

import pytest

# Les modules qu'on inspecte. Tout `src.*` importable — si un module ne
# s'importe pas du tout, c'est un autre test qui doit le dire.
PAQUET = "src"

# Noms tolérés : ils n'existent qu'à l'exécution sous conditions.
TOLERES: frozenset[str] = frozenset(
    {
        # `__debug__` et consorts, au cas où une version de Python les compile
        # en LOAD_GLOBAL plutôt qu'en constante.
        "__debug__",
    }
)


def _modules_du_paquet() -> list[types.ModuleType]:
    paquet = importlib.import_module(PAQUET)
    trouves: list[types.ModuleType] = []
    for info in pkgutil.walk_packages(paquet.__path__, prefix=f"{PAQUET}."):
        if ".tests" in info.name:
            continue
        try:
            trouves.append(importlib.import_module(info.name))
        except Exception:  # noqa: BLE001
            # Un module qui ne s'importe pas est le problème d'un autre test ;
            # l'ignorer ici évite de transformer ce garde-fou en alarme sur une
            # dépendance absente.
            continue
    return trouves


def _codes(objet) -> list:
    """Tous les objets-code imbriqués : fonctions, méthodes, lambdas,
    compréhensions. Une compréhension a son propre code object, et c'est
    justement là que se cachent les noms qu'on lit sans les voir."""
    vus, pile, sortie = set(), [objet], []
    while pile:
        c = pile.pop()
        if id(c) in vus:
            continue
        vus.add(id(c))
        sortie.append(c)
        for const in c.co_consts:
            if isinstance(const, types.CodeType):
                pile.append(const)
    return sortie


def _noms_globaux(module: types.ModuleType) -> set[str]:
    fichier = getattr(module, "__file__", None)
    if not fichier or not fichier.endswith(".py"):
        return set()
    with open(fichier, encoding="utf-8") as f:
        source = f.read()
    racine = compile(source, fichier, "exec")
    noms: set[str] = set()
    for code in _codes(racine):
        for instr in dis.get_instructions(code):
            if instr.opname in ("LOAD_GLOBAL", "DELETE_GLOBAL"):
                nom = instr.argval
                if isinstance(nom, str):
                    noms.add(nom)
    return noms


MODULES = _modules_du_paquet()


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
def test_les_noms_globaux_existent(module: types.ModuleType) -> None:
    """Chaque nom lu comme global doit exister — au module, ou dans builtins."""
    manquants = sorted(
        nom
        for nom in _noms_globaux(module)
        if nom not in TOLERES
        and not hasattr(module, nom)
        and not hasattr(builtins, nom)
    )
    assert not manquants, (
        f"{module.__name__} référence {manquants} sans les définir ni les "
        "importer au niveau du module. Python ne s'en plaint qu'à l'exécution, "
        "et si l'appel vit dans un `try/except`, la panne est SILENCIEUSE."
    )


def test_le_garde_fou_attrape_vraiment_le_cas() -> None:
    """Contrôle négatif : sans lui, ce fichier pourrait ne rien vérifier.

    On fabrique un module qui référence un nom absent, exactement comme
    `http_api` le faisait, et on exige que l'inspection le voie.
    """
    faux = types.ModuleType("faux")
    code = "def f():\n    return nom_qui_nexiste_pas(1)\n"
    exec(compile(code, "<faux>", "exec"), faux.__dict__)  # noqa: S102

    racine = compile(code, "<faux>", "exec")
    noms = set()
    for c in _codes(racine):
        for instr in dis.get_instructions(c):
            if instr.opname == "LOAD_GLOBAL" and isinstance(instr.argval, str):
                noms.add(instr.argval)

    assert "nom_qui_nexiste_pas" in noms
    assert not hasattr(faux, "nom_qui_nexiste_pas")


def test_l_inspection_couvre_bien_le_paquet() -> None:
    """Deuxième contrôle négatif : si `_modules_du_paquet` rendait une liste
    vide (renommage du paquet, erreur d'import silencieuse), la
    paramétrisation ci-dessus ne lancerait AUCUN test et la suite resterait
    verte en ne vérifiant rien."""
    noms = {m.__name__ for m in MODULES}
    assert len(MODULES) >= 20, f"seulement {len(MODULES)} modules inspectés"
    for attendu in ("src.http_api", "src.lib.metiers", "src.tools.personalize"):
        assert attendu in noms, f"{attendu} n'est pas inspecté"
