"""PT1 — la branche `interested` de WF-7 après le pivot tri :
garde désabonnement AVANT toute écriture, marqueur interested_at idempotent,
plus aucune chaîne auto-reply/composer.

Contient aussi le contrat « le journal ne dit QUE ce qui a réussi », épinglé
sur les deux branches qui pinguent Slack : `interested` (hot lead) et `other`
(review manuel)."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


async def test_suppressed_par_statut_contact(monkeypatch):
    from src import supabase_client
    from src.tools import reply

    async def fake_select(table, params=None, **kw):
        assert table == "contacts"
        return [{"status": "opted_out"}]

    monkeypatch.setattr(supabase_client, "select", fake_select)
    assert await reply._interested_lead_is_suppressed("ct-1", "a@b.ca") is True


async def test_suppressed_par_liste(monkeypatch):
    from src import supabase_client
    from src.tools import reply

    async def fake_select(table, params=None, **kw):
        if table == "contacts":
            return [{"status": "replied"}]
        assert table == "suppression_list"
        return [{"reason": "opt_out"}]

    monkeypatch.setattr(supabase_client, "select", fake_select)
    assert await reply._interested_lead_is_suppressed("ct-1", "a@b.ca") is True


async def test_pas_suppressed(monkeypatch):
    from src import supabase_client
    from src.tools import reply

    async def fake_select(table, params=None, **kw):
        return [{"status": "contacted"}] if table == "contacts" else []

    monkeypatch.setattr(supabase_client, "select", fake_select)
    assert await reply._interested_lead_is_suppressed("ct-1", "a@b.ca") is False


async def test_fail_open_sur_erreur_de_lecture(monkeypatch):
    from src import supabase_client
    from src.tools import reply

    async def boom(table, params=None, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(supabase_client, "select", boom)
    # None = « je n'ai pas pu vérifier », distinct de False = « vérifié, pas
    # désabonné ». None est FALSY : le fail-open tient tel quel (un ping de trop,
    # William arbitre, vaut mieux qu'un hot lead perdu sur une panne de lecture),
    # mais l'appelant peut désormais avertir dans le ping que la vérif est morte.
    assert await reply._interested_lead_is_suppressed("ct-1", "a@b.ca") is None


async def test_marqueur_pose_avec_filtre_idempotent(monkeypatch):
    from src import supabase_client
    from src.tools import reply

    captured: dict = {}

    async def fake_update(table, patch, filters=None, **kw):
        captured["table"] = table
        captured["patch"] = patch
        captured["filters"] = filters
        return [{}]

    monkeypatch.setattr(supabase_client, "update", fake_update)
    await reply._mark_contact_interested("ct-1")
    assert captured["table"] == "contacts"
    assert "interested_at" in captured["patch"]
    assert captured["filters"]["id"] == "eq.ct-1"
    assert captured["filters"]["interested_at"] == "is.null"


async def test_marqueur_sans_contact_id_ne_lit_pas_la_base(monkeypatch):
    from src import supabase_client
    from src.tools import reply

    async def boom(*a, **kw):
        raise AssertionError("aucun appel DB attendu")

    monkeypatch.setattr(supabase_client, "update", boom)
    await reply._mark_contact_interested(None)  # ne lève pas


def test_chaine_composer_retiree():
    from src.tools import reply

    for symbole in (
        "_call_composer",
        "_count_prior_auto_replies",
        "AUTO_REPLY_CONFIDENCE_THRESHOLD",
        "MAX_AUTO_REPLIES_PER_CONVERSATION",
    ):
        assert not hasattr(reply, symbole), symbole


def test_prompt_composer_supprime():
    from pathlib import Path

    from src.tools import reply

    # Ancré sur le module (pas le cwd) — robuste peu importe d'où pytest tourne.
    prompts = Path(reply.__file__).resolve().parents[1] / "prompts"
    assert not (prompts / "reply_compose.md").exists()
    assert (prompts / "reply_classifier.md").exists()


# =====================================================================
# C4 — le journal d'actions ne dit QUE ce qui a réussi (branche interested)
# =====================================================================

_RESEARCH = {
    "company_summary": "Plomberie familiale à Montréal, service 24/7.",
    "personalization_hooks": ["Leads du soir non confirmés"],
}


def _wire_interested(monkeypatch, *, notify_par_categorie=None, contacts_update_boom=False,
                     categorie="interested", interested_at=None):
    """Monte un `handle_reply` complet qui atterrit dans la branche `categorie`
    (défaut : `interested` ; `other` pour le review manuel ; `unsubscribe`).

    Retourne un dict d'observations : `notifies` (un dict par appel à notify),
    `updates` (table, patch). `notify_par_categorie` mappe la catégorie Slack
    vers le booléen que `notify` doit rendre (défaut : True partout).

    `interested_at` = valeur rendue par la lecture ciblée `select=interested_at`
    (branche unsubscribe) : None = ce contact n'avait jamais dit oui."""
    from src import supabase_client
    from src.lib import slack as slack_mod
    from src.tools import reply

    notify_par_categorie = notify_par_categorie or {}
    vu: dict = {"notifies": [], "updates": []}

    async def fake_select(table, *, params=None, schema=None):
        p = params or {}
        if table == "messages":
            if p.get("direction") == "eq.inbound":
                return []  # pas de doublon
            return [{"id": "m-parent", "contact_id": "ct-1", "campaign_id": "camp-1",
                     "body_text": "Notre courriel d'origine."}]
        if table == "contacts":
            if "email" in p:  # _find_contact_by_email
                return [{"id": "ct-1", "company_id": "co-1", "first_name": "Jean",
                         "last_name": "Roy", "email": "jean@x.ca", "status": "contacted"}]
            if p.get("select") == "interested_at":  # _contact_interested_at
                return [{"interested_at": interested_at}]
            return [{"status": "contacted"}]  # garde désabonnement
        if table == "suppression_list":
            return []
        if table == "companies":
            return [{"id": "co-1", "name": "Plomberie X", "website": "https://x.ca",
                     "track": "agence-ia", "research_json": _RESEARCH}]
        if table == "conversations":
            return []  # pas de RDV déjà booké
        raise AssertionError(f"select inattendu sur {table!r}")

    async def fake_insert(table, row, **kw):
        return [{"id": {"messages": "in-1", "agent_runs": "ar-1"}.get(table, "x-1")}]

    async def fake_update(table, patch, *, filters=None, schema=None):
        vu["updates"].append((table, patch))
        if contacts_update_boom and table == "contacts":
            raise RuntimeError("db down")
        return [{}]

    async def fake_notify(*, text, blocks=None, context=None, category=None):
        vu["notifies"].append({"text": text, "blocks": blocks,
                               "context": context, "category": category})
        return notify_par_categorie.get(category, True)

    def fake_classifier(reply_text, *, original_email_text, model):
        return ({"category": categorie, "confidence": 0.93,
                 "reasoning_one_line": "réponse ambiguë"}, {"input_tokens": 1,
                "output_tokens": 1, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0})

    monkeypatch.setattr(supabase_client, "select", fake_select)
    monkeypatch.setattr(supabase_client, "insert", fake_insert)
    monkeypatch.setattr(supabase_client, "update", fake_update)
    monkeypatch.setattr(slack_mod, "notify", fake_notify)
    monkeypatch.setattr(reply, "_call_classifier", fake_classifier)
    return vu


