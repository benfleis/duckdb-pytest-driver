"""Self-tests for the optional `Provisioner.backend()` hook (RESOURCE-PLANNING.md phase 4).

Offline, single process (`-n0`): two DIFFERENT `Provisioner` subclasses, registered under two
different scopes, targeting the SAME RO table name. Proves two things:

  - default (no `backend()` override): scoping stays per-CLASS, exactly what phase 3 shipped --
    the two classes do NOT share, so the fake `instantiate()` runs once per class.
  - `backend()` overridden to the SAME name on both: `_ro_store_key` scopes by that name instead,
    so the two classes now DO share -- the RO-shared-identity guarantee
    (`decorate.coordination_key`) is really about `backend`, not which Python class implements it.
"""

import os
import textwrap


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


_ROOT_CONFTEST = """
    from ducktest import register_suite, service

    def pytest_configure(config):
        register_suite(config, "root", default=True, services=[service("svc", start=lambda c: {"ok": True})])
"""

_SUB_CONFTEST = """
    import os
    from ducktest import register_provisioner
    from ducktest.provision import Provisioner


    class _{cls_name}(Provisioner):
        def execute(self, sql):
            pass

        def rw_target(self, spec, token, state, dry_run):
            return f"rw_{{{{token}}}}"

        def ro_target(self, spec, state):
            return "shared_ro_table"

        def instantiate(self, spec, target, dry_run, state):
            with open(os.environ["LOG"], "a") as f:
                f.write("{tag}:" + target + "\\n")

        def make_init_sql(self, bindings, *, redact=False):
            return ""

        def backend(self):
            return os.environ.get("{backend_env}")


    def pytest_configure(config):
        register_provisioner(config, _{cls_name}(), scope=os.path.dirname(__file__))
"""

_RO_TEST = """
    from ducktest import requires

    @requires(source="db.schema.shared_ro_table", access="ro")
    def test_{name}(resources):
        assert True
"""


def _setup(pytester):
    _write(pytester, "conftest.py", _ROOT_CONFTEST)
    _write(
        pytester,
        "a/conftest.py",
        _SUB_CONFTEST.format(cls_name="ProvA", tag="A", backend_env="BACKEND_NAME_A"),
    )
    _write(
        pytester,
        "b/conftest.py",
        _SUB_CONFTEST.format(cls_name="ProvB", tag="B", backend_env="BACKEND_NAME_B"),
    )
    _write(pytester, "a/test_a.py", _RO_TEST.format(name="a"))
    _write(pytester, "b/test_b.py", _RO_TEST.format(name="b"))


def test_default_scoping_is_per_class_not_shared(pytester, monkeypatch):
    log = pytester.path / "instantiate.log"
    monkeypatch.setenv("LOG", str(log))
    monkeypatch.delenv("BACKEND_NAME_A", raising=False)
    monkeypatch.delenv("BACKEND_NAME_B", raising=False)
    _setup(pytester)
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
    lines = log.read_text().splitlines()
    assert sorted(lines) == ["A:shared_ro_table", "B:shared_ro_table"]  # each class instantiates its own


def test_shared_backend_name_shares_ro_across_provisioner_classes(pytester, monkeypatch):
    log = pytester.path / "instantiate.log"
    monkeypatch.setenv("LOG", str(log))
    monkeypatch.setenv("BACKEND_NAME_A", "shared-backend")
    monkeypatch.setenv("BACKEND_NAME_B", "shared-backend")
    _setup(pytester)
    result = pytester.runpytest_subprocess("-n", "0", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
    lines = log.read_text().splitlines()
    assert len(lines) == 1  # single-flighted across the two DIFFERENT classes, by shared backend name


def test_ro_store_key_uses_backend_override_when_present():
    """Unit-level: `_ro_store_key` prefers `self.backend()`; default (None) falls back to the
    class-qualname, the phase-3 behavior every existing Provisioner subclass keeps unchanged."""
    from ducktest.provision import Provisioner

    class _Named(Provisioner):
        def execute(self, sql):
            pass

        def rw_target(self, spec, token, state, dry_run):
            return "rw"

        def ro_target(self, spec, state):
            return "t"

        def instantiate(self, spec, target, dry_run, state):
            pass

        def make_init_sql(self, bindings, *, redact=False):
            return ""

        def backend(self):
            return "explicit-name"

    class _Unnamed(_Named):
        def backend(self):
            return None

    assert _Named()._ro_store_key("tbl") == "ro::explicit-name::tbl"
    unnamed_key = _Unnamed()._ro_store_key("tbl")
    assert unnamed_key == f"ro::{__name__}.{_Unnamed.__qualname__}::tbl"
    assert os.linesep not in unnamed_key
