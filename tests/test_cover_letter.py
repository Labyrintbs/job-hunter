from jobhunter.apply import cover_letter as CL
from jobhunter.models import Job


def _job():
    return Job(source="wttj", external_id="1", title="ML Engineer", company="Acme",
              description="we build production ML systems")


def test_draft_injects_judge_context(monkeypatch):
    captured = {}
    monkeypatch.setattr(CL.provider, "generate",
                        lambda prompt, **kw: captured.update(prompt=prompt) or "cover letter body")
    CL.draft(_job(), judge_context="Rated 'good' fit (70/100): solid domain overlap")
    assert "FIT-JUDGE'S OWN ASSESSMENT" in captured["prompt"]
    assert "Rated 'good' fit (70/100): solid domain overlap" in captured["prompt"]


def test_draft_grounds_the_letter_in_the_tailored_cv_when_given(monkeypatch):
    captured = {}
    monkeypatch.setattr(CL.provider, "generate",
                        lambda prompt, **kw: captured.update(prompt=prompt) or "cover letter body")
    CL.draft(_job(), cv_text="TAILORED_CV_ONLY_CONTENT")
    assert "TAILORED_CV_ONLY_CONTENT" in captured["prompt"]
    assert "exact CV sent with this application" in captured["prompt"]
    assert "NeRF" not in captured["prompt"]        # the full base CV is not also included


def test_draft_without_a_tailored_cv_uses_the_whole_base_cv_uncapped(monkeypatch):
    from jobhunter.llm.profile import profile_text
    captured = {}
    monkeypatch.setattr(CL.provider, "generate",
                        lambda prompt, **kw: captured.update(prompt=prompt) or "cover letter body")
    CL.draft(_job())
    assert profile_text() in captured["prompt"]    # incl. SKILLS at the end, no 6000-char cut


def test_draft_passes_up_to_16000_chars_of_the_description(monkeypatch):
    captured = {}
    monkeypatch.setattr(CL.provider, "generate",
                        lambda prompt, **kw: captured.update(prompt=prompt) or "cover letter body")
    # markers straddle the old 4000 cutoff and the new 16000 one
    long_desc = ("x" * 3990) + "PAST_OLD_CUTOFF" + ("x" * 11980) + "WITHIN_16000" + "xxx" + "PAST_CUTOFF"
    assert long_desc.index("PAST_CUTOFF") == 16000
    job = Job(source="wttj", external_id="1", title="ML Engineer", company="Acme", description=long_desc)

    CL.draft(job)

    assert "PAST_OLD_CUTOFF" in captured["prompt"] and "WITHIN_16000" in captured["prompt"]
    assert "PAST_CUTOFF" not in captured["prompt"]    # still bounded


def test_draft_without_judge_context_omits_the_block(monkeypatch):
    captured = {}
    monkeypatch.setattr(CL.provider, "generate",
                        lambda prompt, **kw: captured.update(prompt=prompt) or "cover letter body")
    CL.draft(_job())
    assert "FIT-JUDGE'S OWN ASSESSMENT" not in captured["prompt"]


FR_LETTER = ("Je vous écris au sujet du poste d'ingénieur Machine Learning dans votre équipe. Mon stage "
             "chez DiliTrust m'a permis de travailler sur l'extraction d'entités juridiques avec des "
             "prompts structurés, et je souhaite mettre cette expérience au service de vos projets. "
             "Je suis disponible immédiatement à Paris.") * 2
EN_LETTER = ("I am applying for the Machine Learning Engineer role in your team. My internship at DiliTrust "
             "let me work on legal entity extraction with structured prompts, and I would like to bring "
             "this experience to your projects. I am available immediately in Paris.") * 2


def test_the_prompt_asks_for_the_requested_language(monkeypatch):
    seen = {}
    monkeypatch.setattr(CL.provider, "generate",
                        lambda prompt, system=None, **kw: seen.update(system=system, prompt=prompt) or "x")
    CL.draft(_job(), language="fr")
    assert "written in French" in seen["system"] and "vous" in seen["system"]
    CL.draft(_job(), language="en")
    assert "written in English" in seen["system"]


def test_the_feedback_of_a_rejected_attempt_reaches_the_prompt(monkeypatch):
    seen = {}
    monkeypatch.setattr(CL.provider, "generate", lambda prompt, **kw: seen.update(prompt=prompt) or "x")
    CL.draft(_job(), language="fr", feedback="the letter reads as EN")
    assert "previous attempt was rejected: the letter reads as EN" in seen["prompt"]
    assert "Write the whole letter in French" in seen["prompt"]


def test_language_problem_only_counts_function_words():
    assert CL.language_problem(FR_LETTER, "fr") == ""
    assert CL.language_problem(EN_LETTER, "en") == ""
    assert "reads as EN" in CL.language_problem(EN_LETTER, "fr")
    assert "reads as FR" in CL.language_problem(FR_LETTER, "en")
    mixed = FR_LETTER + " The fine-tuning of LLM pipelines with LangGraph agents."   # a little English is fine
    assert CL.language_problem(mixed, "fr") == ""


def test_a_wrong_language_draft_is_retried_once_then_kept(monkeypatch, tmp_path):
    from jobhunter import fetch_diag
    answers = iter([EN_LETTER, FR_LETTER])
    calls = []
    monkeypatch.setattr(CL.provider, "generate", lambda prompt, **kw: calls.append(prompt) or next(answers))
    with fetch_diag.run_tracking() as tracker:
        path = CL.draft_to_file(_job(), tmp_path, language="fr")
    assert len(calls) == 2 and "previous attempt was rejected" in calls[1]
    assert "Je vous écris" in path.read_text(encoding="utf-8")
    assert dict(tracker.counts) == {}


def test_a_letter_still_in_the_wrong_language_is_tracked(monkeypatch, tmp_path):
    from jobhunter import fetch_diag
    monkeypatch.setattr(CL.provider, "generate", lambda prompt, **kw: EN_LETTER)
    with fetch_diag.run_tracking() as tracker:
        path = CL.draft_to_file(_job(), tmp_path, language="fr")
    assert path.exists()
    assert dict(tracker.counts) == {("cover", "Acme", "cover_language_mismatch"): 1}


def test_a_correct_first_draft_costs_one_call(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(CL.provider, "generate", lambda prompt, **kw: calls.append(1) or FR_LETTER)
    CL.draft_to_file(_job(), tmp_path, language="fr")
    assert len(calls) == 1


def test_the_letter_prompt_carries_the_job_location_and_the_relocation_closing(monkeypatch):
    seen = {}
    monkeypatch.setattr(CL.provider, "generate",
                        lambda prompt, system=None, **kw: seen.update(prompt=prompt, system=system) or "x")
    job = Job(source="wttj", external_id="2", title="ML Engineer", company="Acme",
              location="Toulouse, Occitanie, France", description="we build production ML systems")
    CL.draft(job)
    assert "Location: Toulouse, Occitanie, France" in seen["prompt"]
    assert "open to relocating" in seen["system"] and "Paris area" not in seen["system"]
