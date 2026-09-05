from jobhunter import schedule


def test_cron_line_shape():
    line = schedule.cron_line(8, 30)
    assert line.startswith("30 8 * * *")
    assert "jobhunter.cli run" in line
    assert schedule.MARKER in line


def test_cron_line_interval_hours_overrides_hour_minute():
    line = schedule.cron_line(8, 30, interval_hours=12)
    assert line.startswith("30 */12 * * *")   # minute kept, hour becomes an interval pattern
    assert "jobhunter.cli run" in line
    assert schedule.MARKER in line


def test_with_entry_adds_and_is_idempotent():
    existing = "0 0 * * * /other/job\n"
    once = schedule.with_entry(existing, schedule.cron_line(8, 0))
    twice = schedule.with_entry(once, schedule.cron_line(9, 0))
    # user's other job preserved
    assert "/other/job" in twice
    # only one jobhunter line ever present
    assert sum(schedule.MARKER in l for l in twice.splitlines()) == 1
    # replacement took the new time
    assert any(l.startswith("0 9 * * *") for l in twice.splitlines())


def test_without_marker_removes_only_ours():
    existing = "0 0 * * * /other/job\n" + schedule.cron_line(8, 0) + "\n"
    cleaned = schedule.without_marker(existing)
    assert "/other/job" in cleaned
    assert schedule.MARKER not in cleaned


def test_process_cron_line_shape():
    line = schedule.process_cron_line(2)
    assert line.startswith("0 */2 * * *")
    assert "jobhunter.cli process" in line
    assert schedule.PROCESS_MARKER in line


def test_python_resolution_prefers_override_then_conda(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBHUNTER_PYTHON", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    # explicit override wins over everything
    monkeypatch.setenv("JOBHUNTER_PYTHON", "/opt/custom/bin/python")
    assert schedule._python() == "/opt/custom/bin/python"
    # an active conda env's python is preferred over the repo .venv
    conda = tmp_path / "conda"
    (conda / "bin").mkdir(parents=True)
    (conda / "bin" / "python").touch()
    monkeypatch.setenv("JOBHUNTER_PYTHON", "")
    monkeypatch.setenv("CONDA_PREFIX", str(conda))
    assert schedule._python() == str(conda / "bin" / "python")
