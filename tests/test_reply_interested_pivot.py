"""PT1 — la branche `interested` de WF-7 après le pivot tri :
garde désabonnement AVANT toute écriture, marqueur interested_at idempotent,
plus aucune chaîne auto-reply/composer.

Contient aussi le contrat « le journal ne dit QUE ce qui a réussi », épinglé
sur toutes les branches qui pinguent Slack : `interested` (hot lead), `other`
(review manuel), `unsubscribe` (un intéressé qui se désabonne) et
`not_interested` (réponse négative — AC2, 2026-08-24)."""
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
    `updates` (table, patch), `inserts` (table, row — c'est là que passe l'upsert
    de conversation, donc le seul endroit où se lit l'état `cold`/`hot`/`lost`).
    `notify_par_categorie` mappe la catégorie Slack vers le booléen que `notify`
    doit rendre (défaut : True partout).

    `interested_at` = valeur rendue par la lecture ciblée `select=interested_at`
    (branche unsubscribe) : None = ce contact n'avait jamais dit oui."""
    from src import supabase_client
    from src.lib import slack as slack_mod
    from src.tools import reply

    notify_par_categorie = notify_par_categorie or {}
    vu: dict = {"notifies": [], "updates": [], "inserts": []}

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
        vu["inserts"].append((table, row))
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


# =====================================================================
# La branche `not_interested` — plus rien ne disparaît en silence (AC2)
# =====================================================================

# Volontairement PAS un « non merci » : le prompt du classifieur range aussi
# « on gère ça à l'interne » et « recontactez-moi dans 6 mois » dans
# `not_interested`. Ce sont des objections traitables, et elles partaient au
# cimetière sans un mot. Le tri fin est AC2 ; ici on rend seulement l'extrait
# visible pour que William tranche lui-même.
#
# ⚠️ Formulé avec des mots qu'on ne trouve NULLE PART ailleurs dans le bloc : le
# ping contient aussi une ligne d'exemples (« on gère ça à l'interne »,
# « rappelez-moi dans 6 mois »). Un extrait rédigé avec ces mots-là rendait le
# test increvable — il matchait le texte fixe même quand l'extrait réel était
# vide. Vérifié par mutation : `reply_preview=""` doit faire rougir.
_REFUS_DOUX = (
    "Merci, mais on s'occupe de tout ça nous-mêmes. Reparlez-m'en au printemps."
)


async def test_une_reponse_negative_est_signalee_avec_son_extrait(monkeypatch):
    """L'extrait EST le service rendu : sans lui, le ping dirait seulement
    « quelqu'un a dit non » et William ne pourrait pas distinguer un refus franc
    d'un « rappelez-moi en janvier »."""
    from src.tools import reply

    vu = _wire_interested(monkeypatch, categorie="not_interested")
    out = await reply.handle_reply(_payload(_REFUS_DOUX))

    assert out.category == "not_interested"
    assert "slack_not_interested" in out.actions_taken
    leads = [n for n in vu["notifies"] if n["category"] == "leads"]
    assert len(leads) == 1
    assert leads[0]["context"] == "wf7_not_interested"
    corps = str(leads[0]["blocks"])
    assert "on s'occupe de tout ça nous-mêmes" in corps
    assert "au printemps" in corps
    assert "jean@x.ca" in corps
    assert "Jean Roy" in corps
    assert "Plomberie X" in corps
    assert "93" in corps  # la confiance du classifieur, en clair


async def test_le_ping_dit_letat_reel_et_ne_promet_aucune_relance(monkeypatch):
    """Le contact EST disqualifié. Un ping qui dirait « à relancer » ou « en
    attente » ferait attendre William d'un système qui ne fera plus rien."""
    from src.tools import reply

    vu = _wire_interested(monkeypatch, categorie="not_interested")
    await reply.handle_reply(_payload(_REFUS_DOUX))

    corps = str([n for n in vu["notifies"] if n["category"] == "leads"][0]["blocks"])
    assert "disqualified" in corps
    assert "recontactera plus" in corps
    for mensonge in ("à relancer", "en attente", "file de reprise"):
        assert mensonge not in corps.lower(), mensonge


async def test_le_contact_reste_disqualifie_et_la_conversation_froide(monkeypatch):
    """Non-régression : on AJOUTE de la visibilité, on ne change pas le
    comportement. Le changement de comportement, c'est AC2."""
    from src.tools import reply

    vu = _wire_interested(monkeypatch, categorie="not_interested")
    out = await reply.handle_reply(_payload(_REFUS_DOUX))

    assert "contact_disqualified" in out.actions_taken
    assert ("contacts", {"status": "disqualified"}) in vu["updates"]
    conversations = [row for table, row in vu["inserts"] if table == "conversations"]
    assert len(conversations) == 1
    assert conversations[0]["state"] == "cold"


async def test_un_ping_de_refus_rate_ne_sinscrit_pas_comme_envoye(monkeypatch):
    """Même contrat d'honnêteté que ses voisines. Pas de repli sur #alertes :
    un prospect qui dit non n'est pas une panne (choix déjà fait pour la
    branche `other`)."""
    from src.tools import reply

    vu = _wire_interested(monkeypatch, categorie="not_interested",
                          notify_par_categorie={"leads": False})
    out = await reply.handle_reply(_payload(_REFUS_DOUX))

    assert "slack_not_interested_ping_failed" in out.actions_taken
    assert "slack_not_interested" not in out.actions_taken
    assert not [n for n in vu["notifies"] if n["category"] == "alerts"]
    # la disqualification, elle, part quand même — le ping n'est pas une garde
    assert "contact_disqualified" in out.actions_taken


async def test_une_disqualification_ratee_ne_sinscrit_pas_comme_faite(monkeypatch):
    """Le journal ne dit QUE ce qui a réussi — même contrat que la branche
    `interested`. Si l'écriture du statut échoue, le contact reste `contacted`
    et redevient éligible à un envoi ; un `contact_disqualified` inscrit quand
    même ferait croire la porte fermée le jour où ce lead reçoit un courriel de
    trop. C'est le mode d'échec que le projet a déjà payé avec le désabonnement
    qui répondait « c'est fait » sans rien enregistrer."""
    from src.tools import reply

    vu = _wire_interested(monkeypatch, categorie="not_interested",
                          contacts_update_boom=True)
    out = await reply.handle_reply(_payload(_REFUS_DOUX))

    assert "contact_disqualified_failed" in out.actions_taken
    assert "contact_disqualified" not in out.actions_taken
    # L'écriture a bien été TENTÉE : le test doit rougir sur le journal, pas sur
    # une branche qui aurait simplement cessé d'écrire.
    assert ("contacts", {"status": "disqualified"}) in vu["updates"]
    # Le ping, lui, part quand même — c'est le seul moyen que William le voie.
    assert "slack_not_interested" in out.actions_taken


async def test_une_absence_du_bureau_ne_pingue_toujours_rien(monkeypatch):
    """Garde-fou de périmètre : le nouveau ping ne doit pas déborder sur la
    seule branche encore volontairement muette. Un répondeur d'absence qui
    sonne sur #leads, c'est du bruit pur."""
    from src.tools import reply

    vu = _wire_interested(monkeypatch, categorie="out_of_office")
    out = await reply.handle_reply(_payload("Je suis absent jusqu'au 5 septembre."))

    assert out.category == "out_of_office"
    assert "ooo_logged" in out.actions_taken
    assert vu["notifies"] == []
    assert not [t for t, _ in vu["updates"] if t == "contacts"]


def test_not_interested_blocks_portent_letat_reel():
    from src.lib import slack

    fallback, blocks = slack.build_not_interested_blocks(
        contact_name="Jean Roy",
        company_name="Plomberie X",
        contact_email="jean@plomberiex.ca",
        # Mots absents de la ligne d'exemples du bloc — sinon l'assertion
        # ci-dessous passerait même avec un extrait vide (cf. _REFUS_DOUX).
        reply_preview="On s'occupe de tout ça nous-mêmes.",
        confidence=0.88,
        track="agence-ia",
    )
    joined = str(blocks)
    assert "[AGENCE-IA]" in fallback  # même préfixe de track que ses voisins
    assert "Jean Roy" in fallback and "Plomberie X" in fallback
    assert "jean@plomberiex.ca" in joined
    assert "s'occupe de tout ça nous-mêmes" in joined
    assert "88" in joined            # la confiance, en clair
    assert "disqualified" in joined  # ce que le système a VRAIMENT fait


def test_not_interested_blocks_ont_un_en_tete_et_bornent_lextrait():
    """Deux garde-fous que `build_hot_lead_blocks` et
    `build_interested_unsubscribed_blocks` ont déjà et qui manquaient ici.

    L'en-tête : sans lui le ping arrive dans #leads sans titre, à côté des
    « 🔥 Hot lead » — or les deux appellent des gestes opposés, et c'est
    justement le titre qui les sépare d'un coup d'œil.

    La borne sur l'extrait : une réponse de prospect cite très souvent tout le
    fil au complet. Non borné, l'extrait pousse le bloc au-delà de la limite de
    Slack, qui coupe alors où il veut — au pire, l'API refuse le message et le
    ping est perdu, ce qui ramène exactement le silence qu'on vient de boucher.
    """
    from src.lib import slack

    _, blocks = slack.build_not_interested_blocks(
        contact_name="A", company_name="B", contact_email="a@b.ca",
        reply_preview="x" * 4000, confidence=0.5, track="agence-ia",
    )
    assert blocks[0]["type"] == "header"
    assert "[AGENCE-IA]" in blocks[0]["text"]["text"]

    extrait = [b for b in blocks if "Sa réponse" in str(b.get("text", ""))]
    assert len(extrait) == 1
    texte = extrait[0]["text"]["text"]
    assert "…" in texte              # coupé par nous, pas par Slack
    assert len(texte) < 600


def test_not_interested_blocks_disent_a_qui_revient_le_geste():
    """Dire l'état sans dire à qui revient la suite, c'est se lire comme un
    accusé de réception : « c'est réglé, rien à faire ». Or c'est faux — la
    moitié de ces réponses sont des objections traitables, et tant qu'AC2
    n'existe pas, PERSONNE ne reprendra le lead si William ne le fait pas.
    La phrase qui lui rend la main est donc une exigence, pas un ornement.
    """
    from src.lib import slack

    _, blocks = slack.build_not_interested_blocks(
        contact_name="A", company_name="B", contact_email="a@b.ca",
        reply_preview="Pas intéressé.", confidence=0.99,
    )
    corps = str(blocks)
    assert "t'appartient" in corps
    # …sans jamais retomber dans la promesse d'une file qui n'existe pas.
    for mensonge in ("à relancer", "en attente", "file de reprise"):
        assert mensonge not in corps.lower(), mensonge
