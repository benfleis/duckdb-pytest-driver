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


def test_unpaired_slow_body_is_not_suppressed_by_same_stem_python(pytester):
    stub, _ = _stub_binary(pytester)
    _write(pytester, "test/sql/large.test_slow")
    _write(pytester, "test/sql/large.py", "# Slow paired drivers are not supported yet.\n")

    result = _run(pytester, stub, "--slow")

    result.assert_outcomes(passed=1)


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
