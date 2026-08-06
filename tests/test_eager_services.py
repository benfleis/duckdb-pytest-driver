"""Self-tests for up-front service provisioning + env adoption (the service analog of
`credential(adopt="env")`). Offline: FAKE start/populate/to_env logging to files; a bare Python test
reads `os.environ` to prove the derived env was adopted (the stand-in for a bare `.test` subprocess,
which needs a built binary).

Redesign note: the eager/on_demand `provision` disposition is GONE. Collect-first provisions every
service a SELECTED test needs UP FRONT (from the plan) on the controller; workers adopt its env from
the store. `to_env`/`populate` stay (WHAT/HOW env + seed, not WHEN); there is no disposition to choose.
"""

import stat
import textwrap

from ducktest import Service, service, use_service


# --- use_service binding (pure) -----------------------------------------------------------


def test_use_service_binds_policy_without_mutating_base():
    # MIGRATED: the `provision` disposition is gone; use_service now binds only to_env/populate policy
    # (and depends_on) onto a shared descriptor without mutating it.
    base = service("azurite", start=lambda c: {"endpoint": "x"})
    assert base.to_env is None and base.populate is None
    bound = use_service(base, to_env=lambda b: {"E": b["endpoint"]}, populate=lambda b, c: None)
    assert isinstance(bound, Service)
    assert bound.to_env is not None and bound.populate is not None
    assert bound.key == "azurite" and bound.start is base.start  # generic bits carried over
    assert base.to_env is None and base.populate is None  # base untouched (shared descriptor)


# REMOVED (was test_service_rejects_bad_provision): the `provision` field no longer exists on the
# Service descriptor (the eager/on_demand disposition is deleted), so there is nothing to reject.


def test_narrating_flag_logic():
    # --steps narrates; --repl/--provision-keep narrate unless --no-steps; --steps wins over --no-steps.
    from ducktest.plugin import _narrating

    class Cfg:
        def __init__(self, **o):
            self._o = o

        def getoption(self, k, default=None):
            return self._o.get(k, default)

    assert _narrating(Cfg()) is False
    assert _narrating(Cfg(**{"--steps": True})) is True
    assert _narrating(Cfg(**{"--repl": True})) is True
    assert _narrating(Cfg(**{"--repl": True, "--no-steps": True})) is False
    assert _narrating(Cfg(**{"--steps": True, "--no-steps": True})) is True


def test_to_env_is_carried_by_the_descriptor():
    # MIGRATED (was test_to_env_allowed_for_any_disposition): to_env is a property of the descriptor,
    # adopted by whatever process provisions the service (the store shares the block). No disposition.
    base = service("x", start=lambda c: None)
    assert use_service(base, to_env=lambda b: {}).to_env is not None
    assert service("x", start=lambda c: None, to_env=lambda b: {}).to_env is not None


# --- eager path end-to-end (isolated pytest run) ------------------------------------------


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


_CONFTEST = """
    import os
    from ducktest import register_suite, service

    def _start(config):
        with open(os.environ["BOOT_LOG"], "a") as f: f.write("b\\n")
        return {"endpoint": "svc://up"}

    def _populate(block, config):
        with open(os.environ["POP_LOG"], "a") as f: f.write("p\\n")

    def _to_env(block):
        return {"SVC_ENDPOINT": block["endpoint"]}

    def _attach(overrides, config):
        return {"endpoint": overrides.get("endpoint", "svc://default")}

    def pytest_configure(config):
        register_suite(config, "s", path=".", default=True, services=[
            service("svc", start=_start, populate=_populate, to_env=_to_env, attach=_attach)])
"""


def test_up_front_boots_once_adopts_env_populates_once(pytester, monkeypatch):
    boot = pytester.path / "boot.log"
    pop = pytester.path / "pop.log"
    monkeypatch.setenv("BOOT_LOG", str(boot))
    monkeypatch.setenv("POP_LOG", str(pop))
    _write(pytester, "conftest.py", _CONFTEST)
    _write(
        pytester,
        "test_inner.py",
        """
        from ducktest.plugin import _item_env
        def test_env_adopted(request):
            # the up-front service's to_env resolves for an item belonging to its suite (SPEC 3.7)
            assert _item_env(request.config, request.node)["SVC_ENDPOINT"] == "svc://up"
        """,
    )
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert boot.read_text().count("b") == 1  # booted exactly once (store single-flight)
    assert pop.read_text().count("p") == 1  # populated exactly once (store single-flight, per suite)


