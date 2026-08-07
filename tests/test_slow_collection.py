import json
import stat
import textwrap


def _write(pytester, name, body="query I\nSELECT 42;\n----\n42\n"):
    path = pytester.path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))
    return path


def _stub_binary(pytester):
    log = pytester.path / "invocations.jsonl"
    script = pytester.path / "stub_unittest"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, os, sys

            def names(argv):
                out = []
                it = iter(argv)
                for arg in it:
                    if arg == "-f":
                        path = next(it, None)
                        if path:
                            with open(path) as handle:
                                out.extend(line.strip() for line in handle if line.strip())
                    elif arg.endswith((".test", ".test_slow", ".test_coverage")):
                        out.append(arg)
                return out

            selected = names(sys.argv[1:])
            if selected:
                with open({str(log)!r}, "a") as handle:
                    handle.write(json.dumps({{"args": sys.argv[1:], "names": selected,
                                             "cell": os.environ.get("CELL")}}) + "\\n")
            for name in selected:
                event = {{"event": "end", "name": name, "status": "ok",
                         "passes": 1, "fails": 0, "skip-mode": 0}}
                sys.stderr.write("[TEST_EVENT] " + json.dumps(event) + "\\n")
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script, log


def _run(pytester, stub, *args):
    return pytester.runpytest_subprocess(
        "--unittest-binary", str(stub), "-p", "no:cacheprovider", *args
    )


def _invocations(log):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def _make_all_body_types(pytester):
    _write(pytester, "test/sql/regular.test")
    _write(pytester, "test/sql/large.test_slow")
    _write(pytester, "test/sql/instrumented.test_coverage")


def test_slow_bodies_are_opt_in(pytester):
    stub, _ = _stub_binary(pytester)
    _make_all_body_types(pytester)

    result = _run(pytester, stub)

    result.assert_outcomes(passed=1)


def test_slow_option_collects_slow_and_coverage_bodies(pytester):
    stub, _ = _stub_binary(pytester)
    _make_all_body_types(pytester)

    result = _run(pytester, stub, "--slow")

    result.assert_outcomes(passed=3)


_PAIRED_DRIVER = """
    import os
    from ducktest import run_paired

    def test_large(request):
        # proves the .py's OWN test function executed, distinct from the stub binary having been
        # invoked with the right name -- a bare (unsuppressed) body would ALSO produce that
        # invocation without ever running this function
        with open(os.environ["DRIVER_RAN_LOG"], "a") as f:
            f.write("ran\\n")
        run_paired(request)
"""


def test_slow_paired_driver_suppresses_the_bare_body_and_runs_the_exact_slow_name(pytester, monkeypatch):
    ran = pytester.path / "driver_ran.log"
    monkeypatch.setenv("DRIVER_RAN_LOG", str(ran))
    stub, log = _stub_binary(pytester)
    _write(pytester, "test/sql/large.test_slow")
    _write(pytester, "test/sql/large.py", _PAIRED_DRIVER)

    result = _run(pytester, stub, "--slow")

    result.assert_outcomes(passed=1)  # the driver's own test, not a second item for the bare body
    assert ran.read_text().count("ran") == 1  # the driver's Python actually ran, body wasn't bare
    invocations = _invocations(log)
    assert [call["names"] for call in invocations] == [["test/sql/large.test_slow"]]


def test_slow_paired_driver_is_not_collected_without_slow(pytester, monkeypatch):
    """The --slow gate that already applies to a bare slow body must apply to its driver too --
    otherwise a plain `pytest` run would silently execute an expensive body it never asked for."""
    ran = pytester.path / "driver_ran.log"
    monkeypatch.setenv("DRIVER_RAN_LOG", str(ran))
    stub, log = _stub_binary(pytester)
    _write(pytester, "test/sql/large.test_slow")
    _write(pytester, "test/sql/large.py", _PAIRED_DRIVER)

    result = _run(pytester, stub)

    result.assert_outcomes()  # nothing collected: no bare body (unpaired-slow gate), no driver either
    assert not ran.exists()
    assert _invocations(log) == []


def test_slow_paired_driver_carries_the_slow_marker(pytester, monkeypatch):
    monkeypatch.setenv("DRIVER_RAN_LOG", str(pytester.path / "driver_ran.log"))
    stub, _ = _stub_binary(pytester)
    _write(pytester, "test/sql/large.test_slow")
    _write(pytester, "test/sql/large.py", _PAIRED_DRIVER)
    _write(pytester, "test/sql/regular.test")

    result = _run(pytester, stub, "--slow", "-m", "slow")

    result.assert_outcomes(passed=1, deselected=1)


