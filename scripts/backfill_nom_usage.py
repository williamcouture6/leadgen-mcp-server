"""Backfill de `companies.nom_usage`, et de rien d'autre.

Calcule le nom d'usage des fiches déjà recherchées, SANS appel LLM ni re-scrape,
en relisant `research_json` — soit le champ `nom_usage` que le modèle rend
depuis le 2026-09-17, soit, pour les fiches plus anciennes qui ne l'ont pas, le
début de `company_summary`. Dans les DEUX cas, le candidat passe par la MÊME
garde que l'écrivain : `lib.avis.nom_usage_fiable`.

🔴 POURQUOI IL N'APPELLE PAS `db.update_company_research`.
Même raison que `backfill_metiers.py` : cette fonction repose `status` ET
`last_enriched_at`, et `last_enriched_at` porte la ré-éligibilité à 90 jours du
backlog de recherche. Un backfill de colonne dérivée doit être un NO-OP sur tout
le reste de la fiche — d'où l'`update` direct avec un patch d'UNE colonne.

🔴 IL NE SE LANCE QUE SUR DÉCISION EXPLICITE DE WILLIAM.
Comme tous les rattrapages de ce dépôt. Une session qui lit ce fichier va vouloir
le rejouer d'elle-même : NE LE FAIS PAS. Signale que le rejeu est dû, dis ce
qu'il changerait, et attends.

🔴 LES TROIS CONDITIONS DE LA MIGRATION 0072, et où elles en sont.

  1. **Un vrai cron WF-3 doit avoir mesuré le taux de refus de la garde.**
     ✅ Fait le 2026-09-18 : 20 candidats, 18 acceptés, 2 refusés — 10 %, bien
     sous le seuil de 30 % au-delà duquel c'est la GARDE qu'il faudrait ajuster
     plutôt que 546 fiches. Les deux refus retombent proprement sur le libellé
     Google : « Mon pays c'est l'hiver » (vrai nom de MPCH, mais rien dans le
     libellé Google ne le prouve) et « Entreprise TREMA » (singulier contre le
     pluriel de Google).

  2. **Aucune entreprise ne doit voir son nom changer ENTRE l'écriture d'un
     brouillon et son jugement.** C'est la condition qui compte vraiment, et la
     0072 l'exprimait par un raccourci — « la file de brouillons non jugés à
     zéro ». Ce script la tient plus précisément : il ÉCARTE les entreprises qui
     ont un brouillon en attente de jugement (voir `_avec_brouillon_en_attente`).

     ⚠️ Ce n'est pas une commodité, c'est le cœur du problème que la 0072 ferme.
     Le nom est résolu DEUX FOIS À DEUX DATES — par le rédacteur à l'écriture,
     par le juge à la relecture, qui peut venir six jours plus tard. Si la
     colonne change entre les deux, le juge lit « le nom est X » sous un corps
     qui dit Y, et refuse. Un refus est TERMINAL : le contact reste gelé à vie.
     Mesuré le 2026-09-18 : **19 des 20 brouillons en attente** étaient dans ce
     cas.

  3. **La décision de William.** ✅ 2026-09-18.

🔴 LE GROUPE DE CONTRÔLE, et il refuse d'écrire s'il échoue.
Même principe que `backfill_bras_eligibles.py` : on ne fait pas confiance au
script, on lui demande de prouver qu'il a raison sur des cas dont la réponse est
connue d'avance.

  · **négatifs connus** — des candidats que la garde DOIT refuser, mesurés en
    base : « Les » pour Aqua-Verre, « N. Théorêt » pour Déneigement Théoret ;
  · **le taux sur les fiches PROPRES** — celles dont le libellé Google n'est pas
    bourré (`nom_commercial(name) == name` et au plus trois mots). Sur celles-là
    le nom extrait devrait, la plupart du temps, ÉGALER le nom Google. Sous 80 %,
    le script abandonne : ça voudrait dire que l'extraction depuis
    `company_summary` produit autre chose qu'un nom.

Usage :
    python -m scripts.backfill_nom_usage            # à blanc, n'écrit RIEN
    python -m scripts.backfill_nom_usage --ecrire   # écrit
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import unicodedata
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.lib.avis import nom_commercial, nom_usage_fiable  # noqa: E402
from src import supabase_client as sb  # noqa: E402

# Le critère de rattrapage, recopié de la migration 0072 pour que les deux
# disent la même chose.
STATUTS_EXCLUS = ("disqualified", "suppressed")
MAX_MOTS_PREFIXE = 6
PLANCHER_FICHES_PROPRES = 0.80


def _sans_accents(t: str) -> str:
    plat = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in plat if not unicodedata.combining(c))


def candidat_depuis_resume(resume: str, nom_brut: str) -> str | None:
    """Le plus LONG préfixe du résumé que la garde accepte, tel qu'elle le rend.

    ⚠️ On retient ce que la GARDE REND, jamais le préfixe brut. Elle nettoie
    avant d'accepter (glose entre parenthèses, suffixe légal), donc « Symetric
    (Cèdres Gatineau) est une entreprise… » doit donner « Symetric » et non le
    préfixe. Une première version de ce calcul gardait le préfixe : elle
    écrivait `Symetric (Cèdres Gatineau)` dans la colonne.
    """
    mots = resume.split()
    retenu: str | None = None
    for n in range(1, MAX_MOTS_PREFIXE + 1):
        if n > len(mots):
            break
        prefixe = " ".join(mots[:n]).strip(" ,.;:-")
        rendu = nom_usage_fiable(prefixe, nom_brut)
        if rendu:
            retenu = rendu
    return retenu


def nom_usage_de(fiche: dict[str, Any]) -> str | None:
    """Le nom d'usage d'une fiche, ou None si rien ne se prouve."""
    rj = fiche.get("research_json") or {}
    nom_brut = fiche.get("name") or ""
    # 1. le champ que le modèle rend depuis le 2026-09-17, quand il existe
    direct = nom_usage_fiable(rj.get("nom_usage"), nom_brut)
    if direct:
        return direct
    # 2. sinon, le début du résumé — les fiches antérieures n'ont pas le champ
    resume = rj.get("company_summary")
    if isinstance(resume, str) and resume.strip():
        return candidat_depuis_resume(resume, nom_brut)
    return None


def _fiche_propre(fiche: dict[str, Any]) -> bool:
    """Le libellé Google n'est pas bourré : le nom extrait devrait l'égaler."""
    nom = fiche.get("name") or ""
    return nom_commercial(nom) == nom and len(nom.split()) <= 3


def groupe_de_controle(temoins: list[dict[str, Any]]) -> list[str]:
    """Les échecs qui interdisent d'écrire. Liste vide = on peut y aller.

    🔴 `temoins` EST UNE POPULATION STABLE, PAS LES FICHES À ÉCRIRE. La nuance
    a coûté un faux refus le 2026-09-18, au deuxième passage du rattrapage :

    la première version mesurait sur les fiches candidates. Au premier passage
    (510 fiches, mélange représentatif) le taux était bon. Au second il ne
    restait que 35 fiches — c'est-à-dire, **par construction, celles que le
    premier passage n'a PAS su résoudre**. Le taux est tombé à 43 % et le
    script a refusé d'écrire, alors que rien n'avait changé dans l'extraction.

    Mesurer un outil sur un résidu trié pour sa difficulté ne dit rien sur
    l'outil. Le témoin lit donc TOUTES les fiches recherchées, qu'elles aient
    déjà un nom ou non : la référence ne bouge pas d'un passage à l'autre.
    """
    echecs: list[str] = []

    # a. des négatifs mesurés en base : la garde DOIT les refuser
    negatifs = (
        ("Les", "Lavage de vitres Services Aqua-Verre inc."),
        ("N. Théorêt", "Déneigement Théoret"),
        ("Groupe Sani", "Sani Nettoyage"),
    )
    for candidat, brut in negatifs:
        if nom_usage_fiable(candidat, brut) is not None:
            echecs.append(
                f"la garde accepte « {candidat} » pour « {brut} » — elle s'est "
                "relâchée depuis la mesure du 2026-09-17"
            )

    # b. sur les fiches propres, le nom extrait doit égaler le nom Google
    #    UNE FOIS PASSÉ PAR LA MÊME GARDE.
    #
    # 🔴 LE PREMIER CRITÈRE SE CONTREDISAIT LUI-MÊME, et le dry-run du
    # 2026-09-18 l'a montré : 73 % au lieu des 80 % exigés, donc refus d'écrire.
    # En regardant les 76 « échecs », presque tous étaient des AMÉLIORATIONS :
    #
    #     Pavage Robillard Inc        -> Pavage Robillard
    #     mini excavation s.major     -> Mini Excavation S. Major
    #     T3 CIRCLE EXTERMINATION     -> T3 Circle
    #
    # Le « Inc. » qui saute, c'est LA GARDE ELLE-MÊME qui le retire (nettoyage
    # du suffixe légal). Comparer son résultat au libellé brut, qui le porte
    # encore, ne pouvait qu'échouer. Le repère juste est le libellé passé par la
    # MÊME garde — sinon on mesure l'écart entre la garde et elle-même.
    #
    # ⚠️ Le seuil de 80 % n'a PAS bougé. C'est le repère qui était faux, pas la
    # barre qui était trop haute — baisser un seuil pour faire passer un
    # contrôle, c'est supprimer le contrôle.
    propres = [f for f in temoins if _fiche_propre(f)]
    if propres:
        def _repere(f: dict[str, Any]) -> str:
            nom = f.get("name") or ""
            return _sans_accents(nom_usage_fiable(nom, nom) or nom)

        egaux = sum(
            1 for f in propres
            if _sans_accents(nom_usage_de(f) or "") == _repere(f)
        )
        taux = egaux / len(propres)
        if taux < PLANCHER_FICHES_PROPRES:
            echecs.append(
                f"sur les {len(propres)} fiches au libellé propre, seules "
                f"{egaux} ({taux:.0%}) rendent le nom Google — attendu ≥ "
                f"{PLANCHER_FICHES_PROPRES:.0%}. L'extraction depuis le résumé "
                "produit autre chose qu'un nom."
            )
    return echecs


async def _avec_brouillon_en_attente() -> set[str]:
    """Les entreprises dont un brouillon attend le juge — voir condition 2."""
    messages = await sb.select_all(
        "messages",
        params={
            "select": "contact_id",
            "status": "eq.draft",
            "compliance_check_passed": "is.null",
        },
        order="id.asc",
    )
    ids = [m["contact_id"] for m in messages if m.get("contact_id")]
    if not ids:
        return set()
    entreprises: set[str] = set()
    for i in range(0, len(ids), 100):
        tranche = ids[i:i + 100]
        contacts = await sb.select_all(
            "contacts",
            params={
                "select": "company_id",
                "id": "in.(" + ",".join(tranche) + ")",
            },
            order="id.asc",
        )
        entreprises.update(c["company_id"] for c in contacts if c.get("company_id"))
    return entreprises


async def principal(ecrire: bool) -> int:
    fiches = await sb.select_all(
        "companies",
        params={
            "select": "id,name,nom_usage,research_json,status",
            "nom_usage": "is.null",
            "research_json": "not.is.null",
            "status": "not.in.(" + ",".join(STATUTS_EXCLUS) + ")",
        },
        # ⚠️  est obligatoire et doit porter sur une colonne UNIQUE :
        # sans ORDER BY stable, deux pages successives peuvent sauter ou
        # dupliquer des lignes.  ne l'est pas (des homonymes existent).
        order="id.asc",
    )
    print(f"{len(fiches)} fiche(s) candidates au rattrapage.")

    # Le témoin lit sa propre population, indépendante de ce qu'il reste à
    # écrire — voir le docstring de `groupe_de_controle`.
    temoins = await sb.select_all(
        "companies",
        params={
            "select": "id,name,nom_usage,research_json,status",
            "research_json": "not.is.null",
            "status": "not.in.(" + ",".join(STATUTS_EXCLUS) + ")",
        },
        order="id.asc",
    )
    print(f"{len(temoins)} fiche(s) au groupe de contrôle (population stable).")

    echecs = groupe_de_controle(temoins)
    if echecs:
        print("\n🔴 GROUPE DE CONTRÔLE EN ÉCHEC — rien n'est écrit :")
        for e in echecs:
            print(f"   · {e}")
        return 1
    print("✅ groupe de contrôle passé.")

    gelees = await _avec_brouillon_en_attente()
    print(f"{len(gelees)} entreprise(s) écartée(s) : un brouillon attend le juge.")

    acceptes: list[tuple[str, str, str]] = []
    refuses: list[tuple[str, str]] = []
    for f in fiches:
        if f["id"] in gelees:
            continue
        nom = nom_usage_de(f)
        if nom:
            acceptes.append((f["id"], f["name"], nom))
        else:
            rj = f.get("research_json") or {}
            refuses.append((f["name"], str(rj.get("nom_usage") or "(rien à extraire)")))

    changes = [(i, a, b) for i, a, b in acceptes if a != b]
    print(f"\n{len(acceptes)} accepté(s), dont {len(changes)} qui CHANGENT le nom "
          f"imprimé · {len(refuses)} refusé(s) → repli inchangé")

    print("\n--- ce qui CHANGE (les 40 premiers) ---")
    for _i, avant, apres in changes[:40]:
        print(f"  {avant[:58]:60s} -> {apres}")
    print("\n--- refusés (les 20 premiers) ---")
    for nom, cand in refuses[:20]:
        print(f"  {nom[:58]:60s} <- candidat : {cand[:40]}")

    if not ecrire:
        print("\n(à blanc — rien n'a été écrit. Relancer avec --ecrire.)")
        return 0

    for i, (cid, _avant, nom) in enumerate(acceptes, 1):
        # ⚠️ Le filtre porte AUSSI sur `nom_usage=is.null` : si WF-3 a écrit
        # entre la lecture et l'écriture, SA valeur gagne. Même garde que
        # `backfill_bras_eligibles.py`.
        await sb.update(
            "companies",
            {"nom_usage": nom},
            filters={"id": f"eq.{cid}", "nom_usage": "is.null"},
        )
        if i % 50 == 0:
            print(f"  … {i}/{len(acceptes)}")
    print(f"\n✅ {len(acceptes)} fiche(s) écrites.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ecrire", action="store_true",
                    help="écrit vraiment (sans ce drapeau, tourne à blanc)")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(principal(args.ecrire)))
