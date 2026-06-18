import pytest

from src.tools import brand_kit as BK


def test_flex_tool_schema_has_8_block_types():
    schema = BK._FLEX_TOOL["input_schema"]
    blocs = schema["properties"]["blocs"]["items"]
    # union fermée via oneOf, chacun discriminé par 'type'
    type_enums = set()
    for variant in blocs["oneOf"]:
        type_enums |= set(variant["properties"]["type"]["enum"])
    assert type_enums == {
        "titre", "texte", "liste", "image", "galerie", "stats", "cta", "faq",
    }
    # aucune clé d'URL libre : seuls des *_id pour les images
    dumped = str(schema)
    assert "url_id" in dumped
    assert "'url'" not in dumped  # pas de champ url libre dans le schéma d'entrée
