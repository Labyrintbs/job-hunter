import pytest

from jobhunter import db as db_mod


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point the DB helpers at a throwaway database."""
    p = tmp_path / "test.db"
    monkeypatch.setattr(db_mod, "DB_PATH", p)
    monkeypatch.setattr(db_mod, "DATA_DIR", tmp_path)
    db_mod.init_db()
    return p


@pytest.fixture
def config():
    from jobhunter.config import load_search_config
    return load_search_config()
