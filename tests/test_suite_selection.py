"""Self-tests for Phase-1 suite selection: auto-marker + default-scan deselection + banner.

Offline: no duckdb/op/docker/network. Each case spins up an isolated inner pytest run (subprocess,
via pytester) so REAL hook ordering — our auto-marker vs pytest's builtin `-m`/`-k` deselection —
is exercised, not a mocked config. An inner ``test/conftest.py`` registers two suites: a default
"smoke" suite and a non-default "cloud" suite (path + marker). Dummy items live in and out of the
cloud path; ``cloud_body.py`` carries NO Python marker (only path membership) to prove the
auto-marker beats `-m cloud` filtering the way a real `.test`/SQLLogic body would.

Proven:
  * bare run              -> cloud deselected, smoke kept; the banner line appears.
  * -m cloud              -> cloud selected (incl. the marker-less body item), smoke deselected.
  * -m 'not cloud'        -> cloud deselected.
  * a path into the cloud dir -> cloud selected, NO banner (explicit selection).
  * -k <substr>           -> no default deselection (respected verbatim).
  * vanilla (no suites)    -> identical to today: no banner, nothing deselected.
"""

import textwrap


def _write(pytester, name, body):
    p = pytester.path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


# A conftest registering a default "smoke" suite and a non-default "cloud" suite (path + marker).
_SUITE_CONFTEST = """
    from ducktest import register_suite


    def pytest_configure(config):
        register_suite(config, "smoke", path="test/smoke", default=True)
        register_suite(config, "cloud", path="test/cloud", default=False)
"""

# Dummy items: two smoke, one cloud with a Python @pytest.mark.cloud, and one cloud "body" with NO
# marker (path membership only). The body item proves the auto-marker makes `-m cloud` see it.
_SMOKE_A = "def test_smoke_a(): pass"
_SMOKE_B = "def test_smoke_b(): pass"
_CLOUD_MARKED = """
    import pytest

    @pytest.mark.cloud
    def test_cloud_marked(): pass
"""
_CLOUD_BODY = "def test_cloud_body(): pass"  # no marker: only path membership -> auto-marked


def _lay_out(pytester):
    _write(pytester, "test/conftest.py", _SUITE_CONFTEST)
    _write(pytester, "test/smoke/test_smoke_a.py", _SMOKE_A)
    _write(pytester, "test/smoke/test_smoke_b.py", _SMOKE_B)
    _write(pytester, "test/cloud/test_cloud_marked.py", _CLOUD_MARKED)
    # A body-like item: carries NO Python @pytest.mark, belongs to the cloud suite only by path
    # (the proxy for a `.test`/SQLLogic body). The auto-marker must give it the `cloud` marker.
    _write(pytester, "test/cloud/test_cloud_body.py", _CLOUD_BODY)


def _run(pytester, *args):
    return pytester.runpytest_subprocess("-p", "no:cacheprovider", *args)


def test_bare_run_deselects_cloud_and_shows_banner(pytester):
    _lay_out(pytester)
    result = _run(pytester)
    # smoke kept (2), cloud (2) deselected on the bare/default run.
    result.assert_outcomes(passed=2, deselected=2)
    result.stdout.fnmatch_lines(
        ["*duck-test suites: default set selected; deselected: cloud (pass a path or -m cloud to include)*"]
    )


def test_m_cloud_selects_cloud_including_markerless_body(pytester):
    # -m cloud must select BOTH cloud items — including cloud_body.py, which carries no Python
    # marker. That only works if our auto-marker runs BEFORE pytest's builtin -m deselection.
    _lay_out(pytester)
    result = _run(pytester, "-m", "cloud")
    result.assert_outcomes(passed=2, deselected=2)  # 2 cloud selected, 2 smoke deselected by -m
    # No default-scan banner on an explicit selection.
    result.stdout.no_fnmatch_line("*duck-test suites: default set selected*")


def test_m_not_cloud_deselects_cloud(pytester):
    _lay_out(pytester)
    result = _run(pytester, "-m", "not cloud")
    result.assert_outcomes(passed=2, deselected=2)  # 2 smoke selected, 2 cloud deselected


def test_path_into_cloud_selects_cloud_no_banner(pytester):
    _lay_out(pytester)
    result = _run(pytester, "test/cloud")
    result.assert_outcomes(passed=2)  # both cloud items run
    result.stdout.no_fnmatch_line("*duck-test suites: default set selected*")


def test_k_selection_is_respected_verbatim(pytester):
    # -k is an explicit selection -> NO default-scan deselection; pytest's own -k does the filtering.
    # -k smoke selects only the 2 smoke tests; the 2 cloud tests are deselected by -k, not by us,
    # and there is no default-scan banner.
    _lay_out(pytester)
    result = _run(pytester, "-k", "smoke")
    result.assert_outcomes(passed=2, deselected=2)
    result.stdout.no_fnmatch_line("*duck-test suites: default set selected*")


def test_k_cloud_selects_cloud_no_default_scan(pytester):
    # A -k that names cloud tests keeps them (no default-scan deselection stealing them back).
    _lay_out(pytester)
    result = _run(pytester, "-k", "cloud")
    result.assert_outcomes(passed=2, deselected=2)  # 2 cloud kept, 2 smoke dropped by -k
    result.stdout.no_fnmatch_line("*duck-test suites: default set selected*")


def test_vanilla_no_suites_is_unaffected(pytester):
    # No suite declared anywhere -> vanilla: nothing deselected, no banner, everything runs.
    _write(pytester, "test/test_a.py", "def test_a(): pass")
    _write(pytester, "test/test_b.py", "def test_b(): pass")
    result = _run(pytester)
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*duck-test suites*")
