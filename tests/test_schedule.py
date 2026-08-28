from jobhunter import schedule


def test_cron_line_shape():
    line = schedule.cron_line(8, 30)
    assert line.startswith("30 8 * * *")
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
