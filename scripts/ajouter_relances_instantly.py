"""Ajoute les deux étapes de relance à la campagne Instantly `agence-ia`.

    python mcp-server/scripts/ajouter_relances_instantly.py              # INSPECTE, n'écrit rien
    python mcp-server/scripts/ajouter_relances_instantly.py --appliquer  # écrit

🔴 POURQUOI CE SCRIPT EXISTE.
WF-6 pousse `followup_1_body` et `followup_2_body` en variables de lead depuis
le 2026-08-30. Mais la campagne n'a qu'**une seule étape** : les variables
arrivent chez un destinataire qui ne les lit pas, et **les relances ne partent
jamais**. Or 68 % des réponses positives arrivent après la 2ᵉ touche — sans ces
deux étapes, on perd les deux tiers du rendement sans qu'aucun compteur ne
bronche.

⚠️ CE SCRIPT NE PEUT PAS TOURNER DEPUIS UNE SESSION CLAUDE CODE : `.env` ne
porte que des valeurs d'exemple, la vraie `INSTANTLY_API_KEY` vit sur Railway.
C'est voulu — une clé d'API n'a pas à transiter par une conversation.

# Ce qu'il fait, et ce qu'il refuse de faire

- **Par défaut il n'écrit RIEN.** Il lit la campagne, affiche sa séquence
  actuelle, et imprime le payload exact qu'il enverrait. Tu regardes, puis tu
  relances avec `--appliquer`.
- **Il construit les nouvelles étapes À PARTIR de l'existante**, jamais depuis
  un gabarit codé en dur. Le `PATCH` d'Instantly remplace le tableau `sequences`
  en entier : le reconstruire de mémoire écraserait l'étape 1 qui fonctionne.
- **Il refuse de tourner si la campagne a déjà plus d'une étape.** Sans ça, un
  deuxième lancement ajouterait deux relances de plus, et le prospect recevrait
  cinq courriels.

# Les délais

`delay` est le nombre de jours d'attente AVANT l'étape, compté depuis la
précédente. Étape 2 à 3 jours (J+3), étape 3 à 4 jours de plus (J+7) — ce que
la copie annonce.

# Le sujet vide

Les relances partent **en fil**, sans objet : c'est ce qui les rattache à la
conversation au lieu d'ouvrir un nouveau courriel. Un sujet vide est la façon
dont Instantly exprime ça.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

API_BASE = "https://api.instantly.ai/api/v2"

# Les deux étapes à ajouter. Le corps est une VARIABLE que WF-6 remplit par
# lead ; le texte réel vit dans `messages.followups`, jugé par WF-5 avant
# d'arriver ici.
RELANCES = [
    {"jours_apres_precedente": 3, "variable": "{{followup_1_body}}", "quand": "jour 3"},
    {"jours_apres_precedente": 4, "variable": "{{followup_2_body}}", "quand": "jour 7"},
]


def _cle() -> str:
    cle = (os.environ.get("INSTANTLY_API_KEY") or "").strip()
    if not cle:
        print(
            "ERREUR : INSTANTLY_API_KEY est vide.\n"
            "  Elle vit sur Railway, pas dans le .env du repo. Pose-la dans ton\n"
            "  environnement avant de lancer :\n"
            "     $env:INSTANTLY_API_KEY = '<ta cle>'      (PowerShell)\n"
            "     export INSTANTLY_API_KEY='<ta cle>'      (bash)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return cle


def _campagne_id(argument: str | None) -> str:
    cid = (argument or os.environ.get("INSTANTLY_CAMPAIGN_ID_REACTI") or "").strip()
    if not cid:
        print(
            "ERREUR : aucun identifiant de campagne.\n"
            "  Passe --campagne <uuid>, ou pose INSTANTLY_CAMPAIGN_ID_REACTI.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return cid


def _entetes(cle: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cle}", "Content-Type": "application/json"}


def lire_campagne(cle: str, cid: str) -> dict:
    r = httpx.get(f"{API_BASE}/campaigns/{cid}", headers=_entetes(cle), timeout=30.0)
    if r.status_code >= 400:
        print(f"ERREUR GET campagne {r.status_code} : {r.text[:400]}", file=sys.stderr)
        raise SystemExit(1)
    return r.json()


def decrire_sequence(campagne: dict) -> list[dict]:
    """Les étapes actuelles, à plat. Tolère les deux formes qu'Instantly rend."""
    sequences = campagne.get("sequences") or []
    etapes: list[dict] = []
    for seq in sequences:
        etapes.extend(seq.get("steps") or [])
    return etapes