def _payload(corps="Oui, montrez-moi ça."):
    from src.tools import reply

    return reply.HandleReplyIn(
        lead_email="jean@x.ca",
        reply_subject="Re: votre site",
        reply_body_text=corps,
        provider_message_id_inbound="inb-1",
        provider_message_id_parent="out-1",
    )


async def test_le_chemin_heureux_journalise_les_trois_actions(monkeypatch):
    from src.tools import reply

    _wire_interested(monkeypatch)
    out = await reply.handle_reply(_payload())

    assert out.category == "interested"
    assert "contact_replied" in out.actions_taken
    assert "contact_interested" in out.actions_taken
    assert "slack_hot_lead" in out.actions_taken
    assert not [a for a in out.actions_taken if a.endswith("_failed")]


async def test_un_ping_rate_ne_sinscrit_pas_comme_envoye(monkeypatch):
    """Le ping EST la file de travail : s'il ne passe pas, le journal doit le
    dire et une alerte doit partir — sinon le hot lead disparaît en silence."""
    from src.tools import reply

    vu = _wire_interested(monkeypatch, notify_par_categorie={"leads": False})
    out = await reply.handle_reply(_payload())

    assert "slack_ping_failed" in out.actions_taken
    assert "slack_hot_lead" not in out.actions_taken
    assert "alert_ping_sent" in out.actions_taken
    alerte = [n for n in vu["notifies"] if n["category"] == "alerts"]
    assert len(alerte) == 1
    assert "ct-1" in alerte[0]["text"]
    assert "jean@x.ca" in alerte[0]["text"]


async def test_une_alerte_de_repli_ratee_ne_sinscrit_pas(monkeypatch):
    """Slack entièrement mort : `slack_ping_failed` reste, `alert_ping_sent` non."""
    from src.tools import reply

    _wire_interested(monkeypatch, notify_par_categorie={"leads": False, "alerts": False})
    out = await reply.handle_reply(_payload())

    assert "slack_ping_failed" in out.actions_taken
    assert "alert_ping_sent" not in out.actions_taken