def test_up_front_with_attach_adopts_env_without_booting(pytester, monkeypatch):
    boot = pytester.path / "boot.log"
    pop = pytester.path / "pop.log"
    monkeypatch.setenv("BOOT_LOG", str(boot))
    monkeypatch.setenv("POP_LOG", str(pop))
    _write(pytester, "conftest.py", _CONFTEST)
    _write(
        pytester,
        "test_inner.py",
        """
        from ducktest.plugin import _item_env
        def test_env_from_attach(request):
            # env from the attached endpoint
            assert _item_env(request.config, request.node)["SVC_ENDPOINT"] == "svc://external"
        """,
    )
    result = pytester.runpytest_subprocess(
        "-n", "0", "--existing-service", "svc=svc://external", "-p", "no:cacheprovider"
    )
    result.assert_outcomes(passed=1)
    assert not boot.exists()  # attach => never booted
    # populate runs on every attach (no store/single-flight for it, by design) -- collect-first triggers
    # it once, this test's own _item_env call triggers it again; idempotent by contract either way.
    assert pop.read_text().count("p") == 2


def test_populate_runs_exactly_once(pytester, monkeypatch):
    # MIGRATED (was test_on_demand_populates_once): populate has no disposition — a fixture-pulled
    # service still populates exactly once (single-flighted through the store).
    pop = pytester.path / "pop.log"
    monkeypatch.setenv("POP_LOG", str(pop))
    _write(
        pytester,
        "conftest.py",
        """
        import os
        import pytest
        from ducktest import register_suite, service, get_suites, provision_service

        def _start(config): return {"endpoint": "svc://up"}
        def _populate(block, config):
            with open(os.environ["POP_LOG"], "a") as f: f.write("p\\n")

        def pytest_configure(config):
            register_suite(config, "s", default=True, services=[
                service("svc", start=_start, populate=_populate, fixture="svc_fx")])

        @pytest.fixture(scope="session")
        def svc_fx(request):
            svc = next(s for t in get_suites(request.config) for s in t.services if s.key == "svc")
            return provision_service(request.config, svc)
        """,
    )
    _write(pytester, "test_inner.py", "def test_a(svc_fx):\n    assert svc_fx['endpoint'] == 'svc://up'\n")
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert pop.read_text().count("p") == 1  # single-flighted: populated exactly once


def test_to_env_adopted_on_fixture_pull(pytester):
    # a worker that provisions the service (fixture pull) can resolve its to_env, scoped to items
    # belonging to the declaring suite -- not a global os.environ mutation.
    _write(
        pytester,
        "conftest.py",
        """
        import pytest
        from ducktest import register_suite, service, get_suites, provision_service

        def pytest_configure(config):
            register_suite(config, "s", path=".", default=True, services=[
                service("svc", start=lambda c: {"endpoint": "svc://up"},
                        to_env=lambda b: {"SVC_ENV": b["endpoint"]}, fixture="svc_fx")])

        @pytest.fixture(scope="session")
        def svc_fx(request):
            svc = next(s for t in get_suites(request.config) for s in t.services if s.key == "svc")
            return provision_service(request.config, svc)
        """,
    )
    _write(
        pytester,
        "test_inner.py",
        """
        from ducktest.plugin import _item_env
        def test_env_adopted_worker_side(request, svc_fx):
            assert _item_env(request.config, request.node)["SVC_ENV"] == "svc://up"
        """,
    )
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


# REMOVED (was test_provision_never_pulled_without_attach_fails_loud): the `provision='never'`
# disposition is deleted. A service with no boot capability is simply expressed as an
# `--existing-service`-only attach target; there is no separate "never" stance to fail loud on.