def test_coverage_paired_driver_is_also_supported(pytester, monkeypatch):
    # .test_coverage is a slow-lane suffix too (_SLOW_SUFFIXES) -- pairing must work identically.
    ran = pytester.path / "driver_ran.log"
    monkeypatch.setenv("DRIVER_RAN_LOG", str(ran))
    stub, log = _stub_binary(pytester)
    _write(pytester, "test/sql/instrumented.test_coverage")
    _write(pytester, "test/sql/instrumented.py", _PAIRED_DRIVER)

    result = _run(pytester, stub, "--slow")

    result.assert_outcomes(passed=1)
    assert ran.read_text().count("ran") == 1
    invocations = _invocations(log)
    assert [call["names"] for call in invocations] == [["test/sql/instrumented.test_coverage"]]


def test_fast_paired_driver_is_unaffected_by_the_slow_gate(pytester, monkeypatch):
    # regression guard: the new driver_body_path resolution must still prefer .test first and
    # never require --slow for an ordinary fast pairing.
    ran = pytester.path / "driver_ran.log"
    monkeypatch.setenv("DRIVER_RAN_LOG", str(ran))
    stub, log = _stub_binary(pytester)
    _write(pytester, "test/sql/answer.test")
    _write(pytester, "test/sql/answer.py", _PAIRED_DRIVER)

    result = _run(pytester, stub)

    result.assert_outcomes(passed=1)
    assert ran.read_text().count("ran") == 1
    invocations = _invocations(log)
    assert [call["names"] for call in invocations] == [["test/sql/answer.test"]]


def test_naming_a_slow_body_on_the_cli_redirects_to_its_driver(pytester, monkeypatch):
    # the config.args rewrite (pytest_configure) that sends an explicitly-named body to its same-
    # stem driver used to hardcode ".test" -- naming a .test_slow directly would otherwise collect
    # nothing, since the body is still suppressed by has_driver regardless of the arg rewrite.
    ran = pytester.path / "driver_ran.log"
    monkeypatch.setenv("DRIVER_RAN_LOG", str(ran))
    stub, log = _stub_binary(pytester)
    _write(pytester, "test/sql/large.test_slow")
    _write(pytester, "test/sql/large.py", _PAIRED_DRIVER)

    result = _run(pytester, stub, "--slow", "test/sql/large.test_slow")

    result.assert_outcomes(passed=1)
    assert ran.read_text().count("ran") == 1
    invocations = _invocations(log)
    assert [call["names"] for call in invocations] == [["test/sql/large.test_slow"]]


def test_slow_marker_is_available_to_marker_selection(pytester):
    stub, _ = _stub_binary(pytester)
    _make_all_body_types(pytester)

    result = _run(pytester, stub, "--slow", "-m", "slow")

    result.assert_outcomes(passed=2, deselected=1)


def test_slow_body_runs_alone_with_exact_registered_name(pytester):
    stub, log = _stub_binary(pytester)
    _write(pytester, "test/sql/regular.test")
    _write(pytester, "test/sql/other.test")
    _write(pytester, "test/sql/large.test_slow")

    result = _run(pytester, stub, "--slow", "--batch-size", "10")

    result.assert_outcomes(passed=3)
    invocations = _invocations(log)
    assert sorted(len(call["names"]) for call in invocations) == [1, 2]
    assert ["test/sql/large.test_slow"] in [call["names"] for call in invocations]


def test_slow_matrix_cells_remain_marked_and_isolated(pytester):
    stub, log = _stub_binary(pytester)
    _write(
        pytester,
        "test/conftest.py",
        """
        from ducktest import register_suite

        def pytest_configure(config):
            register_suite(config, "matrixed", path="test/matrixed", matrix=[
                {"id": "a", "properties": {"CELL": "a"}},
                {"id": "b", "properties": {"CELL": "b"}},
            ])
        """,
    )
    _write(pytester, "test/matrixed/large.test_slow")

    result = _run(pytester, stub, "--slow", "-m", "slow", "--batch-size", "10")

    result.assert_outcomes(passed=2)
    invocations = _invocations(log)
    assert [call["names"] for call in invocations] == [
        ["test/matrixed/large.test_slow"],
        ["test/matrixed/large.test_slow"],
    ]
    assert {call["cell"] for call in invocations} == {"a", "b"}