async def test_une_ecriture_ratee_est_journalisee_comme_ratee(monkeypatch):
    """Sans ça, `contact_interested` mentait : le marqueur n'existe pas en base,
    donc le lead n'entre jamais dans le compteur « en attente de site »."""
    from src.tools import reply

    _wire_interested(monkeypatch, contacts_update_boom=True)
    out = await reply.handle_reply(_payload())

    assert "contact_interested_failed" in out.actions_taken
    assert "contact_interested" not in out.actions_taken
    assert "contact_replied_failed" in out.actions_taken
    assert "contact_replied" not in out.actions_taken
    # le ping part quand même — c'est le seul moyen que William le voie
    assert "slack_hot_lead" in out.actions_taken


async def test_le_ping_porte_le_brief_de_recherche(monkeypatch):
    """C1 — le brief voyage AVEC le ping (`companies.research_json` n'était
    qu'une colonne morte du select)."""
    from src.tools import reply

    vu = _wire_interested(monkeypatch)
    await reply.handle_reply(_payload())

    leads = [n for n in vu["notifies"] if n["category"] == "leads"]
    assert len(leads) == 1
    corps = str(leads[0]["blocks"])
    assert "Brief pré-RDV" in corps
    assert "Plomberie familiale" in corps


async def test_verif_desabonnement_en_panne_est_dite_au_journal_et_au_ping(monkeypatch):
    """C4d — fail-open conservé (le lead passe) mais jamais en silence."""
    from src.tools import reply

    vu = _wire_interested(monkeypatch)
    monkeypatch.setattr(reply, "_interested_lead_is_suppressed",
                        lambda *a, **kw: _none())
    out = await reply.handle_reply(_payload())

    assert "suppression_check_failed" in out.actions_taken
    assert "skipped_interested_suppressed" not in out.actions_taken  # fail-open
    assert "slack_hot_lead" in out.actions_taken
    corps = str([n for n in vu["notifies"] if n["category"] == "leads"][0]["blocks"])
    assert "vérif désabonnement en panne" in corps


async def _none():
    return None


# =====================================================================
# La branche `other` (review manuel) — dernière poche du journal malhonnête
# =====================================================================

async def test_review_journalise_le_ping_seulement_sil_est_passe(monkeypatch):
    from src.tools import reply

    vu = _wire_interested(monkeypatch, categorie="other")
    out = await reply.handle_reply(_payload())

    assert out.category == "other"
    assert "slack_review" in out.actions_taken
    review = [n for n in vu["notifies"] if n["context"] == "wf7_review"]
    assert len(review) == 1


async def test_un_ping_de_review_rate_ne_sinscrit_pas_comme_envoye(monkeypatch):
    """`slack_review` était inscrit sans lire le retour de `notify` : un ping
    perdu laissait croire que la réponse avait été mise en file de review.
    Pas de repli sur #alertes ici — c'est du review manuel, pas un hot lead ;
    seule l'honnêteté du journal est en jeu."""
    from src.tools import reply

    vu = _wire_interested(monkeypatch, categorie="other",
                          notify_par_categorie={"leads": False})
    out = await reply.handle_reply(_payload())

    assert "slack_review_failed" in out.actions_taken
    assert "slack_review" not in out.actions_taken
    assert not [n for n in vu["notifies"] if n["category"] == "alerts"]


# =====================================================================
# La branche `unsubscribe` — visibilité « intéressé PUIS désabonné » (2026-08-23)
# =====================================================================

_DESABO = "Finalement, retirez-moi de votre liste svp."


async def test_desabonnement_dun_lead_jamais_interesse_ne_pingue_rien_de_plus(monkeypatch):
    """Cas de loin le plus courant : quelqu'un qui n'a jamais dit oui se
    désabonne. Aucun ping — sinon #leads devient du bruit et le signal se perd."""
    from src.tools import reply

    vu = _wire_interested(monkeypatch, categorie="unsubscribe", interested_at=None)
    out = await reply.handle_reply(_payload(_DESABO))

    assert out.category == "unsubscribe"
    assert "suppression_added" in out.actions_taken
    assert "contact_opted_out" in out.actions_taken
    assert not [a for a in out.actions_taken if a.startswith("interested_lead_unsubscribed")]
    assert vu["notifies"] == []


