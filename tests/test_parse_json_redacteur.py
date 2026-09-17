"""Le rédacteur peut bavarder après son JSON — le brouillon s'écrit quand même.

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
sans accolade » passait très bien. Le repli marchait la plupart du temps, donc
personne ne l'a soupçonné — jusqu'au jour où le modèle a répondu deux fois.

Ce fichier fixe les SEPT formes que le rédacteur a le droit de rendre, et les
deux qu'il doit se faire refuser.
"""
from __future__ import annotations

import json

import pytest

from src.tools.personalize import _parse_json

ATTENDU = {"email_subject": "objet", "email_body": "corps"}
_JSON = json.dumps(ATTENDU, ensure_ascii=False)


@pytest.mark.parametrize(
    ("forme", "texte"),
    [
        ("objet seul", _JSON),
        ("entouré de balises de code", f"```json\n{_JSON}\n```"),
        ("objet puis de la prose", f"{_JSON}\n\nVoilà le courriel."),
        # Celui-ci est le piège discret : la prose qui suit contient une
        # accolade, donc l'ancien repli « jusqu'à la dernière accolade »
        # emportait la phrase avec.
        ("objet puis prose AVEC accolade", f"{_JSON}\nNote : {{voir plus haut}}"),
        # 🔴 LA PANNE DU 2026-09-17, à la lettre.
        ("DEUX objets à la suite", f'{_JSON}\n{{"note": "bis"}}'),
        ("prose avant l'objet", f"Voici ce que je propose :\n{_JSON}"),
        # Une accolade DANS une valeur : aucune expression régulière ne sait
        # qu'elle ne ferme rien. C'est pour ça que le découpage appartient à
        # l'analyseur JSON et pas à un motif.
        ("accolade à l'intérieur d'une chaîne", json.dumps(
            {"email_subject": "objet", "email_body": "} pas la fin"},
            ensure_ascii=False,
        )),
    ],
)
def test_le_premier_objet_complet_est_lu(forme: str, texte: str) -> None:
    lu = _parse_json(texte)
    assert lu["email_subject"] == "objet", forme
    assert "email_body" in lu, forme


@pytest.mark.parametrize(
    ("forme", "texte"),
    [
        ("aucun objet", "je n'ai pas compris la demande"),
        ("objet jamais refermé", '{"email_subject": "objet"'),
        # Une liste est du JSON valide, mais le reste du code lit des clés :
        # la laisser passer déplacerait la panne plus loin, où elle serait
        # beaucoup plus dure à rattacher à sa cause.
        ("une liste au lieu d'un objet", "[1, 2, 3]"),
    ],
)
def test_ce_qui_n_est_pas_un_objet_est_refuse_clairement(
    forme: str, texte: str
) -> None:
    with pytest.raises(ValueError):
        _parse_json(texte)


def test_la_panne_du_2026_09_17_est_bien_celle_qu_on_a_corrigee() -> None:
    """Le témoin négatif : sans lui, les cas du dessus pourraient tous passer
    pour une raison sans rapport avec le défaut réparé.

    On reconstitue ici l'ANCIEN comportement — `json.loads` sur tout ce qui va
    de la première à la dernière accolade — et on vérifie qu'il échouait bien
    sur cette forme-là. Si un jour ce test devient vert des deux côtés, c'est
    que la forme reproduite n'est plus celle qui a fait tomber WF-4.
    """
    casse = f'{_JSON}\n{{"note": "bis"}}'

    with pytest.raises(json.JSONDecodeError) as capture:
        json.loads(casse[casse.index("{"): casse.rindex("}") + 1])
    assert "Extra data" in str(capture.value)

    assert _parse_json(casse)["email_subject"] == "objet"
