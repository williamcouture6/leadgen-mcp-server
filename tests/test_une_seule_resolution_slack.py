"""La résolution d'un webhook Slack vit à UN SEUL endroit.

🔴 CE FICHIER EXISTE À CAUSE D'UNE MESURE, le 2026-09-14. Un conseil de
relecture a compté **quatre** implémentations de « quelle variable
d'environnement porte le webhook de cette catégorie ? » dans ce dépôt — et l'une
d'elles avait **déjà divergé** : `/wf9/healthcheck` lisait
`os.environ.get("SLACK_WEBHOOK_BOOKINGS")` sans le `.strip()` que
`lib/slack._webhook_url` applique.

Conséquence mesurable : une variable valant `"   "` — le résultat très ordinaire
d'un copier-coller dans le tableau de bord Railway — faisait dire au healthcheck
`slack_bookings_configured: true` pendant que `notify` retombait sur le repli ou
ne partait pas du tout. Un healthcheck vert sur un canal mort, c'est-à-dire la
chose exacte qu'un healthcheck existe pour rendre impossible.

La duplication n'était donc pas un risque théorique : elle avait produit son
défaut. Ce test empêche la cinquième copie, parce qu'un test d'équivalence entre
deux fonctions (voir `test_alert_healthcheck.py`) ne peut rien contre une
troisième qu'il ne connaît pas.
"""
from __future__ import annotations

import re
from pathlib import Path

# Les lectures directes sont légitimes DANS le module qui définit la règle.
_MODULE_DE_LA_REGLE = "lib/slack.py"

# `os.environ.get("SLACK_WEBHOOK…")`, `os.environ["SLACK_WEBHOOK…"]`, et la
# forme par constante `os.environ.get(slack_lib.SLACK_WEBHOOK_ENV)` — c'est
# celle-là, la plus innocente à l'œil, qui portait la copie divergente.
_LECTURE_DIRECTE = re.compile(
    r"os\.environ(?:\.get)?[\(\[]\s*(?:[A-Za-z_][A-Za-z_0-9]*\.)?[\"']?SLACK_WEBHOOK"
)


def test_aucune_lecture_directe_d_un_webhook_hors_du_module_slack() -> None:
    """Passer par `is_configured` / `voie_du_canal` / `notify`, jamais par
    `os.environ`. Ces trois-là partagent une seule résolution, donc une seule
    règle de `.strip()`, une seule priorité, un seul repli."""
    src = Path(__file__).resolve().parent.parent / "src"
    fautifs: list[str] = []
    for f in sorted(src.rglob("*.py")):
        rel = f.relative_to(src).as_posix()
        if rel == _MODULE_DE_LA_REGLE:
            continue
        for num, ligne in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if _LECTURE_DIRECTE.search(ligne):
                fautifs.append(f"{rel}:{num}: {ligne.strip()}")

    assert not fautifs, (
        "Lecture directe d'un webhook Slack hors de lib/slack.py — c'est une "
        "copie de la résolution, et la dernière avait perdu son .strip() :\n  "
        + "\n  ".join(fautifs)
    )
