"""Un modèle peut bavarder autour de son JSON — le travail se fait quand même.

🔴 PANNE RÉELLE DU 2026-09-17, 12 h 33. Alerte Slack :
`WF-4 — la rédaction échoue · 1 échec(s), 9 brouillon(s) écrit(s) sur 10`,
détail `JSONDecodeError('Extra data: line 18 column 1 (char 2443)')`.

« Extra data » veut dire que le modèle a rendu son objet PUIS autre chose.
L'ancienne version le prévoyait à moitié : `json.loads` échouait, et le repli
prenait « tout entre la première et la dernière accolade ». Quand ce qui suit
est un SECOND objet, ce repli les avale tous les deux — et deux objets collés
ne forment pas un objet valide non plus. Le `json.loads` du repli n'était dans
aucun `try` : l'erreur sortait et le courriel était perdu.

⚠️ Ce qui a rendu le défaut invisible pendant des mois : le cas « objet + prose
sans accolade » passait très bien. Le repli marchait la plupart du temps.

🔴 ET IL Y AVAIT CINQ COPIES, pas une. Un conseil de relecture l'a montré le
jour même : `research.py`, `meeting.py`, `reply.py` et `compliance.py` portaient
le même corps, mot pour mot. La même réponse qui a fait tomber le rédacteur
faisait tomber le juge de conformité et le classement des réponses. C'est la
duplication qui était le défaut ; il n'y a plus qu'une implémentation,
`lib/json_du_modele`, et cinq noms qui y mènent.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from src.lib.json_du_modele import objet_json_du_modele

ATTENDU = {"email_subject": "objet", "email_body": "corps"}
_JSON = json.dumps(ATTENDU, ensure_ascii=False)


@pytest.mark.parametrize(
    ("forme", "texte"),
    [
        ("objet seul", _JSON),
        ("entouré de balises de code", f"```json\n{_JSON}\n```"),
        ("objet puis de la prose", f"{_JSON}\n\nVoilà le courriel."),
        # Le piège discret : la prose qui suit contient une accolade, donc
        # l'ancien repli « jusqu'à la dernière accolade » emportait la phrase.
        ("objet puis prose AVEC accolade", f"{_JSON}\nNote : {{voir plus haut}}"),
        # 🔴 LA PANNE DU 2026-09-17, à la lettre.
        ("DEUX objets à la suite", f'{_JSON}\n{{"note": "bis"}}'),
        ("prose avant l'objet", f"Voici ce que je propose :\n{_JSON}"),
        # 🔴 LE CAS QUE LE PREMIER CORRECTIF PERDAIT ENCORE, et il n'a rien de
        # théorique : le gabarit du rédacteur est bâti sur des jetons à
        # accolades — {OUVREUR_A}, {VILLE}, {ANCRE_A}, {NOTE} — que le modèle
        # manipule pendant toute sa réponse. Ne tenter que la PREMIÈRE accolade
        # suffisait à reperdre le brouillon.
        ("jeton de gabarit avant l'objet", f"Gabarit A, {{OUVREUR_A}} rempli :\n{_JSON}"),
        # Une accolade DANS une valeur : aucune expression régulière ne sait
        # qu'elle ne ferme rien. Il FAUT du bavardage après pour que le cas
        # distingue quoi que ce soit — sans lui, l'ancienne version passait
        # aussi, et le test ne prouvait rien. Corrigé le 2026-09-17.
        (
            "accolade dans une chaîne, puis bavardage",
            json.dumps(
                {"email_subject": "objet", "email_body": "} pas la fin"},
                ensure_ascii=False,
            )
            + "\nVoilà.",
        ),
    ],
)
def test_le_premier_objet_complet_est_lu(forme: str, texte: str) -> None:
    lu = objet_json_du_modele(texte)
    assert lu["email_subject"] == "objet", forme
    assert "email_body" in lu, forme


@pytest.mark.parametrize(
    ("forme", "texte"),
    [
        ("aucun objet", "je n'ai pas compris la demande"),
        ("objet jamais refermé", '{"email_subject": "objet"'),
        # Une liste est du JSON valide, mais les cinq appelants lisent des clés
        # (`.get("subject")`, `.get("verdict")`) : la laisser passer déplacerait
        # la panne là où elle serait bien plus dure à rattacher à sa cause.
        ("une liste au lieu d'un objet", "[1, 2, 3]"),
        ("une chaîne au lieu d'un objet", '"désolé, je ne peux pas"'),
    ],
)
def test_ce_qui_n_est_pas_un_objet_est_refuse_clairement(
    forme: str, texte: str
) -> None:
    with pytest.raises(ValueError):
        objet_json_du_modele(texte)


def test_le_message_dit_QUI_a_recu_la_reponse_illisible() -> None:
    """Cinq appelants partagent le code : sans l'étiquette, l'erreur dans
    `agent_runs` ne dirait pas si c'est le rédacteur, le juge ou la recherche."""
    with pytest.raises(ValueError, match="compliance"):
        objet_json_du_modele("rien ici", source="compliance")


