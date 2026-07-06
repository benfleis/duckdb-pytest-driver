"""Self-tests for the `.test` collector lane, run against a STUB unittest binary.

The stub (an executable script dropped in a tmp dir) mimics the real Catch2 binary's
`--emit-test-events` contract: for each selected `.test` it writes a
`[TEST_EVENT] {json}` `end` line to stderr. Any test whose name contains "fail" is reported
as `status:error` (a forced mismatch). This lets us prove — offline — that:
  * a driverless `.test` is collected and run, and a pass is reported;
  * a forced mismatch surfaces as a pytest failure;
  * batched execution attributes pass/fail per test.

The plugin is exercised as it ships: auto-registered via its pytest11 entry point, pointed at
the stub with `--unittest-binary`, with working_dir/test_root auto-detected from the rootdir.
"""

import stat
import textwrap

# A tiny stub "unittest" binary. Python (not bash) to keep the JSON emission unambiguous;
# it is still just a small executable script dropped into the run's tmp dir.
STUB = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys

    def names(argv):
        out = []
        it = iter(argv)
        for a in it:
            if a == "-f":                       # batch: names live in the following file
                path = next(it, None)
                if path:
                    with open(path) as f:
                        out += [ln.strip() for ln in f if ln.strip()]
            elif a.endswith(".test"):           # single: name passed directly
                out.append(a)
        return out

    for n in names(sys.argv[1:]):
        sys.stdout.write("running %s\\n" % n)
        if "fail" in n:
            ev = {"event": "end", "name": n, "status": "error",
                  "passes": 0, "fails": 1, "skip-mode": 0, "data": "forced mismatch"}
        else:
            ev = {"event": "end", "name": n, "status": "ok",
                  "passes": 1, "fails": 0, "skip-mode": 0}
        sys.stderr.write("[TEST_EVENT] " + json.dumps(ev) + "\\n")
    sys.exit(0)
    """
)


def _stub_binary(pytester):
    p = pytester.path / "stub_unittest"
    p.write_text(STUB)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _make_test(pytester, relpath, body="query I\nSELECT 42;\n----\n42\n"):
    f = pytester.path / relpath
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    return f


def _run(pytester, stub):
    # Subprocess run so the installed pytest11 entry point loads exactly like a real consumer.
    # No conftest, no pytest.ini: working_dir=rootdir and test_root=<rootdir>/test auto-detect.
    return pytester.runpytest_subprocess("--unittest-binary", str(stub), "-p", "no:cacheprovider")


def test_driverless_test_is_collected_and_passes(pytester):
    stub = _stub_binary(pytester)
    _make_test(pytester, "test/sql/answer.test")
    result = _run(pytester, stub)
    result.assert_outcomes(passed=1)


def test_forced_mismatch_is_reported_as_failure(pytester):
    stub = _stub_binary(pytester)
    _make_test(pytester, "test/sql/mismatch_fail.test")
    result = _run(pytester, stub)
    result.assert_outcomes(failed=1)


def test_batch_attributes_pass_and_fail_per_test(pytester):
    stub = _stub_binary(pytester)
    _make_test(pytester, "test/sql/a_ok.test")
    _make_test(pytester, "test/sql/b_fail.test")
    result = _run(pytester, stub)
    result.assert_outcomes(passed=1, failed=1)


def test_ignore_dirs_are_skipped(pytester):
    # A .test under a default-ignored top-level dir (build/) must NOT be collected.
    stub = _stub_binary(pytester)
    _make_test(pytester, "test/sql/answer.test")
    _make_test(pytester, "build/leftover.test")
    result = _run(pytester, stub)
    result.assert_outcomes(passed=1)


def test_public_api_and_compat_alias():
    import duckdb_pytest_driver as d
    import driver

    for name in d.__all__:
        assert hasattr(d, name), name
    # the `driver` shim re-exports the same objects
    assert driver.run_paired is d.run_paired
    assert driver.plugin is d.plugin
