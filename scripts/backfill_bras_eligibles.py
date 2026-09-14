"""Rattrapage de `messages.bras_eligibles` sur les brouillons qui n'en ont pas.

🔴 LE TROU QU'IL COMBLE, mesuré le 2026-09-13 sur la vraie base.

    cohorte  messages   A   B   C   D
    (vide)      57     23  21   6   7
    CD          29      0   0  14  15
    ABCD        23      0   0  10  13

La ligne `ABCD` annonce « les quatre bras étaient en jeu » et ne contient NI A
NI B. `v_bras_en_tete` élit la cohorte qui compare le plus de bras : elle
afficherait donc « comparaison à quatre » en n'opposant en réalité que C à D,
avec A et B absents du classement pendant que le tableau juste au-dessus montre
leurs chiffres. Un lecteur qui survole lit « C gagne ».

Les 57 lignes vides ont deux origines distinctes :

  · LA CONVENTION DE LA 0055. Son rattrapage SQL a volontairement laissé à NULL
    les leads A/B des lots `ABCD`, faute de pouvoir distinguer « les quatre
    étaient en jeu » de « la garde a écarté C et D ». C'était la bonne décision
    avec les moyens d'alors — `companies.metiers` n'existait pas encore.

  · UN RETARD DE DÉPLOIEMENT. Le code qui écrit la colonne est daté du
    2026-09-10 20h51 mais n'a été poussé que le 2026-09-13 vers 19h48. WF-4 a
    donc tourné trois midis (11, 12, 13) sur la version précédente, qui ne
    connaissait pas la colonne. Ces 31 lignes-là ne sont pas une convention,
    c'est une perte sèche.

🔴 POURQUOI C'EST UN SCRIPT ET PAS UNE MIGRATION SQL.
Reconstituer l'ensemble éligible demande de savoir si C et D étaient servables,
et cette règle est `metiers_reconnus and (citation_autorisee or nb_services >= 2)`
— trois calculs dont le premier est le dictionnaire de `lib/metiers`. L'écrire
en SQL aurait recopié ce dictionnaire dans une seconde vérité, exactement ce que
le docstring de `lib/gabarits` interdit. On appelle donc le vrai code, par
`tete_fixe_servable_pour_entreprise`.

🔴 POURQUOI LE RECALCUL EST FIDÈLE, ET COMMENT ON LE PROUVE.
Deux conditions, vérifiées avant d'écrire quoi que ce soit :

  1. LE DICTIONNAIRE N'A PAS BOUGÉ. `RACINES`, `EXIGE`, `EXCLUSIONS`, `ECRASE`,
     `FENETRE_PAR_METIER` sont identiques entre la version déployée le
     2026-09-09 et aujourd'hui (comparés par valeur, commentaires exclus). Et le
     calcul ne lit AUCUNE date — voir `tete_fixe_servable_pour_entreprise`.

  2. LE GROUPE DE CONTRÔLE. Les 23 lignes déjà étiquetées `ABCD` qui ont servi
     C ou D PROUVENT que le métier était reconnu à la rédaction. Si le recalcul
     d'aujourd'hui en contredit une seule, il est infidèle et le script REFUSE
     d'écrire. C'est le cœur : on ne devine pas, on vérifie sur des lignes dont
     la réponse est déjà connue avant de toucher à celles dont elle ne l'est pas.

⚠️ LES LOTS `CD` SONT HORS CONTRÔLE, VOLONTAIREMENT. Quand la consigne ne
demande QUE des gabarits à tête fixe, `bras_eligibles` les garde même sans
métier reconnu (il n'y a rien vers quoi basculer). Servir C dans un lot `CD` ne
prouve donc rien, et ces lignes ne peuvent ni valider ni invalider le recalcul.

⚠️ IL NE TOUCHE JAMAIS UNE LIGNE DÉJÀ ÉTIQUETÉE. Le filtre d'écriture porte
`bras_eligibles=is.null` en plus de l'id : si WF-4 écrit entre la lecture et
l'écriture, c'est SA valeur qui gagne — elle décrit le tirage réel, la nôtre
n'en est qu'une reconstitution.

⚠️ IL EST À USAGE UNIQUE. Une fois la colonne écrite par WF-4 à chaque
brouillon, il ne trouve plus rien. Le relancer est un no-op, pas une erreur.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import sys
from collections import Counter
from pathlib import Path

# UTF-8 stdout pour PowerShell
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import supabase_client as sb  # noqa: E402
from src.lib.gabarits import (  # noqa: E402
    GABARITS_A_TETE_FIXE,
    bras_eligibles_texte,
    tete_fixe_servable_pour_entreprise,
)

# 200, pas 2000. ⚠️ PostgREST plafonne TOUTE réponse à 1000 lignes sans rien
# signaler : toute lecture passe par `select_all`, qui pagine.
TAILLE_PAGE = 200


async def run(track: str, lot: str, dry_run: bool) -> int:
    messages = await sb.select_all(
        "messages",
        order="created_at.asc",
        page_size=TAILLE_PAGE,
        params={
            "select": "id,contact_id,template_choice,bras_eligibles,status,created_at",
            "direction": "eq.outbound",
            "template_choice": "not.is.null",
            "track": f"eq.{track}",
        },
    )
    print(f"[{track}] {len(messages)} messages sortants portant un bras.")
    if not messages:
        return 0

    contacts = await sb.select_all(
        "contacts", order="id.asc", page_size=TAILLE_PAGE,
        params={"select": "id,company_id"},
    )
    company_par_contact = {c["id"]: c["company_id"] for c in contacts}

    companies = await sb.select_all(
        "companies", order="id.asc", page_size=TAILLE_PAGE,
        params={"select": "id,name,research_json,google_rating,google_reviews_count"},
    )
    fiche_par_id = {co["id"]: co for co in companies}

    def recalcul(msg: dict) -> tuple[dict | None, bool]:
        """La fiche de l'entreprise du message, et C/D y étaient-ils servables ?"""
        co_id = company_par_contact.get(msg["contact_id"])
        fiche = fiche_par_id.get(co_id) if co_id else None
        if fiche is None:
            return None, False
        return fiche, tete_fixe_servable_pour_entreprise(fiche)

    # ---------------------------------------------------------------- CONTRÔLES
    # Deux barrières AVANT toute écriture. Un seul manquement et on n'écrit rien :
    # une étiquette fausse est pire qu'une étiquette absente, parce qu'elle entre
    # dans la comparaison en ayant l'air d'une donnée.
    incoherences: list[str] = []
    orphelins = 0

    for m in messages:
        stocke = m.get("bras_eligibles")
        if not stocke:
            continue
        # Barrière 1 : la donnée existante est-elle cohérente avec elle-même ?
        if m["template_choice"] not in stocke:
            incoherences.append(
                f"  {m['id'][:8]} sert {m['template_choice']} hors de son ensemble stocke {stocke!r}"
            )
            continue
        # Barrière 2 : LE GROUPE DE CONTRÔLE. Un lot à quatre bras qui a servi C
        # ou D prouve que le métier était reconnu. Le recalcul doit le retrouver.
        if stocke == "ABCD" and m["template_choice"] in GABARITS_A_TETE_FIXE:
            fiche, servable = recalcul(m)
            if fiche is None:
                orphelins += 1
                continue
            if not servable:
                incoherences.append(
                    f"  {m['id'][:8]} a servi {m['template_choice']} dans un lot ABCD, "
                    f"mais le recalcul dit C/D inservables pour {fiche.get('name')!r}"
                )

    controles = sum(
        1 for m in messages
        if m.get("bras_eligibles") == "ABCD" and m["template_choice"] in GABARITS_A_TETE_FIXE
    )
    print(f"Groupe de controle : {controles} lignes deja etiquetees ABCD ayant servi C ou D.")
    if orphelins:
        print(f"  (dont {orphelins} sans fiche d entreprise lisible, ecartees du controle)")

    if incoherences:
        print(f"\n🔴 {len(incoherences)} INCOHERENCE(S) — AUCUNE ECRITURE :")
        for ligne in incoherences[:20]:
            print(ligne)
        if len(incoherences) > 20:
            print(f"  ... et {len(incoherences) - 20} autres")
        print(
            "\nLe recalcul contredit des lignes dont la reponse est deja connue.\n"
            "Ne pas forcer : corriger la cause (dictionnaire modifie ? fiche\n"
            "re-researchee depuis la redaction ?) avant de rejouer."
        )
        return 1
    print("  ✅ aucune contradiction : le recalcul reproduit les lignes deja connues.")

    # ---------------------------------------------------------------- ECRITURE
    a_ecrire: list[tuple[dict, str]] = []
    refus: list[str] = []
    sans_fiche = 0

    for m in messages:
        if m.get("bras_eligibles"):
            continue
        fiche, servable = recalcul(m)
        if fiche is None:
            sans_fiche += 1
            continue
        calcule = bras_eligibles_texte(lot, metier_connu=servable)
        # Barrière 3 : ce qu'on s'apprête à écrire doit CONTENIR le bras qui a
        # réellement été servi. Sinon l'étiquette dirait « ce bras ne pouvait pas
        # sortir » sur une ligne où il est sorti — une contradiction écrite en base.
        if not calcule or m["template_choice"] not in calcule:
            refus.append(
                f"  {m['id'][:8]} a servi {m['template_choice']} mais le calcul rend {calcule!r} "
                f"({fiche.get('name')})"
            )
            continue
        a_ecrire.append((m, calcule))

    if refus:
        print(f"\n🔴 {len(refus)} LIGNE(S) CONTREDISENT LE TIRAGE — AUCUNE ECRITURE :")
        for ligne in refus[:20]:
            print(ligne)
        return 1

    repartition = Counter(v for _, v in a_ecrire)
    print(f"\n{len(a_ecrire)} ligne(s) a etiqueter : " + ", ".join(
        f"{k}={n}" for k, n in sorted(repartition.items())
    ) or "aucune")
    if sans_fiche:
        print(f"  ({sans_fiche} sans fiche d entreprise lisible, laissees a NULL)")

    if dry_run:
        for m, calcule in a_ecrire[:15]:
            fiche, _ = recalcul(m)
            print(f"  {m['id'][:8]} {m['created_at'][:10]} bras={m['template_choice']} "
                  f"-> {calcule}  ({fiche.get('name') if fiche else '?'})")
        if len(a_ecrire) > 15:
            print(f"  ... et {len(a_ecrire) - 15} autres")
        print("\nDRY-RUN : rien ecrit. Relance sans --dry-run pour appliquer.")
        return 0

    ecrites = 0
    for m, calcule in a_ecrire:
        # ⚠️ `bras_eligibles=is.null` dans le filtre : anti-clobber. Si WF-4 a
        # ecrit entre notre lecture et notre ecriture, SA valeur gagne — elle
        # decrit le tirage reel, la notre n en est qu une reconstitution.
        await sb.update(
            "messages",
            {"bras_eligibles": calcule},
            filters={"id": f"eq.{m['id']}", "bras_eligibles": "is.null"},
        )
        ecrites += 1

    print("─" * 60)
    print(f"ecrites={ecrites}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Rattrapage de messages.bras_eligibles (usage unique, groupe de controle obligatoire)."
    )
    ap.add_argument("--track", default="agence-ia", choices=["OPT", "agence-ia"])
    # ⚠️ LA CONSIGNE DU LOT NE SE DEVINE PAS, elle se lit dans le JSON n8n :
    # `n8n/workflows/wf-reacti-4-personalize.json` poste "ABCD". Toutes les
    # lignes a rattraper viennent de la — un lot "CD" ne peut pas servir A ni B,
    # et celles du 2026-09-09 anterieures a 20h UTC ont deja ete etiquetees
    # 'CD' par la migration 0055.
    ap.add_argument("--lot", default="ABCD")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args.track, args.lot, args.dry_run)))


if __name__ == "__main__":
    main()
