"""Docker-backed stack integration suite — the ``@pytest.mark.stack`` lane.

Why this file is safe to have in the tree even though CI has no broker
====================================================================
Every broker-touching test here carries ``@pytest.mark.stack``. The default
pytest selection deselects that marker (``addopts = "-ra -m 'not perf and not
stack'"`` in ``pyproject.toml``), so Sonar coverage — which runs the *default*
selection — never depends on docker or a live broker. The one unmarked test in
this file, :func:`test_default_selection_excludes_every_stack_test`, proves that
isolation mechanically rather than by assertion-of-faith, and needs no docker.

A second gate on top of the marker
-----------------------------------
Even when a developer runs ``pytest -m stack`` on a machine with no docker (or no
mosquitto image), these tests must SKIP, not ERROR. So beyond the marker they
also require the opt-in env var ``EVENTS_STACK_IT=1`` and a usable docker + image
— mirroring the ``EVENTS_TEST_BROKER`` skip convention in ``tests/test_client.py``.
Without the opt-in the whole suite skips cleanly.

The throwaway broker
--------------------
Each test that needs a broker spins up its OWN, via :func:`broker_factory`:

* image: the stack's own pin (:data:`~events_cli.stack.MOSQUITTO_IMAGE`) if
  present, else ``eclipse-mosquitto:2`` (override with ``EVENTS_TEST_IMAGE``).
  This suite is what caught **deviation d2**: the pin was originally the
  suffix-free ``eclipse-mosquitto:2.1.2``, which upstream has never published —
  a pull returns ``no such manifest``, so ``events up`` would have failed on any
  clean host while passing here off a locally-cached image. The 2.1 line ships
  only as ``2.1.x-alpine``. :func:`test_the_pinned_image_tag_is_actually_pullable`
  below is the standing guard against that class of bug.
* a **uniquely named** container (``events-cli-it-<pid>-<rand>``) and named
  volume (``events-cli-it-data-<pid>-<rand>``) — never ``events-mosquitto`` /
  ``events-cli`` (the real stack's names) and never any ``nova-*`` name.
* an **ephemeral loopback port** (``127.0.0.1:<free>:1883``) — never host 1883,
  which on this box carries the live ``nova-mosquitto`` robot broker.
* force teardown (``docker rm -f`` + ``docker volume rm``) in the fixture
  finalizer, so a failed run leaves no orphan container or volume.

Client tooling runs INSIDE the container
-----------------------------------------
The host has no ``mosquitto_pub`` / ``mosquitto_sub``, so the pub/sub round-trip
and the retained-state reads/writes go through ``docker exec <broker>
mosquitto_pub`` / ``mosquitto_sub`` — the client binaries ship in the image.
Broker *readiness* is probed with the importable :class:`EventClient` (paho),
which keeps the readiness path off ``docker exec`` entirely.

The unclean-kill measurement (h17 / park v2)
--------------------------------------------
:func:`test_retained_state_bound_after_unclean_kill` *measures* mosquitto's
retained-state durability after a SIGKILL; it does not assert a comfortable
belief. See that test's docstring for the exact timing window and what a
contradiction of the assumption would look like.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from events_cli.client import EventClient
from events_cli.stack import MOSQUITTO_IMAGE
from events_cli.stack._docker import docker_available

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Opt-in required to actually spin up docker brokers (the second gate above the
#: ``stack`` marker). Absent → every broker-backed test SKIPS, never ERRORS.
_OPT_IN_ENV = "EVENTS_STACK_IT"
#: Override the broker image (defaults to the pinned tag, then the floating one).
_IMAGE_ENV = "EVENTS_TEST_IMAGE"
_FALLBACK_IMAGE = "eclipse-mosquitto:2"

#: Deliberately far larger than any test's lifetime, so NO periodic autosave can
#: fire during a test. That makes the unclean-kill window unambiguous: nothing
#: was flushed on a timer, so only a clean-shutdown flush could persist state.
_LONG_AUTOSAVE = 3600

#: How long a broker gets to become reachable. Sized for a *contended* host, not
#: a quiet one: on the development box (load average ~45, a dozen GPU service
#: containers) a fresh mosquitto container has taken minutes to go from
#: `docker run` to an accepted MQTT CONNECT, even though the broker logs
#: "running" almost immediately. A tight bound here fails a perfectly healthy
#: broker, so this is deliberately long — these tests are marker-gated and
#: opt-in, so a slow path costs nobody the default gate.
_READY_TIMEOUT = 300
#: How long a broker gets to stop serving after a stop/kill (teardown of the
#: port mapping is much faster than setup, but still not instant under load).
_DOWN_TIMEOUT = 180

_CONF_TEMPLATE = """\
listener 1883
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
persistence_file mosquitto.db
autosave_interval {autosave}
log_dest stdout
"""


# --- docker seam (tests only; bandit excludes tests/) ----------------------


def _docker(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """Run a ``docker`` argv with captured output and a hard subprocess timeout.

    ``stdin`` is closed so no invocation can block waiting to attach one. The
    Python timeout SIGKILLs the client on expiry (reliable, unlike a shell
    ``timeout`` SIGTERM that a stuck ``docker exec`` client can ignore).
    """
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _image_present(image: str) -> bool:
    try:
        return _docker("image", "inspect", image, timeout=20).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _resolve_image() -> str | None:
    """Pick a usable local mosquitto image, or ``None`` if none is present."""
    override = os.environ.get(_IMAGE_ENV)
    candidates = [override] if override else [MOSQUITTO_IMAGE, _FALLBACK_IMAGE]
    for candidate in candidates:
        if candidate and _image_present(candidate):
            return candidate
    return None


def _require_stack_env() -> str:
    """Skip unless opted in AND docker + a mosquitto image are available."""
    if not os.environ.get(_OPT_IN_ENV):
        pytest.skip(f"set {_OPT_IN_ENV}=1 to run the docker-backed stack integration suite")
    if not docker_available():
        pytest.skip("docker is not on PATH")
    image = _resolve_image()
    if image is None:
        pytest.skip(
            f"no mosquitto image present (looked for {MOSQUITTO_IMAGE!r} then {_FALLBACK_IMAGE!r}; "
            f"override with {_IMAGE_ENV})"
        )
    return image


def _free_port() -> int:
    """A currently-free TCP port on loopback."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --- lifecycle: never block on the docker CLIENT, never poll `docker inspect` -
