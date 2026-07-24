"""Session-wide safety guard: the production broker must survive the test run.

Why this file exists
====================
The docker-backed suites (``@pytest.mark.stack``) start, stop, restart, kill and
remove mosquitto containers. On the machine this was developed on — and on any
machine where someone has run ``events up`` — there is also a container named
**``events-mosquitto``** bound to ``127.0.0.1:1883``, which is the real stack's
broker and, in the development case, the **event broker for a live robot's
nervous system**. Nothing in the test tree may touch it.

The suites are written so they cannot: every throwaway broker gets a unique
``events-cli-it-<pid>-<rand>`` container name, its own named volume, and an
**ephemeral loopback port**, and nothing ever invokes ``events up`` / ``events
down`` or a ``docker compose`` command against the ``events-cli`` project. That
is the design. This file is the *evidence*: it records the production
container's identity and start time before the session and re-reads them after,
so "the tests never restarted it" is a measured fact at the end of every run
rather than a claim about how the tests were written.

Failure of this fixture is not a flaky test — it means an isolation rule was
broken and the finding is the point. It reports the before/after pair rather
than just failing, so a reviewer can see *what* changed.

Cost when it does not apply
---------------------------
Gated on the same ``EVENTS_STACK_IT`` opt-in as the stack suites. Without it no
test starts a container at all, so there is nothing to guard and the default
(dockerless, CI) selection pays nothing — not even a ``docker`` lookup.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from events_cli.stack import CONTAINER_NAME, PROJECT_NAME

# Re-exported so BOTH docker-backed suites see one broker lifecycle without
# either importing the other. The machinery stays in test_stack_integration.py,
# where it was written and proven; a conftest re-export is how pytest is meant to
# share a fixture, and it avoids the F811 that importing a fixture directly into
# a test module produces (the parameter name shadows the imported name).
from tests.test_stack_integration import broker_factory  # noqa: F401

#: The opt-in the stack suites require. Mirrored here rather than imported so
#: this guard has no import dependency on the suite it guards.
_OPT_IN_ENV = "EVENTS_STACK_IT"

#: What identifies the production broker, and what must not change about it.
#: ``Id`` catches "removed and recreated" (a new container answering to the same
#: name); ``StartedAt`` catches "restarted"; ``Status`` catches "stopped".
_INSPECT_FORMAT = "{{.Id}} {{.State.Status}} {{.State.StartedAt}}"


def inspect_production_broker() -> str | None:
    """The production broker's identity + start time, or ``None`` if it is absent.

    ``None`` is a legitimate answer — most machines running this suite have never
    run ``events up`` — and is compared like any other value: absent before and
    absent after is fine, absent after having been present is a failure.
    """
    try:
        probe = subprocess.run(
            ["docker", "inspect", "--format", _INSPECT_FORMAT, CONTAINER_NAME],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if probe.returncode != 0:
        return None
    return probe.stdout.strip() or None


@pytest.fixture(scope="session", autouse=True)
def production_broker_untouched() -> object:
    """Assert the real ``events-mosquitto`` broker is byte-identical after the session.

    Same container id, same state, same ``StartedAt``. A restart, a stop, or a
    remove-and-recreate all move at least one of those, so any of the forbidden
    operations reaching the production stack fails the session here even if the
    test that did it passed.
    """
    if not os.environ.get(_OPT_IN_ENV):
        yield
        return

    before = inspect_production_broker()
    yield
    after = inspect_production_broker()

    assert after == before, (
        f"the production broker {CONTAINER_NAME!r} (compose project {PROJECT_NAME!r}) "
        f"CHANGED during this test run — it must never be stopped, restarted, killed "
        f"or removed by any test.\n"
        f"  before: {before!r}\n"
        f"  after:  {after!r}\n"
        f"(format: '<container id> <state> <startedAt>'; None means the container did "
        f"not exist)"
    )
