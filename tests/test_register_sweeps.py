"""Wiring test: idempotent sweep registration via a CronPort (framework-agnostic)."""

import json

import pytest

from agentic_system import ports
from agentic_system.sweeps import SWEEP_SCHEDULES, register_sweeps, main as sweeps_main


class _FakeCronPort:
    def __init__(self, scripts_dir):
        self._scripts_dir = scripts_dir
        self.names = []
        self.created = []

    def scripts_dir(self):
        return str(self._scripts_dir)

    def list_job_names(self):
        return list(self.names)

    def create_job(self, *, name, schedule, script, workdir):
        self.created.append(name)
        self.names.append(name)


@pytest.fixture()
def fake_cron(tmp_path):
    scripts = tmp_path / "scripts"
    cron = _FakeCronPort(scripts)
    ports.set_cron_port(cron)
    yield cron, scripts


def test_dry_run_reports_scripts_and_creates_nothing(fake_cron):
    cron, scripts = fake_cron
    out = register_sweeps(dry_run=True)
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["created"] == []
    assert out["skipped"] == []
    assert sorted(out["scripts"]) == sorted(
        [str(scripts / f"sweep-{n}.py") for n in SWEEP_SCHEDULES])
    assert not scripts.exists()
    assert cron.created == []


def test_registration_creates_all_sweeps_and_writes_wrappers(fake_cron):
    cron, scripts = fake_cron
    out = register_sweeps()
    assert out["ok"] is True
    assert sorted(out["created"]) == sorted(f"sweep-{n}" for n in SWEEP_SCHEDULES)
    assert out["skipped"] == []
    for name in SWEEP_SCHEDULES:
        p = scripts / f"sweep-{name}.py"
        assert p.exists()
        text = p.read_text(encoding="utf-8")
        assert "from agentic_system.sweeps import main" in text
        assert f"main([{name!r}])" in text
        assert "sys.path.insert" in text
    assert sorted(cron.created) == sorted(f"sweep-{n}" for n in SWEEP_SCHEDULES)


def test_registration_is_idempotent(fake_cron):
    cron, scripts = fake_cron
    register_sweeps()
    assert len(cron.created) == len(SWEEP_SCHEDULES)
    out2 = register_sweeps()
    assert out2["created"] == []
    assert sorted(out2["skipped"]) == sorted(f"sweep-{n}" for n in SWEEP_SCHEDULES)
    assert len(cron.created) == len(SWEEP_SCHEDULES)  # no new jobs


def test_cli_register_dispatch_works(fake_cron, capsys):
    rc = sweeps_main(["register"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert len(out["created"]) == len(SWEEP_SCHEDULES)


def test_usage_message_lists_register(capsys):
    rc = sweeps_main([])
    assert rc == 2
    assert "register" in capsys.readouterr().err