#
# Two behaviours observed on a heavily loaded host (docker 29.1.3, dozens of
# other containers):
#
#   1. `docker run -d` writes the new container id to stdout, the daemon starts
#      the container correctly — and then the CLIENT process never exits.
#      Waiting on its return code times out against a perfectly healthy broker.
#   2. Under the same contention `docker inspect` itself can exceed any sane
#      per-call timeout, so polling daemon state is no more reliable than
#      polling the client.
#
# The fix is to stop asking docker anything about liveness. The broker announces
# its own liveness on the mapped loopback port, so lifecycle transitions are
# observed on the PORT: "up" is a successful MQTT CONNECT via the importable
# client, "down" is a refused TCP connect. Both are cheap, local, and completely
# independent of docker CLI latency. The docker command itself is still fired —
# we simply never wait on the client, and reap it afterwards. Killing a
# `docker run -d` client is safe: the daemon, not the client, owns a detached
# container.


def _fire(*args: str) -> subprocess.Popen:
    """Launch a docker command without waiting for the client to exit."""
    return subprocess.Popen(
        ["docker", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _reap(proc: subprocess.Popen) -> str:
    """Kill and collect a docker client that may never exit on its own.

    Safe to call more than once — a second call on an already-drained process
    returns ``''`` instead of raising.
    """
    if proc.poll() is None:
        proc.kill()
    try:
        out, err = proc.communicate(timeout=10)
    except (subprocess.TimeoutExpired, ValueError):  # pragma: no cover - defensive
        return ""
    return (err or out or "").strip()


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when something accepts a TCP connection on ``host:port``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --- the throwaway broker --------------------------------------------------


@dataclass
class ThrowawayBroker:
    """A uniquely-named, ephemeral-port mosquitto container owned by one test."""

    container: str
    volume: str
    host: str
    port: int
    conf_dir: Path

    def exec(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
        """Run a command inside the broker container.

        The timeout is generous because it must absorb *docker CLI* latency on a
        contended host, not just the in-container work. Anything needing a
        semantic time bound (e.g. how long to wait for a message) passes that
        bound to the mosquitto client itself via ``-W``.
        """
        return _docker("exec", self.container, *args, timeout=timeout)

    def stop_clean(self, timeout: float = _DOWN_TIMEOUT) -> None:
        """SIGTERM + grace, waited out: mosquitto flushes persistence on clean shutdown.

        An explicit stop (rather than ``docker restart``) makes the clean
        shutdown observable — we wait for the port to close, which is after the
        broker has run its shutdown path and written the persistence DB.
        """
        proc = _fire("stop", "-t", "10", self.container)
        try:
            self.await_down(timeout)
        finally:
            _reap(proc)

    def kill_unclean(self, timeout: float = _DOWN_TIMEOUT) -> None:
        """SIGKILL: no shutdown handler runs, so nothing is flushed to disk."""
        proc = _fire("kill", "-s", "KILL", self.container)
        try:
            self.await_down(timeout)
        finally:
            _reap(proc)

    def start(self, timeout: float = _READY_TIMEOUT) -> None:
        proc = _fire("start", self.container)
        try:
            _wait_connectable(self, timeout)
        finally:
            _reap(proc)

    def await_down(self, timeout: float = _DOWN_TIMEOUT) -> None:
        """Wait until the mapped port stops accepting connections."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _port_open(self.host, self.port):
                return
            time.sleep(0.3)
        raise AssertionError(
            f"broker {self.container} still accepting connections on {self.port} "
            f"after {timeout}s"
        )


def _safe_logs(broker: ThrowawayBroker) -> str:
    try:
        result = _docker("logs", "--tail", "40", broker.container, timeout=15)
        return (result.stdout + result.stderr).strip()
    except (subprocess.SubprocessError, OSError):
        return "(logs unavailable)"


def _wait_connectable(broker: ThrowawayBroker, timeout: float = _READY_TIMEOUT) -> None:
    """Block until the broker accepts an MQTT CONNECT, via paho — no docker CLI involved.

    paho reconnects on its own schedule, so one client polled to its deadline is
    a complete readiness probe: it succeeds as soon as the broker is genuinely
    serving MQTT, whatever the docker daemon is doing.
    """
    deadline = time.time() + timeout
    client = EventClient(broker.host, broker.port)
    try:
        while time.time() < deadline:
            if client.is_connected:
                return
            time.sleep(0.1)
    finally:
        client.close()
    raise AssertionError(
        f"broker {broker.container} never became connectable on {broker.port}\n"
        f"--- last logs ---\n{_safe_logs(broker)}"
    )


def _start_broker(image: str, *, autosave_interval: int) -> ThrowawayBroker:
    token = f"{os.getpid()}-{secrets.token_hex(4)}"
    container = f"events-cli-it-{token}"
    volume = f"events-cli-it-data-{token}"
    conf_dir = Path(tempfile.mkdtemp(prefix="events-cli-it-"))
    conf = conf_dir / "mosquitto.conf"
    conf.write_text(_CONF_TEMPLATE.format(autosave=autosave_interval), encoding="utf-8")
    # The broker runs as uid 1883 inside the container and must read the mounted
    # config; the mkdtemp default (0700) would hide it from that uid.
    os.chmod(conf_dir, 0o755)
    os.chmod(conf, 0o644)

    port = _free_port()
    broker = ThrowawayBroker(container, volume, "127.0.0.1", port, conf_dir)
    proc = _fire(
        "run",
        "-d",
        "--name",
        container,
        "-p",
        f"127.0.0.1:{port}:1883",  # NEVER a bare 1883; NEVER 0.0.0.0
        "-v",
        f"{volume}:/mosquitto/data",
        "-v",
        f"{conf}:/mosquitto/config/mosquitto.conf:ro",
        image,
    )
    try:
        _wait_connectable(broker)
    except BaseException:
        _reap(proc)
        _teardown_broker(broker)
        raise
    _reap(proc)
    return broker


def _teardown_broker(broker: ThrowawayBroker) -> None:
    """Remove the container, volume, and temp config — best effort, never raises.

    Runs from the fixture finalizer on every exit path, so a failed test cannot
    leave an orphan container or named volume behind. Like the start path, it
    waits on observed daemon state rather than the docker client's exit.
    """
    # Teardown is the one place we DO wait for the docker client to finish.
    # Removal has no external signal to watch (the port may never have opened,
    # so "port closed" would fire instantly and killing the client mid-removal
    # is exactly how orphans get left behind). Give it a long bound and only
    # kill as a last resort. `-v` also drops any anonymous volume.
    rm = _fire("rm", "-f", "-v", broker.container)
    try:
        rm.communicate(timeout=240)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        _reap(rm)

    # The named volume can only go once the container has. Under heavy load the
    # daemon can still be finishing the container removal here, which makes the
    # first `volume rm` fail with "volume is in use" — so back off and retry
    # rather than leak the volume. (Observed once at load average ~76 with a
    # 3-attempt/2-second loop; the schedule below is deliberately more patient.)
    for attempt in range(5):
        vol = _fire("volume", "rm", "-f", broker.volume)
        try:
            vol.communicate(timeout=120)
            if vol.returncode == 0:
                break
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            _reap(vol)
        time.sleep(2 * (attempt + 1))

    shutil.rmtree(broker.conf_dir, ignore_errors=True)


@pytest.fixture
def broker_factory():
    """Yield a factory that starts throwaway brokers and tears them all down after."""
    image = _require_stack_env()
    created: list[ThrowawayBroker] = []

    def _make(*, autosave_interval: int = _LONG_AUTOSAVE) -> ThrowawayBroker:
        broker = _start_broker(image, autosave_interval=autosave_interval)
        created.append(broker)
        return broker

    try:
        yield _make
    finally:
        for broker in created:
            _teardown_broker(broker)


# --- mosquitto client tooling, run INSIDE the container --------------------


def _publish_retained(broker: ThrowawayBroker, topic: str, payload: str) -> None:
    result = broker.exec(
        "mosquitto_pub", "-h", "127.0.0.1", "-t", topic, "-m", payload, "-q", "1", "-r"
    )
    assert result.returncode == 0, f"mosquitto_pub failed: {result.stderr.strip()}"


def _read_retained(broker: ThrowawayBroker, topic: str, *, wait: int = 3) -> str:
    """Return the retained payload on ``topic``, or ``''`` if none arrives in ``wait`` s.

    ``-W <wait>`` is the real bound: it tells mosquitto_sub itself to give up
    after that many seconds, so "no retained message" costs ``wait`` seconds and
    not a hang. The much larger subprocess timeout is only a backstop absorbing
    docker CLI latency — it must never be the thing that decides "absent", or a
    slow daemon would masquerade as a missing message.
    """
    try:
        result = broker.exec(
            "mosquitto_sub",
            "-h",
            "127.0.0.1",
            "-t",
            topic,
            "-C",
            "1",
            "-W",
            str(wait),
            timeout=wait + 120,
        )
    except subprocess.TimeoutExpired:
        return ""
    return result.stdout.strip()


# --- the default-selection isolation proof (UNMARKED; needs no docker) -----


def _collect(extra_args: list[str]) -> tuple[subprocess.CompletedProcess, set[str]]:
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *extra_args,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    node_ids = {line.strip() for line in proc.stdout.splitlines() if "::" in line}
    return proc, node_ids


def test_default_selection_excludes_every_stack_test() -> None:
    """The default pytest selection collects ZERO stack-marked tests — proven mechanically.

    Rather than trust the marker, this collects two node-id sets in subprocesses:
    the real default selection (``addopts`` applied) and the full stack set
    (``addopts`` cleared, ``-m stack``). The stack set must be non-empty and
    fully DISJOINT from the default set. That disjointness is exactly what keeps
    Sonar coverage — computed from the default selection — independent of any
    broker or docker daemon.
    """
    text = (Path(_REPO_ROOT) / "pyproject.toml").read_text(encoding="utf-8")
    assert "not perf and not stack" in text, "the default addopts stack filter is missing"

    default_proc, default_ids = _collect([])
    stack_proc, stack_ids = _collect(["-o", "addopts=", "-m", "stack"])
    assert default_proc.returncode == 0, default_proc.stderr
    assert stack_proc.returncode == 0, stack_proc.stderr

    assert stack_ids, "expected at least one stack-marked test to exist"
    assert any(
        "test_stack_integration.py" in nid for nid in stack_ids
    ), "this file's integration tests should be in the stack set"
    leaked = stack_ids & default_ids
    assert not leaked, f"stack tests leaked into the default selection: {sorted(leaked)}"


# --- the pin is a name a registry will actually serve ----------------------


@pytest.mark.stack
def test_the_pinned_image_tag_is_actually_pullable() -> None:
    """``MOSQUITTO_IMAGE`` must name a tag the registry publishes.

    This is the guard for **deviation d2**. The pin was originally
    ``eclipse-mosquitto:2.1.2``, taken from the local image's
    ``org.opencontainers.image.version`` label — but that label reports the
    *software* version, and upstream publishes the 2.1 line only as
    ``2.1.x-alpine``. No such tag was ever pushed, so ``events up`` would have
    died with ``no such manifest`` on the first clean host to try it, while
    every dockerless unit test and every run on this box passed: the local
    cache satisfied the reference so nothing ever pulled.

    ``docker manifest inspect`` asks the registry directly and does **not**
    consult the local image store, which is exactly the check that was missing.
    Network-dependent, hence the ``stack`` marker and the opt-in gate.
    """
    if not os.environ.get(_OPT_IN_ENV):
        pytest.skip(f"set {_OPT_IN_ENV}=1 to run the docker-backed stack integration suite")
    if not docker_available():
        pytest.skip("docker is not on PATH")

    probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["docker", "manifest", "inspect", MOSQUITTO_IMAGE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if probe.returncode != 0 and "no such manifest" not in (probe.stderr or "").lower():
        pytest.skip(f"registry unreachable, cannot verify the pin: {probe.stderr.strip()[:200]}")
    assert probe.returncode == 0, (
        f"the pinned image {MOSQUITTO_IMAGE!r} is not published — `events up` would fail to "
        f"pull on a clean host. docker said: {probe.stderr.strip()[:200]}"
    )


# --- retained state across a clean restart ---------------------------------


@pytest.mark.stack
def test_retained_state_survives_clean_docker_restart(broker_factory) -> None:
    """A retained message survives a clean restart (SIGTERM flushes on shutdown).

    The restart is an explicit ``docker stop`` (SIGTERM + grace) followed by
    ``docker start`` rather than ``docker restart``. The signal semantics are
    identical — ``docker restart`` is stop-then-start — but splitting them makes
    the clean shutdown *observable*: the test waits for the container to reach
    not-running, which is precisely the point at which the persistence flush has
    completed, instead of racing an opaque single command.

    autosave_interval is 3600 s and nowhere near firing, so survival here is
    attributable to the clean-shutdown flush alone, not a coincidental autosave.
    """
    broker = broker_factory(autosave_interval=_LONG_AUTOSAVE)
    topic = "events/it/retained/clean"
    payload = f"clean-{secrets.token_hex(4)}"

    _publish_retained(broker, topic, payload)
    assert _read_retained(broker, topic) == payload  # present before the restart

    broker.stop_clean()  # SIGTERM, waited out — the flush happens here
    broker.start()  # returns only once MQTT is being served again

    assert _read_retained(broker, topic) == payload, (
        "retained state was LOST across a CLEAN docker restart — persistence or "
        "the mounted data volume is broken"
    )


# --- retained-state bound after an unclean kill (h17 measurement) ----------


@pytest.mark.stack
def test_retained_state_bound_after_unclean_kill(broker_factory) -> None:
    """MEASURE retained-state durability after an UNCLEAN SIGKILL (frame park v2 / h17).

    Window under test, made explicit:
      1. publish a retained message (QoS 1, so the broker has stored it),
      2. confirm it is present in memory,
      3. ``docker kill -s KILL`` within ~1 s — with ``autosave_interval = 3600 s``
         so NO periodic autosave can have fired,
      4. ``docker start`` the same container on the same volume,
      5. read the topic back.

    The autosave-bound assumption (h17) predicts the retained value is LOST:
    nothing was flushed to the persistence DB on a timer, and SIGKILL runs no
    clean-shutdown flush. This test therefore asserts the value did NOT survive.

    MEASURED at authoring time (mosquitto 2.1.2, this exact window): the retained
    value was LOST — the assumption HELD. If this assertion ever fails because
    the value SURVIVED, the assumption is CONTRADICTED and must be reported (a
    ``/deviate`` decision), NOT silenced by editing the test or the spec's
    mosquitto.conf claim.
    """
    broker = broker_factory(autosave_interval=_LONG_AUTOSAVE)
    topic = "events/it/retained/unclean"
    payload = f"unclean-{secrets.token_hex(4)}"

    _publish_retained(broker, topic, payload)
    assert _read_retained(broker, topic) == payload  # in memory, pre-kill

    broker.kill_unclean()
    broker.start()  # returns only once MQTT is being served again

    survived = _read_retained(broker, topic)
    assert survived == "", (
        "retained state SURVIVED an unclean SIGKILL within the autosave window "
        f"(read back {survived!r}). This CONTRADICTS the autosave-bound assumption "
        "(h17); report it rather than editing this test or the mosquitto.conf claim."
    )


# --- two default-constructed clients, concurrently -------------------------


@pytest.mark.stack
def test_two_default_clients_connect_concurrently(broker_factory) -> None:
    """Two default-constructed clients stay connected at once (unique ids ⇒ no self-kick).

    A broker disconnects an existing session when a second client presents the
    same MQTT client id; identical default ids would make the two clients fight.
    This is the concurrency case from ``tests/test_client.py``, run here against
    a broker this suite spins up rather than an externally supplied one.
    """
    broker = broker_factory()
    a = EventClient(broker.host, broker.port)
    b = EventClient(broker.host, broker.port)
    try:
        assert a.client_id != b.client_id

        deadline = time.time() + 10
        while time.time() < deadline and not (a.is_connected and b.is_connected):
            time.sleep(0.05)
        assert a.is_connected, "client A never connected"
        assert b.is_connected, "client B never connected"

        time.sleep(0.5)  # neither kicks the other a moment later
        assert a.is_connected and b.is_connected, "a client was kicked (id collision?)"
    finally:
        a.close()
        b.close()


# --- pub/sub round-trip, both ends via docker exec -------------------------


@pytest.mark.stack
def test_pubsub_roundtrip_via_docker_exec(broker_factory) -> None:
    """A live (non-retained) pub → broker → sub round-trip, both ends via ``docker exec``.

    The host has no mosquitto client tools, so both the subscriber and the
    publisher run inside the broker container. The subscriber starts first; the
    publisher then fires repeatedly (each a fresh, harmless connection) until the
    subscriber captures one message, removing the subscribe/publish ordering race.
    """
    broker = broker_factory()
    topic = "events/it/roundtrip"
    payload = f"ping-{secrets.token_hex(4)}"

    sub = subprocess.Popen(
        [
            "docker",
            "exec",
            broker.container,
            "mosquitto_sub",
            "-h",
            "127.0.0.1",
            "-t",
            topic,
            "-C",
            "1",
            "-W",
            "15",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline and sub.poll() is None:
            broker.exec("mosquitto_pub", "-h", "127.0.0.1", "-t", topic, "-m", payload, timeout=10)
            time.sleep(0.3)
        out, err = sub.communicate(timeout=10)
    finally:
        if sub.poll() is None:
            sub.kill()
            sub.communicate()
    assert payload in out, f"round-trip failed: stdout={out!r} stderr={err!r}"
