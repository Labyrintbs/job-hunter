import plistlib

from jobhunter import schedule


def test_calendar_intervals_fixed_time():
    assert schedule._calendar_intervals(8, 30, None) == [{"Hour": 8, "Minute": 30}]


def test_calendar_intervals_interval_hours_anchors_at_given_hour():
    # minute kept, fires every N hours starting from `hour` -- 2 + 12h -> 02:00 and 14:00
    assert schedule._calendar_intervals(2, 30, interval_hours=12) == [
        {"Hour": 2, "Minute": 30}, {"Hour": 14, "Minute": 30},
    ]


def test_calendar_intervals_hourly():
    assert schedule._calendar_intervals(0, 0, interval_hours=1) == [
        {"Hour": h, "Minute": 0} for h in range(24)
    ]


def test_cron_line_shape():
    line = schedule.cron_line(8, 30)
    assert schedule.MARKER in line
    assert "jobhunter.cli run" in line
    assert "08:30" in line


def test_process_cron_line_shape():
    line = schedule.process_cron_line(2)
    assert schedule.PROCESS_MARKER in line
    assert "jobhunter.cli process" in line


def test_install_writes_a_loadable_plist_and_current_reads_it_back(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "LAUNCH_AGENTS_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: calls.append(a) or _FakeProc())

    line = schedule.install(8, 0, interval_hours=12)

    plist_path = tmp_path / f"{schedule.MARKER}.plist"
    assert plist_path.exists()
    with open(plist_path, "rb") as f:
        data = plistlib.load(f)
    assert data["Label"] == schedule.MARKER
    assert data["ProgramArguments"][:2] == ["/bin/bash", "-c"]
    assert "jobhunter.cli run" in data["ProgramArguments"][2]
    assert data["StartCalendarInterval"] == [{"Hour": 8, "Minute": 0}, {"Hour": 20, "Minute": 0}]
    assert "launchctl" in calls[-1][0][0]   # loaded via launchctl

    assert schedule.current() == line   # round-trips through the written plist


def test_uninstall_removes_the_plist_file(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "LAUNCH_AGENTS_DIR", tmp_path)
    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: _FakeProc())

    assert schedule.uninstall() is False   # nothing installed yet
    schedule.install(8, 0)
    assert (tmp_path / f"{schedule.MARKER}.plist").exists()

    assert schedule.uninstall() is True
    assert not (tmp_path / f"{schedule.MARKER}.plist").exists()
    assert schedule.uninstall() is False   # already gone


def test_watchdog_and_process_use_distinct_labels_and_dont_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "LAUNCH_AGENTS_DIR", tmp_path)
    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: _FakeProc())

    schedule.install_watchdog(1)
    schedule.install_process(2)

    assert (tmp_path / f"{schedule.WATCHDOG_MARKER}.plist").exists()
    assert (tmp_path / f"{schedule.PROCESS_MARKER}.plist").exists()
    assert schedule.current_watchdog() is not None
    assert schedule.current_process() is not None
    assert schedule.uninstall_watchdog() is True
    assert schedule.current_watchdog() is None
    assert schedule.current_process() is not None   # untouched


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


class _FakeProc:
    returncode = 0
