from jobhunter.notify import channels, digest, dispatch


def R(title="ML Engineer", company="Acme", score=50, llm_score=None, verdict="", url="http://x"):
    return {"title": title, "company": company, "location": "Paris", "score": score,
            "llm_score": llm_score, "llm_verdict": verdict, "llm_reasons": "", "url": url}


def test_effective_score_prefers_llm():
    assert digest.effective_score(R(score=40, llm_score=88)) == 88
    assert digest.effective_score(R(score=40, llm_score=None)) == 40


def test_select_filters_and_sorts():
    rows = [R(title="a", score=30), R(title="b", score=70), R(title="c", score=40, llm_score=90)]
    sel = digest.select(rows, min_score=60)
    assert [r["title"] for r in sel] == ["c", "b"]   # 90, 70; 'a' dropped


def test_markdown_contains_links_and_scores():
    md = digest.markdown([R(title="ML Eng", score=80, url="http://job")])
    assert "[ML Eng](http://job)" in md and "**80**" in md


def test_file_channel_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(channels, "DATA_DIR", tmp_path)
    ch = channels.FileChannel()
    assert ch.available() is True
    path = ch.send("subj", "text", "# md\n")
    assert (tmp_path / "notifications").exists()
    assert open(path).read() == "# md\n"


def test_telegram_availability_and_payload(monkeypatch):
    ch = channels.TelegramChannel()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert ch.available() is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    assert ch.available() is True

    captured = {}
    def fake_post(url, json, timeout):
        captured["url"] = url; captured["json"] = json
        class Resp:
            def raise_for_status(self): pass
        return Resp()
    monkeypatch.setattr(channels.httpx, "post", fake_post)
    assert ch.send("Subject", "body", "# md") == "sent"
    assert "bottok/sendMessage" in captured["url"]
    assert captured["json"]["chat_id"] == "123"
    assert "Subject" in captured["json"]["text"]


def test_email_availability(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert channels.EmailChannel().available() is False
    monkeypatch.setenv("SMTP_HOST", "smtp.x"); monkeypatch.setenv("EMAIL_TO", "me@x")
    assert channels.EmailChannel().available() is True


def test_dispatch_file_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(channels, "DATA_DIR", tmp_path)
    rows = [R(title="hit", score=80), R(title="miss", score=10)]
    out = dispatch.send(rows, {"notifications": {"enabled": True, "min_score": 60, "channels": ["file"]}})
    assert out["selected"] == 1
    assert out["results"]["file"].endswith(".md")


def test_dispatch_disabled():
    out = dispatch.send([R(score=90)], {"notifications": {"enabled": False}})
    assert out["results"] == {} and out.get("skipped") == "disabled"


def test_dispatch_unknown_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(channels, "DATA_DIR", tmp_path)
    out = dispatch.send([R(score=90)], {"notifications": {"min_score": 60, "channels": ["pigeon"]}})
    assert out["results"]["pigeon"] == "unknown channel"
