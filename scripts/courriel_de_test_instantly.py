"""T'envoie à TOI le courriel exact qu'un prospect recevrait.

    python mcp-server/scripts/courriel_de_test_instantly.py                     # INSPECTE
    python mcp-server/scripts/courriel_de_test_instantly.py --moi ton@mail.com  # INSPECTE, montre le payload
    python mcp-server/scripts/courriel_de_test_instantly.py --moi ton@mail.com --appliquer

🔴 POURQUOI UN « SEND TEST » D'INSTANTLY NE SUFFIT PAS.

La question à laquelle ce script répond est : est-ce que `{{email}}` s'interpole
dans le lien de désabonnement de la signature ?

`{{email}}` est une variable de **lead**. Un envoi de test sans lead n'a rien à
y mettre : il affichera `{{email}}` en toutes lettres même si toute la
configuration est juste. Tu conclurais que c'est cassé alors que ça ne l'est
pas — ou l'inverse, ce qui est pire.

Le seul test qui prouve quelque chose, c'est d'être un vrai lead. Ce script
t'ajoute comme lead avec le VRAI corps et les VRAIES relances, pour que tu
reçoives mot pour mot ce que reçoit un contracteur.

⚠️ CE SCRIPT NE PEUT PAS TOURNER DEPUIS UNE SESSION CLAUDE CODE : la vraie
`INSTANTLY_API_KEY` vit sur Railway. C'est voulu — une clé d'API n'a pas à
transiter par une conversation. Récupère-la depuis Railway, pose-la dans ton
environnement le temps du test, et lance le script toi-même.

CE QU'IL VÉRIFIE AVANT D'ÉCRIRE.

Un lead ne part que si la campagne est LANCÉE, donc tout lead déjà présent
partirait en même temps que le tien. Le script les COMPTE, les NOMME, et refuse
d'écrire s'il en trouve d'autres que toi (`--je-sais-ce-que-je-fais` passe
outre).

✅ Au 2026-09-02, William a retiré du workspace Instantly les leads poussés en
juin : la campagne n'en porte plus. Le compte devrait donc être à zéro. La
vérification reste parce qu'elle MESURE au lieu de supposer — c'est ce qui
permet d'arrêter de répéter l'avertissement.

⚠️ À ne pas confondre avec la BASE : `messages` garde 76 lignes `failed` de
juin, et leurs contacts ne seront jamais re-rédigés (la fenêtre WF-4 saute tout
contact qui porte déjà un message). La liste atteignable est donc de 287 sur
363. C'est un fait sur la base, pas un risque côté Instantly.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

RACINE = Path(__file__).resolve().parents[2]
BASE = "https://api.instantly.ai/api/v2"


def charger_env() -> dict[str, str]:
    env: dict[str, str] = dict(os.environ)
    for rel in (".env", "mcp-server/.env"):
        fp = RACINE / rel
        if not fp.exists():
            continue
        for ligne in fp.read_text(encoding="utf-8").splitlines():
            ligne = ligne.strip()
            if ligne and not ligne.startswith("#") and "=" in ligne:
                cle, val = ligne.split("=", 1)
                env.setdefault(cle.strip(), val.strip().strip('"').strip("'"))
    return env


ENV = charger_env()
CLE = (ENV.get("INSTANTLY_API_KEY") or "").strip()
CAMPAGNE = (
    ENV.get("INSTANTLY_CAMPAIGN_ID_REACTI") or ENV.get("INSTANTLY_CAMPAIGN_ID") or ""
).strip()


def api(methode: str, chemin: str, corps: dict | None = None):
    data = json.dumps(corps).encode() if corps is not None else None
    req = urllib.request.Request(
        f"{BASE}{chemin}", data=data, method=methode,
        headers={"Authorization": f"Bearer {CLE}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"erreur": e.read().decode()[:500]}


def main() -> int:
    if not CLE or not CAMPAGNE:
        print("INSTANTLY_API_KEY ou l'id de campagne manquent.")
        print("Récupère-les sur Railway et exporte-les avant de lancer :")
        print('  export INSTANTLY_API_KEY="..."')
        print('  export INSTANTLY_CAMPAIGN_ID_REACTI="..."')
        return 1

    mon_adresse = ""
    for i, a in enumerate(sys.argv):
        if a == "--moi" and i + 1 < len(sys.argv):
            mon_adresse = sys.argv[i + 1].strip()

    st, camp = api("GET", f"/campaigns/{CAMPAGNE}")
    if st != 200:
        print(f"lecture de la campagne impossible ({st}) : {camp}")
        return 1

    etapes = ((camp.get("sequences") or [{}])[0].get("steps") or [])
    print(f"campagne : {camp.get('name')!r}")
    print(f"statut   : {camp.get('status')}  (1 = lancée, 0 = brouillon/pause)")
    print(f"étapes   : {len(etapes)}")
    for n, e in enumerate(etapes, 1):
        corps = ((e.get("variants") or [{}])[0].get("body") or "")
        marqueurs = [m for m in ("email_body", "followup_1_body", "followup_2_body",
                                 "followup_3_body") if m in corps]
        print(f"   {n}. délai {e.get('delay')} j · variables {marqueurs or 'AUCUNE'}")

    if len(etapes) < 4:
        print()
        print(f"⚠️ {len(etapes)} étapes seulement. Les relances sans étape n'arrivent "
              "nulle part : tu ne recevrais pas le fil complet.")

    # On MESURE au lieu de supposer. C'est ce qui permet d'arrêter de répéter
    # l'avertissement d'une conversation à l'autre : le chiffre est lu, pas cru.
    st, leads = api("POST", "/leads/list", {"campaign": CAMPAGNE, "limit": 100})
    liste = leads.get("items", leads.get("data", [])) if isinstance(leads, dict) else []
    autres = [l for l in liste if (l.get("email") or "").lower() != mon_adresse.lower()]
    print()
    print(f"leads déjà dans la campagne : {len(liste)}  · autres que toi : {len(autres)}")
    if autres:
        print("   ⚠️ CES LEADS PARTENT AUSSI si tu lances la campagne pour te tester.")
        for l in autres[:5]:
            print(f"      · {l.get('email')}")
        if len(autres) > 5:
            print(f"      … et {len(autres) - 5} autres")
    else:
        print("   ✅ campagne vide — rien d'autre que toi ne partira.")

    if not mon_adresse:
        print()
        print("Relance avec --moi ton@adresse.com pour voir le payload.")
        return 0

    # Le chemin AVANT l'import — sinon le script ne marche que s'il est lancé
    # depuis `mcp-server/`, ce qui n'est pas la commande donnée en tête.
    sys.path.insert(0, str(RACINE / "mcp-server"))
    from src.lib.relances import CORPS_RELANCES  # noqa: E402

    charge = {
        "campaign": CAMPAGNE,
        "email": mon_adresse,
        "first_name": "William",
        "company_name": "Test interne",
        "custom_variables": {
            "email_subject": "évite de perdre des clients",
            "email_body": "Bonjour,\n\nCeci est un test interne. Ce qui compte est "
                          "au bas du message : la signature et le lien de "
                          "désabonnement.\n\nSi tu vois {{email}} écrit en toutes "
                          "lettres dans le lien, la variable ne s'interpole pas.",
            "followup_1_body": CORPS_RELANCES["relance_1"],
            "followup_2_body": CORPS_RELANCES["relance_2"],
            "followup_3_body": CORPS_RELANCES["relance_3"],
        },
    }

    print()
    print("=== payload qui serait envoyé ===")
    apercu = dict(charge)
    apercu["custom_variables"] = {
        k: (v[:70] + "…" if len(v) > 70 else v)
        for k, v in charge["custom_variables"].items()
    }
    print(json.dumps(apercu, ensure_ascii=False, indent=2))

    if "--appliquer" not in sys.argv:
        print()
        print("INSPECTION SEULEMENT. Ajoute --appliquer pour t'ajouter comme lead.")
        return 0

    if autres and "--je-sais-ce-que-je-fais" not in sys.argv:
        print()
        print(f"REFUS : {len(autres)} autres leads dorment dans cette campagne.")
        print("Retire-les d'abord, ou ajoute --je-sais-ce-que-je-fais.")
        return 1

    st, rep = api("POST", "/leads", charge)
    print(f"ajout du lead -> {st}")
    if st not in (200, 201):
        print(rep)
        return 1
    print("ok. La campagne doit être LANCÉE pour que le courriel parte.")
    print()
    print("À vérifier dans le courriel reçu :")
    print("  1. la signature apparaît-elle au bas ?")
    print("  2. le lien de désabonnement porte-t-il TON adresse, ou « {{email}} » ?")
    print("  3. clique-le : la page pré-remplit-elle le champ ?")
    print("  4. les 3 relances suivent-elles aux délais prévus ?")
    print()
    print("⚠️ RETIRE-TOI de la campagne après le test, sinon tu restes dans la")
    print("   séquence et tu recevras les relances pendant deux semaines.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