def test_run_paired_surfaces_a_swallowed_service_failure(pytester):
    """Collect-first provisioning is best-effort (`provision_reachable`'s `_on_service` swallows a
    boot failure so it can be re-surfaced by a fixture pull). A paired driver with no service-consuming
    fixture — just `request`/`matrix_cell`, e.g. a bare profile selector — had no such pull point, so
    the failure went unsurfaced and the body just silently ran without the service's env. `run_paired`
    now (re-)provisions its item's suite services itself, so this must fail loud instead."""
    _write(
        pytester,
        "test/conftest.py",
        """
        from ducktest import register_suite, service

        def _start(config):
            raise RuntimeError("boom: service infra unavailable")

        def pytest_configure(config):
            register_suite(config, "s", path="test/s",
                services=[service("svc", start=_start, to_env=lambda b: {"SVC_ENV": "x"})])
        """,
    )
    _write(
        pytester,
        "test/s/one.py",
        """
        from ducktest import run_paired

        def test_one(request, matrix_cell):
            run_paired(request)
        """,
    )
    _write(pytester, "test/s/one.test", "query I\nSELECT 1;\n----\n1\n")
    # A stub that always PASSES -- so without the fix this test would wrongly show `passed=1`
    # (the silent-miss bug), proving `run_paired` blocks the invocation rather than the stub itself.
    stub = pytester.path / "stub_unittest"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            if "-l" in sys.argv[1:]:
                sys.exit(0)
            name = next(a for a in sys.argv[1:] if a.endswith(".test"))
            event = {"event": "end", "name": name, "status": "ok", "passes": 1, "fails": 0, "skip-mode": 0}
            sys.stderr.write("[TEST_EVENT] " + json.dumps(event) + "\\n")
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    result = pytester.runpytest_subprocess(
        "--unittest-binary", str(stub), "-n", "0", "-p", "no:cacheprovider", "test/s"
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*boom: service infra unavailable*"])


def test_shared_service_across_suites_boots_and_stops_once(pytester, monkeypatch):
    """A service shared across two suites (the use_service pattern) is ONE physical resource:
    dedup by key means it boots once and — the regression this guards — stops exactly once, not
    once per suite that binds it (a non-idempotent stop() would otherwise double-fire)."""
    boot = pytester.path / "boot.log"
    stop = pytester.path / "stop.log"
    monkeypatch.setenv("BOOT_LOG", str(boot))
    monkeypatch.setenv("STOP_LOG", str(stop))
    _write(
        pytester,
        "conftest.py",
        """
        import os
        from ducktest import register_suite, service, use_service

        def _start(config):
            with open(os.environ["BOOT_LOG"], "a") as f: f.write("b\\n")
            return {"endpoint": "svc://up"}

        def _stop(config):
            with open(os.environ["STOP_LOG"], "a") as f: f.write("s\\n")

        _BASE = service("svc", start=_start, stop=_stop)

        def pytest_configure(config):
            # two DIFFERENT suites, each binding the SAME shared descriptor eagerly
            register_suite(config, "a", path="a", default=True, services=[use_service(_BASE)])
            register_suite(config, "b", path="b", default=True, services=[use_service(_BASE)])
        """,
    )
    # one item per suite -- each genuinely needs the service via its OWN suite, proving the
    # dedup is real cross-suite sharing and not an artifact of a single item needing it once
    _write(pytester, "a/test_a.py", "def test_a():\n    assert True\n")
    _write(pytester, "b/test_b.py", "def test_b():\n    assert True\n")
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
    assert boot.read_text().count("b") == 1  # shared service booted once (store single-flight)
    assert stop.read_text().count("s") == 1  # stopped ONCE, not once per binding suite (the fix)


# --- populate's block survives past the first provisioning call (regression, driver 84af67a) ----


def test_populate_added_field_survives_repeated_provisioning_calls(pytester, monkeypatch):
    """84af67a split boot and populate into separate store entries; the populate entry published
    the factory's return value (None -- `_run_populate` mutated its block but didn't return it), so
    only the FIRST caller (mutating the object in place, synchronously) ever saw the populated
    field. Every later `provision_service` call -- collect-first's own up-front pass counts as the
    first, so any actual test pulling env is already a "later" call -- read back the raw boot block
    via a fresh store round-trip and lost it. Two items force at least two more calls after the
    up-front one, across two workers."""
    _write(
        pytester,
        "conftest.py",
        """
        from ducktest import register_suite, service

        def _start(config):
            return {"endpoint": "svc://up"}

        def _populate(block, config):
            block["seeded"] = "yes"  # mutates in place, like a real populate() would

        def _to_env(block):
            return {"SVC_SEEDED": block.get("seeded", "missing")}

        def pytest_configure(config):
            register_suite(config, "s", path=".", default=True, services=[
                service("svc", start=_start, populate=_populate, to_env=_to_env)])
        """,
    )
    _write(
        pytester,
        "test_inner.py",
        """
        from ducktest.plugin import _item_env

        def test_one(request):
            assert _item_env(request.config, request.node)["SVC_SEEDED"] == "yes"
            assert _item_env(request.config, request.node)["SVC_SEEDED"] == "yes"  # repeated call

        def test_two(request):
            assert _item_env(request.config, request.node)["SVC_SEEDED"] == "yes"
        """,
    )
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)


