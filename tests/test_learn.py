from jobhunter import db, learn
from jobhunter.models import Job


def _add(conn, ext, title, company, desc, label, reasons=""):
    job = Job(source="wttj", external_id=ext, title=title, company=company,
              location="Paris", description=desc)
    jid, _ = db.upsert_job(conn, job, 50, "r")
    db.set_feedback(conn, jid, label, reasons)
    return jid


def test_insufficient_feedback(tmp_db):
    with db.connect() as conn:
        _add(conn, "1", "Blockchain Developer", "X", "web3 solidity", "dismissed")
        out = learn.mine_rules(conn)
    assert out["status"] == "insufficient" and out["new"] == 0


def test_mines_discriminative_terms_inactive(tmp_db):
    with db.connect() as conn:
        _add(conn, "1", "Blockchain Developer", "BadCorp",
             "We build web3 solidity smart contracts on ethereum.", "dismissed", "company,wrong_domain")
        _add(conn, "2", "Web3 Blockchain Engineer", "BadCorp",
             "solidity ethereum defi protocol", "dismissed", "company")
        _add(conn, "3", "Crypto Blockchain Engineer", "OtherCo",
             "solidity smart contracts", "dismissed", "wrong_domain")
        _add(conn, "4", "Machine Learning Engineer", "GoodCo",
             "pytorch nlp models production", "interested")
        _add(conn, "5", "Machine Learning Engineer", "NiceCo",
             "computer vision pytorch", "interested")

        out = learn.mine_rules(conn)
        values = {r["value"] for r in out["rules"]}
        assert out["status"] == "ok"
        assert "blockchain" in values and "solidity" in values     # discriminative negatives
        assert "pytorch" not in values                             # appears in positives -> not mined
        assert "company_block" in {r["kind"] for r in out["rules"]}
        assert "badcorp" in values                                 # dismissed 2x for 'company'

        # Persisted but INACTIVE — nothing filters until approved.
        assert db.list_rules(conn, active=0)
        assert db.active_rules(conn) == []

        # Re-mining is idempotent (no duplicate rows via UNIQUE(kind,value)).
        again = learn.mine_rules(conn)
        assert again["new"] == 0


def test_condense_profile_persists(tmp_db, monkeypatch):
    monkeypatch.setattr(learn.provider, "available", lambda: True)
    captured = {}

    def fake_generate(prompt, system=None, **kw):
        captured["prompt"] = prompt
        return "- Prefer applied ML product roles\n- Avoid blockchain / web3"

    monkeypatch.setattr(learn.provider, "generate", fake_generate)
    with db.connect() as conn:
        _add(conn, "1", "ML Engineer", "GoodCo", "pytorch", "interested")
        _add(conn, "2", "Blockchain Engineer", "BadCorp", "solidity", "dismissed", "wrong_domain")
        _add(conn, "3", "Web3 Engineer", "BadCorp", "ethereum", "dismissed", "wrong_domain")
        out = learn.condense_profile(conn)
        assert out["status"] == "ok" and "blockchain" in out["text"].lower()
        row = db.current_profile(conn)
        assert row["text"] == out["text"] and row["n_pos"] == 1 and row["n_neg"] == 2
    # examples were actually fed to the model
    assert "Blockchain Engineer" in captured["prompt"] and "wrong_domain" in captured["prompt"]


def test_condense_profile_no_llm(tmp_db, monkeypatch):
    monkeypatch.setattr(learn.provider, "available", lambda: False)
    with db.connect() as conn:
        _add(conn, "1", "ML Engineer", "GoodCo", "pytorch", "interested")
        assert learn.condense_profile(conn)["status"] == "no_llm"


def test_condense_profile_insufficient(tmp_db, monkeypatch):
    monkeypatch.setattr(learn.provider, "available", lambda: True)
    monkeypatch.setattr(learn.provider, "generate", lambda *a, **k: "x")
    with db.connect() as conn:
        _add(conn, "1", "ML Engineer", "GoodCo", "pytorch", "interested")
        assert learn.condense_profile(conn)["status"] == "insufficient"


def test_approve_and_reject_flow(tmp_db):
    with db.connect() as conn:
        db.add_rule(conn, "negative_kw", "blockchain", source="learned", active=0)
        rid = db.list_rules(conn, active=0)[0]["id"]
        db.set_rule_active(conn, rid, 1)
        assert [r["value"] for r in db.active_rules(conn)] == ["blockchain"]
        db.delete_rule(conn, rid)
        assert db.list_rules(conn) == []