async def test_un_interesse_qui_se_desabonne_est_signale_avec_le_garde_lcap(monkeypatch):
    from src.tools import reply

    vu = _wire_interested(
        monkeypatch, categorie="unsubscribe",
        interested_at="2026-08-21T14:03:00+00:00",
    )
    out = await reply.handle_reply(_payload(_DESABO))

    assert "interested_lead_unsubscribed" in out.actions_taken
    leads = [n for n in vu["notifies"] if n["category"] == "leads"]
    assert len(leads) == 1
    corps = str(leads[0]["blocks"])
    assert "Ne PAS relancer par courriel" in corps  # l'interdit, en toutes lettres
    assert "LCAP" in corps and "LNNTE" in corps
    assert "2026-08-21" in corps                    # la date du oui
    assert "retirez-moi de votre liste" in corps    # l'extrait de sa réponse
    assert "jean@x.ca" in corps


async def test_la_conformite_du_desabonnement_reste_intacte(monkeypatch):
    """On AJOUTE une notification, on ne touche pas à la conformité : la ligne
    de suppression et le passage à opted_out partent avant le ping, et partent
    même si le ping meurt."""
    from src.tools import reply

    vu = _wire_interested(
        monkeypatch, categorie="unsubscribe",
        interested_at="2026-08-21T14:03:00+00:00",
        notify_par_categorie={"leads": False},
    )
    out = await reply.handle_reply(_payload(_DESABO))

    assert "suppression_added" in out.actions_taken
    assert "contact_opted_out" in out.actions_taken
    assert ("contacts", {"status": "opted_out"}) in vu["updates"]


async def test_un_ping_de_desabonnement_rate_ne_sinscrit_pas_comme_envoye(monkeypatch):
    from src.tools import reply

    _wire_interested(
        monkeypatch, categorie="unsubscribe",
        interested_at="2026-08-21T14:03:00+00:00",
        notify_par_categorie={"leads": False},
    )
    out = await reply.handle_reply(_payload(_DESABO))

    assert "interested_lead_unsubscribed_ping_failed" in out.actions_taken
    assert "interested_lead_unsubscribed" not in out.actions_taken


async def test_lecture_du_marqueur_en_panne_ne_casse_pas_le_desabonnement(monkeypatch):
    """La lecture est un confort ; le désabonnement, non. Une panne de lecture
    avale l'exception et le flux de conformité continue — mais elle se DIT au
    journal (`interested_at_read_failed`), sinon elle est indiscernable d'un
    désabonné qui n'avait jamais dit oui, et le ping manquant part sans trace.
    Même patron que `suppression_check_failed` sur la branche `interested`."""
    from src.tools import reply

    from src import supabase_client

    _wire_interested(monkeypatch, categorie="unsubscribe", interested_at=None)
    # Le select monté par _wire_interested, qu'on laisse répondre pour tout le
    # reste du flux — seule la lecture ciblée du marqueur tombe.
    select_monte = supabase_client.select

    async def select_qui_casse_la_lecture_ciblee(table, *, params=None, schema=None):
        if table == "contacts" and (params or {}).get("select") == "interested_at":
            raise RuntimeError("db down")
        return await select_monte(table, params=params, schema=schema)

    monkeypatch.setattr(supabase_client, "select", select_qui_casse_la_lecture_ciblee)
    out = await reply.handle_reply(_payload(_DESABO))

    assert out.status == "ok"
    assert "contact_opted_out" in out.actions_taken
    assert "interested_at_read_failed" in out.actions_taken
    assert not [a for a in out.actions_taken if a.startswith("interested_lead_unsubscribed")]


async def test_un_desabonne_sans_oui_ne_crie_pas_a_la_panne(monkeypatch):
    """Le contrepoids du test précédent : « il n'a jamais dit oui » est une
    lecture RÉUSSIE. Marquer `interested_at_read_failed` sur le cas de loin le
    plus courant noierait les vraies pannes."""
    from src.tools import reply

    _wire_interested(monkeypatch, categorie="unsubscribe", interested_at=None)
    out = await reply.handle_reply(_payload(_DESABO))

    assert "interested_at_read_failed" not in out.actions_taken


def test_hot_lead_blocks_nouvelle_signature():
    from src.lib import slack

    fallback, blocks = slack.build_hot_lead_blocks(
        contact_name="Jean Roy",
        company_name="Plomberie X",
        contact_email="jean@plomberiex.ca",
        reply_preview="Oui, montrez-moi ça",
        confidence=0.91,
        track="agence-ia",
        website="https://plomberiex.ca",
    )
    joined = str(blocks)
    assert "produire le site" in fallback.lower() or "produire le site" in joined.lower()
    assert "plomberiex.ca" in joined
    assert "Auto-reply" not in joined