def test_populate_added_field_survives_for_a_service_shared_across_suites(pytester, monkeypatch):
    """Same regression, for a descriptor shared by identity across two suites (`_owning_suite_name`
    returns None -- one combined populate key). Each suite's own item must still see the field."""
    _write(
        pytester,
        "conftest.py",
        """
        from ducktest import register_suite, service, use_service

        def _start(config):
            return {"endpoint": "svc://up"}

        def _populate(block, config):
            block["seeded"] = "yes"

        _BASE = service("svc", start=_start, populate=_populate,
                         to_env=lambda b: {"SVC_SEEDED": b.get("seeded", "missing")})

        def pytest_configure(config):
            register_suite(config, "a", path="a", default=True, services=[use_service(_BASE)])
            register_suite(config, "b", path="b", default=True, services=[use_service(_BASE)])
        """,
    )
    _write(
        pytester,
        "a/test_a.py",
        "from ducktest.plugin import _item_env\n"
        "def test_a(request):\n"
        "    assert _item_env(request.config, request.node)['SVC_SEEDED'] == 'yes'\n",
    )
    _write(
        pytester,
        "b/test_b.py",
        "from ducktest.plugin import _item_env\n"
        "def test_b(request):\n"
        "    assert _item_env(request.config, request.node)['SVC_SEEDED'] == 'yes'\n",
    )
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)


# --- cell-narrowed reachability (RESOURCE-PLANNING.md phase 10) ---------------------------


def test_cell_narrowing_preserves_the_needed_suites_service_policy(pytester, monkeypatch):
    """A shared service key needed only by suite B must use B's suite-specific descriptor. Suite A's
    selected cell explicitly excludes the service, so its ``populate`` policy must remain inert."""
    population = pytester.path / "population.log"
    monkeypatch.setenv("POPULATION_LOG", str(population))
    _write(
        pytester,
        "conftest.py",
        """
        import os
        from ducktest import register_suite, service, use_service

        BASE = service("shared", start=lambda c: {"endpoint": "svc://shared"})

        def _populate(name):
            def populate(block, config):
                with open(os.environ["POPULATION_LOG"], "a") as f:
                    f.write(name + "\\n")
            return populate

        def pytest_configure(config):
            register_suite(config, "a", path="a",
                services=[use_service(BASE, populate=_populate("a"))],
                matrix=[{"id": "without-shared", "services": []}])
            register_suite(config, "b", path="b",
                services=[use_service(BASE, populate=_populate("b"))],
                matrix=[{"id": "with-shared", "services": ["shared"]}])
        """,
    )
    _write(pytester, "a/test_a.py", "def test_a(matrix_cell):\n    pass\n")
    _write(pytester, "b/test_b.py", "def test_b(matrix_cell):\n    pass\n")
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
    assert population.read_text() == "b\n"


