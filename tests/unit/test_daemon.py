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
