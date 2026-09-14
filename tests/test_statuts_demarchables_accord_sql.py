"""Le triplet « pas encore démarché » est écrit DEUX FOIS. Ce test les accorde.

Il vit à deux endroits que rien ne relie mécaniquement :

  · `src/tools/send.py` → `CONTACT_STATUTS_DEMARCHABLES` (Python), qui décide
    si un courriel PART ;
  · la DERNIÈRE migration qui redéfinit `v_pourquoi_pas_de_courriel` → le
    `count(*) filter (…)` de `ct_agg`, qui décide si la vue annonce « rien ne
    s'y oppose ».

🔴 LA MIGRATION SE TROUVE PAR NUMÉRO LE PLUS HAUT, JAMAIS EN DUR. Ce fichier
pointait sur la `0062` ; la `0065` a redéfini la vue trois jours plus tard, et
le test aurait continué de lire une définition morte — vert pour toujours,
aveugle au fichier qui fait réellement foi. C'est la même classe de défaut que
celle qu'il surveille : une référence figée vers quelque chose qui bouge.

🔴 CE QUE COÛTE UN DÉSACCORD. Si le SQL est plus permissif que Python, la vue
promet un courriel qui ne partira jamais : une entreprise reste éternellement
« en file », et personne ne cherche pourquoi puisque la vue dit que tout va
bien. Si c'est l'inverse, une entreprise parfaitement envoyable est classée
`contacts_tous_ecartes` et sort des radars. Les deux se remarquent des semaines
plus tard, sur une fiche à la fois.

C'est exactement le patron que le dépôt a déjà payé ailleurs : une règle écrite
deux fois qu'aucun test ne confronte finit par diverger.

⚠️ POURQUOI PAS UNE SOURCE UNIQUE. Parce qu'il n'y en a pas de possible ici :
la vue est évaluée par Postgres, sans accès au Python, et la lire depuis le code
à l'exécution ajouterait un aller-retour réseau sur le chemin d'envoi. On assume
la duplication et on la SURVEILLE — ce fichier est la surveillance.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/ → mcp-server/ → le dépôt `infra` qui contient supabase/
RACINE_INFRA = Path(__file__).resolve().parents[2]
MIGRATIONS = RACINE_INFRA / "supabase" / "migrations"


def _derniere_definition_de_la_vue() -> Path | None:
    """Le fichier de migration le plus RÉCENT qui redéfinit la vue.

    Les migrations sont numérotées et rejouées dans l'ordre : c'est donc la
    dernière qui fait foi. Trier les noms suffit — ils sont préfixés d'un
    numéro à quatre chiffres, zéro-paddé, depuis le début du dépôt.

    ⚠️ On filtre sur le CONTENU (`create … view public.v_pourquoi_pas_de_courriel`)
    et pas seulement sur le nom de fichier : une migration peut toucher la vue
    sans le dire dans son nom, et une autre peut porter son nom sans la
    redéfinir (un simple `comment on view`, par exemple).
    """
    candidates = [
        p for p in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        if re.search(
            r"create\s+(or\s+replace\s+)?view\s+public\.v_pourquoi_pas_de_courriel",
            p.read_text(encoding="utf-8"), re.IGNORECASE,
        )
    ]
    return candidates[-1] if candidates else None


MIGRATION = _derniere_definition_de_la_vue() if MIGRATIONS.is_dir() else None


def _statuts_du_sql(texte: str) -> frozenset[str]:
    """Extrait le triplet du `count(*) filter (where status in (…))` de `ct_agg`.

    On vise cette ligne précise et pas n'importe quel `status in (…)` : le même
    fichier en contient d'autres (`bool_or(status in ('opted_out','bounced'))`),
    et les confondre ferait passer le test pour de mauvaises raisons.
    """
    m = re.search(
        r"count\(\*\)\s+filter\s*\(\s*where\s+status\s+in\s*\(([^)]*)\)\s*\)",
        texte, re.IGNORECASE,
    )
    assert m, (
        "le `count(*) filter (where status in (...))` de ct_agg est introuvable "
        "dans la migration — s'il a été renommé ou réécrit, ce test doit suivre, "
        "pas être supprimé"
    )
    return frozenset(re.findall(r"'([^']+)'", m.group(1)))


@pytest.mark.skipif(
    MIGRATION is None,
    reason=(
        "migrations absentes — `mcp-server` est déployé seul sur Railway, sans "
        "le dépôt `infra` autour. Le test n'a de sens qu'en local, où les deux "
        "coexistent."
    ),
)
def test_le_sql_et_python_disent_le_meme_triplet() -> None:
    from src.tools.send import CONTACT_STATUTS_DEMARCHABLES

    sql = _statuts_du_sql(MIGRATION.read_text(encoding="utf-8"))

    assert sql == set(CONTACT_STATUTS_DEMARCHABLES), (
        "DÉSACCORD entre la vue SQL et send.py.\n"
        f"  SQL    ({MIGRATION.name}, ct_agg)      : {sorted(sql)}\n"
        f"  Python (send.CONTACT_STATUTS_DEMARCHABLES) : "
        f"{sorted(CONTACT_STATUTS_DEMARCHABLES)}\n"
        "Corriger les DEUX, jamais ce test. Et si le triplet change, refaire "
        "tourner le backfill n'y suffira pas : la vue se recalcule seule, mais "
        "les décisions déjà prises par send.py, non."
    )


def test_le_triplet_python_ne_derive_pas_en_silence() -> None:
    """Épingle la valeur elle-même.

    Le test ci-dessus est relatif : quelqu'un qui élargit le triplet des DEUX
    côtés le garderait vert tout en ouvrant l'envoi à des contacts déjà
    démarchés. Celui-ci force à venir lire les conséquences avant d'élargir.
    """
    from src.tools.send import CONTACT_STATUTS_DEMARCHABLES

    assert set(CONTACT_STATUTS_DEMARCHABLES) == {"new", "ready", "researching"}, (
        "Le triplet a changé. Avant de mettre ce test à jour, vérifier que le "
        "nouveau statut ne désigne pas quelqu'un à qui on a DÉJÀ écrit "
        "('contacted'), qui a DÉJÀ répondu ('replied', 'qualified', 'booked') "
        "ou qu'on a écarté ('disqualified', 'opted_out', 'bounced'). Un cold "
        "email de trop à un prospect en conversation, c'est ce qui fait dire "
        "« c'est du spam »."
    )