def test_cell_narrowing_lets_both_suites_populate_a_genuinely_shared_key(pytester, monkeypatch):
    """Unlike the case above, BOTH suites' selected cells genuinely need the shared key -- this is
    the deliberate design point (RESOURCE-PLANNING.md phase 10): populate is per-suite, not a single
    global winner, so both suites' policy applies, cumulatively, exactly once each -- coordinated
    through the store even across the controller and multiple workers (`-n 2`)."""
    population = pytester.path / "population.log"
    monkeypatch.setenv("POPULATION_LOG", str(population))
    _write(
        pytester,
        "conftest.py",
        """
        import os
        from ducktest import register_suite, service, use_service

        BASE = service("shared", start=lambda c: {"endpoint": "svc://shared"})

        def _populate(name):
            def populate(block, config):
                with open(os.environ["POPULATION_LOG"], "a") as f:
                    f.write(name + "\\n")
            return populate

        def pytest_configure(config):
            register_suite(config, "a", path="a",
                services=[use_service(BASE, populate=_populate("a"))],
                matrix=[{"id": "with-shared", "services": ["shared"]}])
            register_suite(config, "b", path="b",
                services=[use_service(BASE, populate=_populate("b"))],
                matrix=[{"id": "with-shared", "services": ["shared"]}])
        """,
    )
    _write(pytester, "a/test_a.py", "def test_a(matrix_cell):\n    pass\n")
    _write(pytester, "b/test_b.py", "def test_b(matrix_cell):\n    pass\n")
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
    lines = population.read_text().splitlines()
    assert sorted(lines) == ["a", "b"]  # each suite's own populate ran, exactly once each


_NARROWED_CELL_CONFTEST = """
    import os
    from ducktest import register_suite, service

    def _good_start(config):
        with open(os.environ["BOOT_LOG"], "a") as f: f.write("good\\n")
        return {"endpoint": "svc://good"}

    def _bad_start(config):
        raise RuntimeError("boom: bad service should never be attempted")

    def pytest_configure(config):
        register_suite(config, "s", path=".", default=True,
            services=[
                service("good_svc", start=_good_start, to_env=lambda b: {"E": b["endpoint"]}),
                service("bad_svc", start=_bad_start, to_env=lambda b: {"E": b["endpoint"]}),
            ],
            matrix=[
                {"id": "good", "services": ["good_svc"]},
                {"id": "bad", "services": ["bad_svc"]},
            ])
"""

_NARROWED_CELL_TEST = """
    # pull the cell's own env eagerly (not just assert matrix_cell) so a swallowed collect-first
    # failure re-surfaces here (same reasoning as run_paired's own re-provisioning)
    from ducktest.plugin import _item_env

    def test_plain(request, matrix_cell):
        _item_env(request.config, request.node)
"""


def test_selecting_one_cell_never_boots_the_other_cells_service(pytester, monkeypatch):
    """The motivating case: a suite's two cells need DIFFERENT services (`good_svc`/`bad_svc`).
    Selecting only the `good` cell must never even attempt `bad_svc` -- not "attempt and swallow",
    genuinely never call its `start`. `-n0` so the controller's own up-front provisioning
    (`_build_plan`/`provision_reachable`) is the only pass, not masked by worker-side narrowing."""
    boot = pytester.path / "boot.log"
    monkeypatch.setenv("BOOT_LOG", str(boot))
    _write(pytester, "conftest.py", _NARROWED_CELL_CONFTEST)
    _write(pytester, "test_inner.py", _NARROWED_CELL_TEST)
    result = pytester.runpytest_subprocess("-n", "0", "-k", "good", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, deselected=1)
    assert boot.read_text().count("good") == 1
    assert "boom" not in result.stdout.str()


def test_selecting_the_other_cell_still_fails_loud_for_its_own_service(pytester, monkeypatch):
    # positive control: narrowing must not also swallow a genuinely-needed failure.
    boot = pytester.path / "boot.log"
    monkeypatch.setenv("BOOT_LOG", str(boot))
    _write(pytester, "conftest.py", _NARROWED_CELL_CONFTEST)
    _write(pytester, "test_inner.py", _NARROWED_CELL_TEST)
    result = pytester.runpytest_subprocess("-n", "0", "-k", "bad", "-p", "no:cacheprovider")
    result.stdout.fnmatch_lines(["*boom: bad service should never be attempted*"])
    assert not boot.exists()  # good_svc, not needed by the selected cell, never attempted either
