import pytest

from jobhunter import db as db_mod
from jobhunter import jd_store as jd_store_mod
from jobhunter.tailor import engine as cv_engine_mod
from jobhunter.tailor import summary as summary_mod


@pytest.fixture(autouse=True)
def no_real_summary_llm(monkeypatch):
    """The summary sentence is one extra LLM call per tailoring; tests that make the
    backend look available must never reach a real one. Tests of the summary itself
    override this."""
    monkeypatch.setattr(summary_mod, "generate_summary", lambda *a, **k: None)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point the DB helpers at a throwaway database."""
    p = tmp_path / "test.db"
    monkeypatch.setattr(db_mod, "DB_PATH", p)
    monkeypatch.setattr(db_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(jd_store_mod, "JD_DIR", tmp_path / "jd")
    monkeypatch.setattr(cv_engine_mod, "CV_OUT_DIR", tmp_path / "cv")
    db_mod.init_db()
    return p


@pytest.fixture
def config():
    from jobhunter.config import load_search_config
    return load_search_config()


@pytest.fixture
def two_page_layout(monkeypatch):
    """Every compile measures as a full two-page CV, so the fit step neither trims nor adds back."""
    layout = cv_engine_mod.fit.Layout(2, 792.0, 12.0, [744.0, 744.0], [55, 55], ["Name", "PROJECTS"])
    monkeypatch.setattr(cv_engine_mod, "_page_layout", lambda pdf: layout)
