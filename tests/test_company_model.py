"""Tests du modèle CompanyIn (insert sourcing).

estimated_employees (int) était une colonne morte (0 lecteur, jamais remplie
au sourcing). Retirée du modèle en même temps que le DROP de la colonne SQL
(migration 0015). insert_company fait model_dump(exclude_none=False) : tant
que le champ existe, la clé part dans l'INSERT companies — incompatible avec
le DROP de la colonne.
"""
from __future__ import annotations

import src.tools.db as dbt


def test_company_in_n_a_pas_estimated_employees() -> None:
    assert "estimated_employees" not in dbt.CompanyIn.model_fields


def test_company_in_dump_n_envoie_pas_estimated_employees() -> None:
    row = dbt.CompanyIn(name="Test Co").model_dump(exclude_none=False)
    assert "estimated_employees" not in row