def construire_sequences(campagne: dict) -> dict:
    """Le tableau `sequences` complet : l'existant TEL QUEL, plus les relances.

    🔴 On repart de l'objet rendu par l'API, sans le reconstruire. Le PATCH
    remplace le tableau en entier — un gabarit codé en dur écraserait l'étape 1
    et sa configuration (variantes, tracking, réglages qu'on ne connaît pas).
    """
    sequences = json.loads(json.dumps(campagne.get("sequences") or []))
    if not sequences:
        print(
            "ERREUR : la campagne n'a AUCUNE séquence. Ce script ajoute des\n"
            "  relances à une séquence existante ; il ne crée pas l'étape 1.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    premiere = sequences[0]
    steps = premiere.get("steps") or []

    for relance in RELANCES:
        steps.append({
            "type": "email",
            "delay": relance["jours_apres_precedente"],
            "variants": [{
                # Sujet VIDE = la relance part en fil, rattachée à la
                # conversation, au lieu d'ouvrir un nouveau courriel.
                "subject": "",
                "body": relance["variable"],
            }],
        })

    premiere["steps"] = steps
    return {"sequences": sequences}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--appliquer", action="store_true",
                    help="écrit réellement. Sans ce drapeau, le script n'inspecte que.")
    ap.add_argument("--campagne", help="uuid de campagne (défaut : INSTANTLY_CAMPAIGN_ID_REACTI)")
    args = ap.parse_args()

    cle = _cle()
    cid = _campagne_id(args.campagne)

    campagne = lire_campagne(cle, cid)
    etapes = decrire_sequence(campagne)

    print(f"Campagne : {campagne.get('name')!r}  ({cid})")
    print(f"Statut   : {campagne.get('status')}  (0 = brouillon, 1 = active)")
    print(f"Étapes actuelles : {len(etapes)}")
    for i, e in enumerate(etapes, 1):
        variantes = e.get("variants") or [{}]
        sujet = variantes[0].get("subject") or "(vide — en fil)"
        corps = (variantes[0].get("body") or "")[:70].replace("\n", " ")
        print(f"  {i}. delai={e.get('delay')}j  sujet={sujet!r}")
        print(f"     corps: {corps}...")

    if len(etapes) > 1:
        print(
            f"\nARRÊT : la campagne a déjà {len(etapes)} étapes.\n"
            "  Ce script n'ajoute des relances qu'à une campagne qui n'en a qu'une.\n"
            "  Le relancer sur une campagne déjà complétée ferait recevoir CINQ\n"
            "  courriels au prospect. Vérifie l'interface avant d'insister.",
            file=sys.stderr,
        )
        return 1

    patch = construire_sequences(campagne)
    apres = (patch["sequences"][0].get("steps") or [])
    print(f"\nAprès modification : {len(apres)} étapes")
    for i, e in enumerate(apres, 1):
        v = (e.get("variants") or [{}])[0]
        print(f"  {i}. delai={e.get('delay')}j  sujet={v.get('subject')!r}  corps={v.get('body')!r}")

    if not args.appliquer:
        print(
            "\n--- INSPECTION SEULEMENT, rien n'a été écrit ---\n"
            "Relance avec --appliquer si ce qui précède est correct."
        )
        return 0

    r = httpx.patch(
        f"{API_BASE}/campaigns/{cid}", headers=_entetes(cle), json=patch, timeout=30.0
    )
    if r.status_code >= 400:
        print(f"ERREUR PATCH {r.status_code} : {r.text[:600]}", file=sys.stderr)
        return 1

    # On RELIT au lieu de croire le code de retour : une écriture qui se croit
    # partie alors que rien n'a bougé est le mode d'échec que ce repo a déjà eu
    # à refermer deux fois côté WF-7.
    relu = decrire_sequence(lire_campagne(cle, cid))
    print(f"\nÉcrit. La campagne porte maintenant {len(relu)} étapes.")
    if len(relu) != 3:
        print(
            f"⚠️ ATTENDU 3 étapes, LU {len(relu)}. Vérifie dans l'interface avant\n"
            "  de considérer la checklist comme cochée.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
