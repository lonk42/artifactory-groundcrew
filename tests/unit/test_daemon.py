from __future__ import annotations

import time

from artifactory_groundcrew.__main__ import _due
from artifactory_groundcrew.config import Settings
from artifactory_groundcrew.state import State


TARGETS = [{"python": "3.12", "platforms": ["manylinux_2_28_x86_64"]}]


def mirror(requirements=("six==1.16.0",), **kw):
    m = {"name": "pkgs", "type": "pypi", "remote": "r", "local": "l",
         "pypi": {"requirements": list(requirements), "targets": TARGETS}}
    return Settings(mirrors=[{**m, **kw}]).mirrors[0]


def state(tmp_path, **prev) -> State:
    s = State(tmp_path)
    s.save_mirror("pkgs", {"last_run": time.time(), **prev})
    return s


def test_a_mirror_that_just_ran_waits_for_its_interval(tmp_path):
    m = mirror()
    assert not _due(state(tmp_path, status="ok", fingerprint=m.fingerprint()), m, retry=600)


def test_a_changed_configuration_is_due_at_once(tmp_path):
    old = mirror()
    new = mirror(["six==1.16.0", "click==8.1.7"])
    assert _due(state(tmp_path, status="ok", fingerprint=old.fingerprint()), new, retry=600)


def test_a_failing_mirror_with_a_changed_configuration_waits_for_retry(tmp_path):
    old = mirror()
    new = mirror(["click==8.1.7"])
    assert not _due(state(tmp_path, status="failed", fingerprint=old.fingerprint()), new, retry=600)


def test_an_interval_change_alone_does_not_force_a_cycle(tmp_path):
    m = mirror()
    assert not _due(state(tmp_path, status="ok", fingerprint=m.fingerprint()), mirror(interval_seconds=7200), retry=600)


def test_a_mirror_pause_overrides_the_global_one_and_both_lift(tmp_path):
    s = State(tmp_path)
    assert s.paused("pkgs") is None
    s.pause(None, "upgrade")
    assert s.paused("pkgs")["scope"] == "all"
    s.pause("pkgs", "bad upstream")
    assert s.paused("pkgs")["reason"] == "bad upstream"
    assert s.resume("pkgs") and s.paused("pkgs")["scope"] == "all"
    assert s.resume(None) and s.paused("pkgs") is None
    assert not s.resume(None)


def test_cli_pause_and_resume(tmp_path, capsys, monkeypatch):
    from artifactory_groundcrew.cli import app

    monkeypatch.setattr(app.log, "configure", lambda *a, **k: None)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"state_dir: {tmp_path / 'state'}\n"
        "mirrors:\n  - {name: g, type: generic, remote: r, local: l, generic: {paths: [a]}}\n"
    )
    run = lambda *cmd: app.main(["--config", str(cfg), *cmd])  # noqa: E731
    assert run("pause", "nope") == 2
    assert run("pause", "g", "--reason", "testing") == 0
    assert run("status") == 0
    assert "g, " in capsys.readouterr().out
    assert run("pause") == 0 and run("resume", "g") == 0
    assert "still held" in capsys.readouterr().err
    assert run("resume") == 0
    assert State(tmp_path / "state").paused("g") is None


def test_a_run_request_is_taken_once(tmp_path):
    s = State(tmp_path)
    assert s.take_request("pkgs") is None
    s.request("pkgs", full=True)
    assert s.requested("pkgs")["full"] is True
    assert s.take_request("pkgs")["full"] is True
    assert s.requested("pkgs") is None and s.take_request("pkgs") is None


def test_cli_run_queues_a_request(tmp_path, capsys, monkeypatch):
    from artifactory_groundcrew.cli import app

    monkeypatch.setattr(app.log, "configure", lambda *a, **k: None)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"state_dir: {tmp_path / 'state'}\n"
        "mirrors:\n  - {name: g, type: generic, remote: r, local: l, generic: {paths: [a]}}\n"
    )
    assert app.main(["--config", str(cfg), "run", "nope"]) == 2
    assert app.main(["--config", str(cfg), "run", "g", "--full"]) == 0
    assert State(tmp_path / "state").requested("g")["full"] is True


def test_the_loop_runs_a_requested_mirror_and_skips_a_paused_one(tmp_path, monkeypatch):
    from artifactory_groundcrew import __main__ as daemon

    settings = Settings(state_dir=tmp_path, mirrors=[
        {"name": "asked", "type": "generic", "remote": "r", "local": "l1", "generic": {"paths": ["a"]}},
        {"name": "held", "type": "generic", "remote": "r", "local": "l2", "generic": {"paths": ["a"]}},
    ])
    s = State(tmp_path)
    s.pause(None)
    s.request("asked", full=True)
    ran = []

    class Client:
        def ping(self):
            pass

    def cycle(client, settings, mirror, state, full=False):
        ran.append((mirror.name, full))

    monkeypatch.setattr(daemon.ArtifactoryClient, "from_settings", classmethod(lambda cls, _: Client()))
    monkeypatch.setattr(daemon, "run_cycle", cycle)
    monkeypatch.setattr(daemon, "_sleep", lambda _: daemon._stop())
    monkeypatch.setattr(daemon, "_stopping", False)
    daemon._loop(settings)
    assert ran == [("asked", True)]
    assert s.requested("asked") is None
