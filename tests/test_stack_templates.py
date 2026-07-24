"""Contract tests for the generated broker stack.

These read the shipped template files. They deliberately do NOT start a broker,
run docker, or touch the network: the security properties asserted here are
properties of a *file*, and a test that needed a live daemon to check them would
be skipped on exactly the machines where it matters.

The mosquitto facts the templates claim (2.1.2's defaults, whether an explicit
listener suppresses the 9883 dashboard, whether ``mosquitto_sub -E`` exits 0)
were verified against the pinned image by hand; live-broker integration lives in
a separate, marked suite.
"""

from __future__ import annotations

import re

import pytest

from events_cli.stack import (
    COMPOSE_FILENAME,
    MOSQUITTO_CONF_FILENAME,
    MOSQUITTO_IMAGE,
    MOSQUITTO_VERSION,
    PUBLISHED_MAPPING,
    StackError,
    template_text,
)


@pytest.fixture(scope="module")
def compose() -> str:
    return template_text(COMPOSE_FILENAME)


@pytest.fixture(scope="module")
def mosquitto_conf() -> str:
    return template_text(MOSQUITTO_CONF_FILENAME)


def _directives(conf: str) -> list[str]:
    """The config's actual directives, with comments and blank lines removed.

    Every assertion about what the broker is *configured* to do has to run
    against this, not the raw text — the file is mostly comments, and half of
    them mention the very settings we are checking are absent.
    """
    return [
        line.strip()
        for line in conf.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _yaml(compose: str) -> str:
    """compose.yaml with comment lines removed.

    Assertions about what Compose is *configured* to do must run against this.
    The comments explain the 0.0.0.0 anti-pattern and quote ``$SYS`` in prose,
    so a raw-text check would flag the explanation as the offence.
    """
    return "\n".join(line for line in compose.splitlines() if not line.strip().startswith("#"))


def _prose(text: str) -> str:
    """Comment text as one whitespace-collapsed line, for phrase assertions.

    A sentence that happens to wrap across two comment lines is still the same
    sentence; without this, every assertion about the documentation would be
    hostage to where the line breaks landed.
    """
    joined = " ".join(line.strip().lstrip("#").strip() for line in text.splitlines())
    return " ".join(joined.split()).lower()


def _compose_image(compose: str) -> str:
    match = re.search(r"^\s*image:\s*(\S+)\s*$", _yaml(compose), flags=re.MULTILINE)
    assert match, "compose.yaml declares no image"
    return match.group(1)


def _published_mappings(compose: str) -> list[str]:
    """The entries under the service's ``ports:`` key, and nothing else.

    Scoped to that block on purpose: a naive scan for quoted list items also
    picks up the healthcheck's argv (``"1883"``, ``"5"``), and asserting those
    are loopback-bound is meaningless.
    """
    mappings: list[str] = []
    inside = False
    indent = 0
    for line in _yaml(compose).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        current = len(line) - len(line.lstrip())
        if inside:
            if stripped.startswith("- ") and current > indent:
                mappings.append(stripped[2:].strip().strip("\"'"))
                continue
            if current <= indent:
                inside = False
        if stripped == "ports:":
            inside = True
            indent = current
    return mappings


# --- templates are reachable ----------------------------------------------


def test_templates_load_from_package_resources() -> None:
    """Both templates load via importlib.resources, so a wheel install works."""
    assert template_text(COMPOSE_FILENAME).strip()
    assert template_text(MOSQUITTO_CONF_FILENAME).strip()


def test_unknown_template_is_rejected() -> None:
    with pytest.raises(StackError):
        template_text("../../../etc/passwd")


# --- criterion 1: loopback-only publish ------------------------------------


def test_compose_publishes_the_literal_loopback_mapping(compose: str) -> None:
    assert PUBLISHED_MAPPING == "127.0.0.1:1883:1883"
    assert "127.0.0.1:1883:1883" in compose


def test_compose_never_publishes_a_bare_or_wildcard_mapping(compose: str) -> None:
    """Every published port must name a loopback host address.

    The failure being guarded is a bare ``1883:1883``. Docker publishes that on
    ``0.0.0.0``, and its DNAT runs before the host firewall, so ufw does not
    save you. Catching it here rather than on the wire is the whole point of
    shipping a template instead of documentation.
    """
    mappings = _published_mappings(compose)
    assert mappings, "no port mapping found in compose.yaml"
    for mapping in mappings:
        assert mapping.startswith("127.0.0.1:"), f"port mapping {mapping!r} is not loopback-bound"
    # The comments name 0.0.0.0 to explain the hazard; the configuration itself
    # must never mention it.
    assert "0.0.0.0" not in _yaml(compose)


def test_compose_publishes_exactly_one_port(compose: str) -> None:
    """One MQTT port and nothing else — no dashboard, no websocket, no metrics."""
    assert _published_mappings(compose) == ["127.0.0.1:1883:1883"]


# --- criterion 1: exact image tag ------------------------------------------


def test_compose_pins_an_exact_patch_tag(compose: str) -> None:
    """A floating major (``eclipse-mosquitto:2``) must fail this test.

    That tag currently resolves to 2.1.2, but it resolved to a 2.0.x for years
    and will move again. The generated mosquitto.conf documents defaults *for a
    specific version* — 2.1 opens an http_api dashboard that 2.0 has no concept
    of — so a moving tag turns those comments into confident lies.

    A variant suffix is allowed after the patch version (``2.1.2-alpine``)
    because upstream publishes the 2.1 line *only* in that form — pinning the
    suffix-free ``2.1.2`` names a tag that has never existed and fails to pull
    with ``no such manifest`` (deviation d2). What must not appear is a tag that
    can *move*, which is what the check below enforces.
    """
    image = _compose_image(compose)
    repository, _, tag = image.rpartition(":")
    assert repository, f"image {image!r} has no tag at all"
    assert re.fullmatch(
        r"\d+\.\d+\.\d+(?:-[a-z0-9.]+)?", tag
    ), f"image tag {tag!r} is not an exact major.minor.patch version"
    assert tag not in {"2", "2.1", "latest", "alpine", "openssl"}


def test_compose_image_matches_the_declared_constant(compose: str) -> None:
    """The template and the Python constant cannot drift apart silently."""
    assert _compose_image(compose) == MOSQUITTO_IMAGE
    # Not ``endswith(f":{MOSQUITTO_VERSION}")``: the tag legitimately carries a
    # variant suffix the software version does not. The version must be the tag's
    # version *component*, so the documented defaults still describe the image.
    _, _, tag = MOSQUITTO_IMAGE.rpartition(":")
    assert tag.split("-")[0] == MOSQUITTO_VERSION


# --- criterion 1: no websocket listener ------------------------------------


def test_compose_has_no_websocket_or_dashboard_port(compose: str) -> None:
    for mapping in _published_mappings(compose):
        assert ":9001:" not in f":{mapping}:", "9001 websocket port must not be published"
        assert ":9883:" not in f":{mapping}:", "9883 http_api port must not be published"


def test_mosquitto_conf_declares_no_websocket_or_http_api_listener(
    mosquitto_conf: str,
) -> None:
    """Checked against directives only — the comments discuss both at length."""
    for directive in _directives(mosquitto_conf):
        assert not directive.startswith("protocol websockets")
        assert not directive.startswith("protocol http_api")
        assert not directive.startswith("http_dir")
        assert directive != "listener 9001"
        assert directive != "listener 9883"


def test_mosquitto_conf_declares_exactly_one_listener(mosquitto_conf: str) -> None:
    listeners = [d for d in _directives(mosquitto_conf) if d.startswith("listener ")]
    assert listeners == ["listener 1883"]


# --- criterion 1: healthcheck ----------------------------------------------


def test_compose_healthcheck_uses_mosquitto_sub(compose: str) -> None:
    assert "healthcheck:" in compose
    assert "mosquitto_sub" in compose


def test_compose_healthcheck_escapes_the_sys_dollar(compose: str) -> None:
    """``$$SYS``, not ``$SYS``.

    Compose interpolates ``$VAR`` in the file. A single ``$SYS`` expands to the
    empty string, the probe subscribes to ``/broker/uptime``, nothing ever
    publishes there, and the healthcheck fails forever against a healthy broker.
    """
    assert "$$SYS/broker/uptime" in compose
    # Checked on the configuration, not the comments: interpolation applies to
    # parsed values, and the comment above the healthcheck necessarily spells
    # the broken single-dollar form to explain what not to write.
    assert not re.search(r"\$SYS", _yaml(compose).replace("$$SYS", ""))


def test_compose_healthcheck_is_bounded(compose: str) -> None:
    """Every probe has a finite timeout; nothing here can block indefinitely."""
    assert re.search(r"^\s*timeout:\s*\d+s\s*$", compose, flags=re.MULTILINE)
    assert re.search(r"^\s*retries:\s*\d+\s*$", compose, flags=re.MULTILINE)
    assert '"5"' in compose  # mosquitto_sub -W 5


# --- criterion 1: persistence ----------------------------------------------


def test_compose_declares_a_named_volume_and_mounts_it(compose: str) -> None:
    """``persistence true`` without this mount is a silent data-loss bug."""
    assert re.search(r"^volumes:\s*$", compose, flags=re.MULTILINE), "no top-level volumes: block"
    assert re.search(r"^\s{2}events-data:\s*$", compose, flags=re.MULTILINE)
    assert "- events-data:/mosquitto/data" in compose


def test_mosquitto_conf_enables_persistence_at_the_mounted_path(
    mosquitto_conf: str, compose: str
) -> None:
    directives = _directives(mosquitto_conf)
    assert "persistence true" in directives

    location = next(d for d in directives if d.startswith("persistence_location "))
    path = location.split(maxsplit=1)[1].rstrip("/")
    # The config's persistence_location and the compose mount point must be the
    # same directory, or the database is written outside the volume.
    assert f"- events-data:{path}" in compose


def test_mosquitto_conf_sets_an_explicit_autosave_interval(mosquitto_conf: str) -> None:
    """The upstream default is 1800s; an unclean kill would lose 30 minutes.

    Stating it explicitly makes the durability bound a documented number that a
    later task can measure against.
    """
    autosave = [d for d in _directives(mosquitto_conf) if d.startswith("autosave_interval ")]
    assert len(autosave) == 1
    seconds = int(autosave[0].split()[1])
    assert 0 < seconds <= 300


def test_mosquitto_conf_sets_an_explicit_backlog_bound(mosquitto_conf: str) -> None:
    """The undrained-backlog bound must never be left as an inherited default.

    Mirrors how ``test_compose_pins_an_exact_patch_tag`` guards the image tag
    against a floating default: a 2026-07-24 probe against this exact template
    showed the unconfigured default queues exactly 1000 QoS>0 messages for an
    offline persistent session and silently drops the newest arrivals once
    full, so the bound must be written down explicitly rather than inherited.
    """
    queued = [d for d in _directives(mosquitto_conf) if d.startswith("max_queued_messages ")]
    assert len(queued) == 1
    limit = int(queued[0].split()[1])
    assert limit > 0

    prose = _prose(mosquitto_conf)
    assert "2026-07-24" in prose
    assert "max_queued_messages" in prose
    assert "newest" in prose
    assert "dropped" in prose
    assert "events logs" in prose


# --- criterion 2: the comments document version-specific defaults ----------


def test_mosquitto_conf_names_the_exact_version_it_documents(
    mosquitto_conf: str,
) -> None:
    assert MOSQUITTO_VERSION in mosquitto_conf


def test_mosquitto_conf_labels_upstream_defaults_and_overrides(
    mosquitto_conf: str,
) -> None:
    """Criterion 2: say which security properties are upstream defaults.

    Both halves are required. Listing only the inherited defaults would hide
    that ``allow_anonymous true`` is a deliberate departure from them.
    """
    prose = _prose(mosquitto_conf)
    assert "upstream default" in prose
    assert "override" in prose


def test_mosquitto_conf_documents_the_allow_anonymous_default(
    mosquitto_conf: str,
) -> None:
    """The one security default this file departs from must be called out.

    Verified against the pinned image: with ``listener 1883`` and no
    ``allow_anonymous``, mosquitto 2.1.2 answers an anonymous subscribe with
    "Connection Refused: not authorised".
    """
    assert "allow_anonymous true" in _directives(mosquitto_conf)
    assert re.search(
        r"`?allow_anonymous`? defaults to false", _prose(mosquitto_conf)
    ), "the conf must state that allow_anonymous defaults to false upstream"


def test_mosquitto_conf_explains_why_the_listener_is_not_localhost_only(
    mosquitto_conf: str,
) -> None:
    """Naming the local-only default is what stops someone "fixing" the listener.

    Inside the container the broker must bind all interfaces or Docker's
    published port cannot reach it. A reader who knows only that "no listener
    means localhost only" would otherwise read ``listener 1883`` as a weakening.
    """
    assert "local only" in _prose(mosquitto_conf)
    assert "127.0.0.1:1883:1883" in mosquitto_conf


def test_mosquitto_conf_does_not_call_retained_messages_history(
    mosquitto_conf: str,
) -> None:
    """Retained messages are the last value on a topic, not a replayable log."""
    prose = _prose(mosquitto_conf)
    assert "not history" in prose
    assert "not a replayable log" in prose


def test_compose_is_valid_yaml_when_a_parser_is_available(compose: str) -> None:
    """Structural parse, when PyYAML happens to be installed.

    Optional on purpose: PyYAML is not a dependency of this zero-dependency
    package, and adding one to lint a template would be a poor trade. Every
    assertion above is a substring or regex check that holds without it.
    """
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(compose)
    service = doc["services"]["broker"]
    assert service["ports"] == ["127.0.0.1:1883:1883"]
    assert service["image"] == MOSQUITTO_IMAGE
    assert "events-data" in doc["volumes"]
    assert service["healthcheck"]["test"][1] == "mosquitto_sub"
    # After YAML parsing the doubled dollar is still doubled: it is Compose,
    # not YAML, that collapses it at interpolation time.
    assert "$$SYS/broker/uptime" in service["healthcheck"]["test"]