# ── Le témoin négatif, et la garde contre le retour des copies ──────────────


def test_la_panne_du_2026_09_17_est_bien_celle_qu_on_a_corrigee() -> None:
    """Sans lui, les cas du dessus pourraient tous passer pour une raison sans
    rapport avec le défaut réparé.

    On rejoue l'ANCIEN découpage — de la première à la dernière accolade — et on
    vérifie qu'il échouait bien sur les DEUX formes que le correctif apporte. Si
    ce témoin devient vert, c'est que les formes reproduites ne sont plus celles
    qui faisaient tomber WF-4.
    """
    for forme in (
        f'{_JSON}\n{{"note": "bis"}}',                      # deux objets
        f"{_JSON}\nNote : {{voir plus haut}}",              # accolade après
    ):
        tranche = forme[forme.index("{"): forme.rindex("}") + 1]
        with pytest.raises(json.JSONDecodeError):
            json.loads(tranche)
        assert objet_json_du_modele(forme)["email_subject"] == "objet"


def test_personne_ne_reecrit_un_analyseur_dans_son_coin() -> None:
    """🔴 LA GARDE QUI COMPTE, parce que le défaut n'était pas le motif — c'était
    qu'il avait été COPIÉ cinq fois.

    Le correctif du 2026-09-17 a d'abord été écrit dans `personalize.py` seul.
    Quatre autres modules portaient le même corps intact, dont le juge de
    conformité. Ce test échoue si une sixième copie apparaît.
    """
    src = Path(__file__).resolve().parent.parent / "src"
    coupables = []
    for f in src.rglob("*.py"):
        if f.name == "json_du_modele.py":
            continue
        texte = f.read_text(encoding="utf-8")
        if "json.loads(match.group(0))" in texte or r're.search(r"\{.*\}"' in texte:
            coupables.append(f.relative_to(src).as_posix())
    assert not coupables, (
        "un analyseur JSON local est réapparu dans : "
        f"{coupables}. Aucune expression régulière ne sait où se ferme un objet "
        "JSON — il faut compter les accolades ET savoir lesquelles sont dans une "
        "chaîne. Appelle `lib.json_du_modele.objet_json_du_modele`."
    )


def test_les_cinq_appelants_passent_bien_par_le_module_partage() -> None:
    """La contre-épreuve du test précédent. Sans elle, supprimer purement et
    simplement les cinq fonctions le rendrait vert."""
    src = Path(__file__).resolve().parent.parent / "src" / "tools"
    attendus = {
        "research.py", "meeting.py", "reply.py",
        "compliance.py", "personalize.py",
    }
    branches = {
        f.name
        for f in src.glob("*.py")
        if "objet_json_du_modele(" in f.read_text(encoding="utf-8")
    }
    assert attendus <= branches, f"ne délèguent plus : {sorted(attendus - branches)}"


def test_le_module_partage_compile_sans_import_mort() -> None:
    """Les imports ont été posés par script, après une première tentative qui
    les avait glissés AU MILIEU d'un import multi-ligne (`meeting.py` ne se
    chargeait plus). Ce test relit l'arbre plutôt que le texte."""
    src = Path(__file__).resolve().parent.parent / "src" / "tools"
    for nom in ("research.py", "meeting.py", "reply.py", "compliance.py",
                "personalize.py"):
        arbre = ast.parse((src / nom).read_text(encoding="utf-8"))
        importe = any(
            isinstance(n, ast.ImportFrom)
            and n.module
            and "json_du_modele" in n.module
            for n in ast.walk(arbre)
        )
        assert importe, f"{nom} appelle la fonction sans l'importer"
