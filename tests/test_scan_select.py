"""ducktest's scan, configuration files and selection, on a small tree made for each test."""

import re
import sys
import textwrap

import pytest

from ducktest.binary import UsageError
from ducktest.scan import scan
from ducktest.select import Selection, apply
from ducktest import leases
from ducktest.testenv import resolve

ROOT_CONF = """
    from ducktest import Project, default, match

    project = Project(
        matrix={"backend": {"oss": {"values": {"catalog": "duck"}}, "dbx": {"tags": ["needs-credentials"]}}},
        suites={
            "smoke": {"paths": ["test/a", "test/b/one.test"]},
            "cmt": {"select": ["case=cmt"]},
            "combo": {"select": ["tag=mine", "backend=oss"]},
            "trimmed": {"paths": ["test/a", "test/b"], "skip": ["backend=oss;tag=a"]},
        },
        rules=[default(tags=["all"], values={"region": "eu"}), match("a/**", tags=["a"], timeout=30)],
    )
"""


def tree(tmp_path, files):
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    return scan(str(tmp_path / "test"), working_dir=str(tmp_path))


@pytest.fixture
def project(tmp_path):
    return tree(
        tmp_path,
        {
            "test/ducktest_conf.py": ROOT_CONF,
            "test/a/ducktest_conf.py": """
                from ducktest import Rules, default, match
                rules = Rules(default(values={"region": "us"}), match("x.test", matrix=["backend"], timeout=5))
            """,
            "test/a/x.test": "",
            "test/a/y.test_slow": "",
            "test/b/one.test": "",
            "test/b/paired.test": "",
            "test/b/paired.py": """
                from ducktest import test
                @test(matrix=[{"case": {"cmt": {}, "plain": {}}}], tags=["mine"])
                def body(ctx): ctx.run_paired()
            """,
            "test/b/solo.py": """
                from ducktest import test
                @test()
                def solo(ctx): pass
            """,
        },
    )


@pytest.fixture
def dnf_project(tmp_path):
    """A tree with two parameters, a python test and a tagged one -- for the disjunctive-normal-form and
    `path=` selection rules, which need more shape than `project`'s single parameter gives."""
    return tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, default, match
                project = Project(
                    matrix={"backend": {"oss": {}, "dbx": {}}, "storage": {"s3": {}, "gcs": {}}},
                    rules=[default(matrix=["backend", "storage"]), match("spark/**", tags=["spark"])],
                )
            """,
            "test/sql/cloud/a.test": "",
            "test/sql/cloud/sub/b.test": "",
            "test/sql/local/c.test": "",
            "test/py/d.py": """
                from ducktest import test
                @test()
                def d(ctx): pass
            """,
            "test/spark/e.test": "",
        },
    )


def ids(tests):
    return [c.name for c in tests]


def test_files_become_tests_by_name_only_and_tests_multiply(project):
    tests, _ = project
    assert ids(tests) == [
        "a/x.test::x[backend=oss]",
        "a/x.test::x[backend=dbx]",
        "a/y.test_slow::y",
        "b/one.test::one",
        "b/paired.py::body[case=cmt]",
        "b/paired.py::body[case=plain]",
        "b/solo.py::solo",
    ]
    by_id = {c.name: c for c in tests}
    assert by_id["b/paired.py::body[case=cmt]"].definition.type == "paired"
    assert by_id["b/paired.py::body[case=cmt]"].definition.body_file == "test/b/paired.test"
    assert by_id["b/solo.py::solo"].definition.type == "python"


def test_an_id_is_the_same_from_every_working_directory(tmp_path, project):
    from_below = scan(str(tmp_path / "test"), working_dir=str(tmp_path / "test" / "a"))[0]
    assert ids(from_below) == ids(project[0])
    # the path the binary and the PATH arguments use does follow the working directory
    assert [c.definition.path for c in from_below][:2] == ["x.test", "x.test"]


def test_the_parts_of_an_id_come_in_the_order_the_rules_then_the_test_name_the_parameters(tmp_path):
    tests, _ = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, default, match
                project = Project(
                    matrix={"backend": {"oss": {}}, "storage": {"s3": {}}},
                    rules=[default(matrix=["storage"]), match("a.py", matrix=["backend", "storage"])],
                )
            """,
            "test/a.py": """
                from ducktest import test
                @test(matrix=[{"case": {"one": 1, "two": 2}}, "backend"])
                def body(ctx): pass
            """,
        },
    )
    assert ids(tests) == [
        "a.py::body[storage=s3,backend=oss,case=one]",
        "a.py::body[storage=s3,backend=oss,case=two]",
    ]


def test_a_test_directory_name_has_no_glob_or_hive_partition_characters(tmp_path):
    tests, _ = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, default
                project = Project(matrix={"storage": {"s3": {}}}, rules=[default(matrix=["storage"])])
            """,
            "test/a.py": """
                from ducktest import test
                @test(matrix=[{"case": {"plain": {}}}])
                def body(ctx): pass
            """,
        },
    )
    assert re.fullmatch(r"body_storage_s3,case_plain_a\.py-[0-9a-f]{6}", tests[0].dir_name)


def test_rules_apply_from_the_root_down_and_the_test_itself_last(project):
    x = next(c for c in project[0] if c.name == "a/x.test::x[backend=oss]")
    assert x.definition.tags == ["all", "a"]
    # the deeper file replaced `region`, the matrix value added `catalog`, and the parameter brings its name
    assert resolve(x).value_map == {"backend": "oss", "region": "us", "catalog": "duck"}
    assert resolve(x).values["region"].source == "a/ducktest_conf.py: default"
    assert resolve(x).values["catalog"].source == "matrix backend=oss"
    assert x.definition.timeout == 5  # a single value: the last rule that set it
    assert any("a/ducktest_conf.py: match 'x.test'" in line for line in x.definition.why)
    paired = next(c for c in project[0] if c.name == "b/paired.py::body[case=cmt]")
    assert paired.definition.tags == ["all", "mine"]
    assert "needs-credentials" in next(c for c in project[0] if c.name.endswith("[backend=dbx]")).tags


def test_a_rule_that_matches_nothing_is_an_error(tmp_path):
    with pytest.raises(UsageError, match="matched no test"):
        tree(
            tmp_path,
            {
                "test/ducktest_conf.py": "from ducktest import Project, match\nproject = Project(rules=[match('s3/authentcated/**', tags=['x'])])",
                "test/s3/authenticated/a.test": "",
            },
        )


def test_an_unknown_rule_field_or_parameter_is_an_error(tmp_path):
    with pytest.raises(UsageError, match=r"unknown option\(s\) \['needs_env'\]"):
        tree(
            tmp_path,
            {
                "test/ducktest_conf.py": "from ducktest import Project, default\nproject = Project(rules=[default(needs_env=['X'])])",
                "test/a.test": "",
            },
        )
    with pytest.raises(UsageError, match="runs over parameter 'nope'; the project's matrix has"):
        tree(
            tmp_path / "2",
            {
                "test/ducktest_conf.py": "from ducktest import Project, default\nproject = Project(rules=[default(matrix=['nope'])])",
                "test/a.test": "",
            },
        )


def select(project, **kwargs):
    tests, proj = project
    return apply(tests, Selection(**kwargs), proj)


def test_a_bare_run_is_the_built_in_default_and_says_why(project):
    selected, why_not = select(project)
    assert "a/y.test_slow::y" not in ids(selected) and len(selected) == 6
    assert why_not["a/y.test_slow::y"] == "not in suite 'default': tag slow"
    assert "a/y.test_slow::y" in ids(select(project, suites=["all"])[0])
    assert ids(select(project, select=["tag=slow"])[0]) == ["a/y.test_slow::y"]


def test_paths_are_a_union_and_a_clauses_terms_must_all_hold(project):
    assert len(select(project, paths=["a/x.test", "b/one.test"])[0]) == 3
    # scope (PATHs) narrows first, the select clause filters within it
    assert ids(select(project, paths=["a"], select=["backend=oss"])[0]) == ["a/x.test::x[backend=oss]"]
    # ';' inside one --select is and: type paired and case cmt
    assert ids(select(project, select=["type=paired;case=cmt"])[0]) == ["b/paired.py::body[case=cmt]"]
    assert ids(select(project, select=["case=cmt,plain"])[0]) == [
        "b/paired.py::body[case=cmt]",
        "b/paired.py::body[case=plain]",
    ]


def test_repeated_select_ors_its_clauses(project):
    # under the old AND-across-options rule this pair would be empty: no test is both backend=oss and tag=mine
    assert ids(select(project, select=["backend=oss", "tag=mine"])[0]) == [
        "a/x.test::x[backend=oss]",
        "b/paired.py::body[case=cmt]",
        "b/paired.py::body[case=plain]",
    ]


def test_a_suite_is_paths_and_selects_with_a_name(project):
    # the smoke suite's paths are directories/files, and a directory brings in the slow test under it
    assert ids(select(project, suites=["smoke"])[0]) == [
        "a/x.test::x[backend=oss]",
        "a/x.test::x[backend=dbx]",
        "a/y.test_slow::y",
        "b/one.test::one",
    ]
    assert ids(select(project, suites=["cmt"])[0]) == ["b/paired.py::body[case=cmt]"]


def test_an_id_is_taken_verbatim(project):
    assert ids(select(project, ids=["b/paired.py::body[case=plain]"])[0]) == ["b/paired.py::body[case=plain]"]


def test_an_id_is_added_to_what_paths_and_selects_give(project):
    slow, plain = "a/y.test_slow::y", "b/paired.py::body[case=plain]"
    selected, why_not = select(project, paths=["b/one.test"], ids=[plain])
    assert ids(selected) == ["b/one.test::one", plain]
    assert why_not["b/solo.py::solo"] == "outside the given paths"
    # no select, path or tag takes out a test that is named by id
    assert ids(select(project, select=["type=python"], ids=[slow])[0]) == [slow, "b/solo.py::solo"]
    assert ids(select(project, suites=["cmt"], ids=[slow])[0]) == [slow, "b/paired.py::body[case=cmt]"]
    # a --select clause still narrows within the given paths for the tests that are not named
    assert ids(select(project, paths=["a"], select=["backend=oss"], ids=[plain])[0]) == [
        "a/x.test::x[backend=oss]",
        plain,
    ]
    # a directory PATH brings in the slow test under it -- no --select given here to narrow it out
    assert slow in ids(select(project, paths=["a"], ids=[plain])[0])


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"select": ["tag=smok"]}, "no scanned test has tag"),
        ({"select": ["backend=gcs"]}, "parameter 'backend' has no value"),
        ({"select": ["backnd=oss"]}, "unknown key 'backnd'"),
        ({"select": ["tag="]}, "write key=value"),
        ({"paths": ["test/nowhere"]}, "holds no test"),
        ({"ids": ["a/x.test::x"]}, "no such test"),
        ({"suites": ["nightly"]}, "no suite \\['nightly'\\]; known suites:"),
        ({"skip": ["tag=smok"]}, "no scanned test has tag"),
        ({"skip": ["backnd=oss"]}, "unknown key 'backnd'"),
        ({"select": ["tag=!a"]}, "write !tag=a"),
        ({"skip": ["tag=!a"]}, "write !tag=a"),
        ({"select": ["tag=a;;tag=mine"]}, "empty term"),
        ({"select": ["tag=a;"]}, "empty term"),
        ({"skip": ["tag=a;"]}, "empty term"),
    ],
)
def test_a_selection_that_can_never_match_is_an_error(project, kwargs, message):
    with pytest.raises(UsageError, match=message):
        select(project, **kwargs)


def test_a_negated_select_is_translated_into_a_skip_at_parse_time(project):
    xid = "a/x.test::x[backend=oss]"
    assert ids(select(project, select=["!tag=a"])[0]) == ids(select(project, skip=["tag=a"])[0])
    _, why_not = select(project, select=["!tag=a"])
    assert why_not[xid] == "--skip tag=a (from --select !tag=a)"


def test_a_skip_clause_may_use_bang_terms(project):
    # today's rule ("--skip !x is an error") is dropped: a skip clause may itself carry a negated term
    assert ids(select(project, skip=["!tag=a"])[0]) == ids(select(project, select=["tag=a"])[0])


def test_skip_combines_with_a_positive_select(project):
    # type=sqllogic gives x[oss], x[dbx], y.test_slow (already out, it is slow) and one; tag=a removes the x's
    assert ids(select(project, select=["type=sqllogic"], skip=["tag=a"])[0]) == ["b/one.test::one"]


def test_two_skips_union_their_removals(project):
    assert ids(select(project, skip=["tag=a", "tag=mine"])[0]) == ["b/one.test::one", "b/solo.py::solo"]


def test_skip_with_a_multi_valued_key_removes_any_match(project):
    assert ids(select(project, skip=["tag=a,mine"])[0]) == ["b/one.test::one", "b/solo.py::solo"]


def test_an_id_beats_a_skip(project):
    xid = "a/x.test::x[backend=oss]"
    assert xid in ids(select(project, skip=["tag=a"], ids=[xid])[0])
    assert xid in ids(select(project, skip=["backend=oss;tag=a"], ids=[xid])[0])


def test_why_line_for_a_command_line_skip(project):
    _, why_not = select(project, skip=["tag=a"])
    assert why_not["a/x.test::x[backend=oss]"] == "--skip tag=a"


def test_why_line_for_a_command_line_skip_clause(project):
    _, why_not = select(project, skip=["backend=oss;tag=a"])
    assert why_not["a/x.test::x[backend=oss]"] == "--skip backend=oss;tag=a"


def test_why_line_says_matches_no_select_clause_once(project):
    _, why_not = select(project, select=["tag=mine", "backend=oss"])
    assert why_not["b/solo.py::solo"] == "matches no --select clause"


@pytest.mark.parametrize("expr", ["tag=a", "type=sqllogic", "case=cmt,plain", "backend=oss", "tag=mine"])
def test_a_negated_select_and_the_equivalent_skip_choose_the_same_tests(project, expr):
    assert ids(select(project, select=[f"!{expr}"])[0]) == ids(select(project, skip=[expr])[0])


@pytest.mark.parametrize("expr", ["tag=a", "backend=oss", "case=cmt"])
def test_a_negated_select_and_skip_agree_on_top_of_a_positive_selection(project, expr):
    with_negation = select(project, paths=["a", "b"], select=[f"!{expr}"])[0]
    with_skip = select(project, paths=["a", "b"], skip=[expr])[0]
    assert ids(with_negation) == ids(with_skip)


def test_a_two_term_negation_only_clause_becomes_two_skips_by_de_morgan(project):
    # !A;!B (keep neither) == --skip A --skip B; a/y.test_slow also carries tag a (from the a/** rule),
    # so it is dropped the same ordinary way, not by any slow-specific rule
    negation, why_not = select(project, select=["!tag=a;!tag=mine"])
    two_skips, _ = select(project, skip=["tag=a", "tag=mine"])
    assert ids(negation) == ids(two_skips) == ["b/one.test::one", "b/solo.py::solo"]
    assert why_not["a/y.test_slow::y"] == "--skip tag=a (from --select !tag=a;!tag=mine)"
    assert why_not["a/x.test::x[backend=oss]"] == "--skip tag=a (from --select !tag=a;!tag=mine)"
    assert why_not["b/paired.py::body[case=cmt]"] == "--skip tag=mine (from --select !tag=a;!tag=mine)"


def test_a_mixed_clause_is_an_ordinary_positive_selection(project):
    # backend=oss AND NOT tag=mine: a positive clause, not a translation -- it narrows the base to just
    # the matching tests, unlike a lone --skip of the same clause, which would only subtract from it
    mixed = select(project, select=["backend=oss;!tag=mine"])[0]
    assert ids(mixed) == ["a/x.test::x[backend=oss]"]
    lone_skip = select(project, skip=["backend=oss;!tag=mine"])[0]
    assert (
        ids(lone_skip)
        == [
            "a/x.test::x[backend=dbx]",
            "a/y.test_slow::y",  # a --skip given: the base is `all`; y has no `backend` parameter, so the clause never holds for it
            "b/one.test::one",
            "b/paired.py::body[case=cmt]",
            "b/paired.py::body[case=plain]",
            "b/solo.py::solo",
        ]
    )


def test_a_bare_run_uses_a_project_default_suite(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, match
                project = Project(
                    suites={"default": {"paths": ["test/keep"], "skip": ["tag=spark"]}},
                    rules=[match("keep/spark.test", tags=["spark"]), match("keep/other.test", tags=["quiet"])],
                )
            """,
            "test/keep/spark.test": "",
            "test/keep/other.test": "",
            "test/outside/far.test": "",
        },
    )
    bare, why_not = apply(tests, Selection(), proj)
    assert ids(bare) == ["keep/other.test::other"]  # the default suite's own skip already removed spark
    assert why_not["keep/spark.test::spark"] == "not in suite 'default'"  # the suite's own skip stays internal
    assert why_not["outside/far.test::far"] == "not in suite 'default'"


def test_a_skip_given_alone_replaces_the_default_entirely(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, match
                project = Project(
                    suites={"default": {"paths": ["test/keep"], "skip": ["tag=spark"]}},
                    rules=[match("keep/spark.test", tags=["spark"]), match("keep/other.test", tags=["quiet"])],
                )
            """,
            "test/keep/spark.test": "",
            "test/keep/other.test": "",
            "test/outside/far.test": "",
        },
    )
    # a --skip on the command line, even alone, means the base is `all`, not the suite `default` -- so
    # spark.test (which the default suite's own skip would drop) and far.test (outside its paths) both run
    selected, why_not = apply(tests, Selection(skip=["tag=quiet"]), proj)
    assert set(ids(selected)) == {"keep/spark.test::spark", "outside/far.test::far"}
    assert why_not == {"keep/other.test::other": "--skip tag=quiet"}


def test_a_lone_skip_runs_everything_else_including_slow(project):
    assert "a/y.test_slow::y" in ids(select(project, skip=["tag=mine"])[0])


def test_a_suites_skip_applies_on_top_of_its_own_selection(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, match
                project = Project(
                    suites={
                        "default": {"paths": ["test/keep"], "skip": ["tag=spark"]},
                        "smoke": {"paths": ["test/keep"], "skip": ["tag=quiet"]},
                    },
                    rules=[
                        match("keep/spark.test", tags=["spark"]),
                        match("keep/quiet.test", tags=["quiet"]),
                        match("keep/plain.test", tags=["plain"]),
                    ],
                )
            """,
            "test/keep/spark.test": "",
            "test/keep/quiet.test": "",
            "test/keep/plain.test": "",
        },
    )
    via_suite, why_not = apply(tests, Selection(suites=["smoke"]), proj)
    assert ids(via_suite) == [
        "keep/plain.test::plain",
        "keep/spark.test::spark",
    ]  # smoke's skip removes quiet, not spark
    # smoke's own skip is internal to it -- why names the suite, not the skip that dropped the test from it
    assert why_not["keep/quiet.test::quiet"] == "not in suite 'smoke'"


def test_a_negation_only_select_alone_replaces_the_default_entirely(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, match
                project = Project(
                    suites={"default": {"paths": ["test/keep"]}},
                    rules=[match("keep/spark.test", tags=["spark"]), match("keep/other.test", tags=["quiet"])],
                )
            """,
            "test/keep/spark.test": "",
            "test/keep/other.test": "",
            "test/outside/far.test": "",
        },
    )
    # translated to a skip, it behaves like any --skip: the base is `all`, not the suite `default`
    selected, why_not = apply(tests, Selection(select=["!tag=spark"]), proj)
    assert set(ids(selected)) == {"keep/other.test::other", "outside/far.test::far"}
    assert why_not["keep/spark.test::spark"] == "--skip tag=spark (from --select !tag=spark)"


def test_a_suites_select_entries_are_ord(project):
    assert ids(select(project, suites=["combo"])[0]) == [
        "a/x.test::x[backend=oss]",
        "b/paired.py::body[case=cmt]",
        "b/paired.py::body[case=plain]",
    ]


def test_a_suites_skip_entry_may_be_a_clause(project):
    # trimmed scopes to test/a + test/b (directories, so the slow test under a is in scope), then drops
    # only tests that are both backend=oss and tag=a -- y has no backend parameter, so it is not dropped
    assert ids(select(project, suites=["trimmed"])[0]) == [
        "a/x.test::x[backend=dbx]",
        "a/y.test_slow::y",
        "b/one.test::one",
        "b/paired.py::body[case=cmt]",
        "b/paired.py::body[case=plain]",
        "b/solo.py::solo",
    ]


def test_suites_is_read_and_an_unrelated_top_level_name_is_ignored(tmp_path):
    tests, proj = tree(tmp_path, {"test/ducktest_conf.py": "SETS = {}", "test/a.test": ""})
    assert proj.suites == {}
    tests, proj = tree(
        tmp_path / "ok",
        {
            "test/ducktest_conf.py": 'from ducktest import Project\nproject = Project(suites={"x": {}})',
            "test/a.test": "",
        },
    )
    assert proj.suites == {"x": {}}


def test_a_project_suite_named_all_is_an_error(tmp_path):
    with pytest.raises(UsageError, match="may not define a suite named 'all'"):
        tree(
            tmp_path,
            {
                "test/ducktest_conf.py": 'from ducktest import Project\nproject = Project(suites={"all": {}})',
                "test/a.test": "",
            },
        )


def test_repeated_suite_unions_the_suites(project):
    combo = select(project, suites=["cmt", "smoke"])[0]
    assert set(ids(combo)) == set(ids(select(project, suites=["cmt"])[0])) | set(
        ids(select(project, suites=["smoke"])[0])
    )


@pytest.mark.parametrize("name", ["smoke", "cmt", "combo", "trimmed"])
def test_suite_option_equals_the_select_key(project, name):
    via_option = select(project, suites=[name])
    via_select = select(project, select=[f"suite={name}"])
    assert ids(via_option[0]) == ids(via_select[0])
    assert via_option[1] == via_select[1]


def test_suite_default_is_a_legal_term(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, match
                project = Project(
                    suites={"default": {"paths": ["test/keep"]}},
                    rules=[match("keep/a.test", tags=["a"]), match("outside/b.test", tags=["b"])],
                )
            """,
            "test/keep/a.test": "",
            "test/outside/b.test": "",
        },
    )
    assert ids(select((tests, proj), select=["suite=default"])[0]) == ["keep/a.test::a"]


def test_suite_term_combines_with_a_negated_suite_term_in_one_clause(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, match
                project = Project(
                    suites={"smoke": {"select": ["tag=smoke"]}, "databricks": {"select": ["tag=databricks"]}},
                    rules=[
                        match("a.test", tags=["smoke"]),
                        match("b.test", tags=["smoke", "databricks"]),
                        match("c.test", tags=["databricks"]),
                    ],
                )
            """,
            "test/a.test": "",
            "test/b.test": "",
            "test/c.test": "",
        },
    )
    selected, _ = select((tests, proj), select=["suite=smoke;!suite=databricks"])
    assert ids(selected) == ["a.test::a"]  # in smoke, not in databricks


def test_a_negated_suite_term_alone_replaces_the_default_entirely(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, match
                project = Project(
                    suites={"default": {"paths": ["test/keep"]}, "spark": {"select": ["tag=spark"]}},
                    rules=[match("keep/spark.test", tags=["spark"]), match("keep/other.test", tags=["quiet"])],
                )
            """,
            "test/keep/spark.test": "",
            "test/keep/other.test": "",
            "test/outside/far.test": "",
        },
    )
    selected, why_not = select((tests, proj), select=["!suite=spark"])
    assert set(ids(selected)) == {"keep/other.test::other", "outside/far.test::far"}  # base is `all`, not `default`
    assert why_not["keep/spark.test::spark"] == "--skip suite=spark (from --select !suite=spark)"


def test_why_line_for_a_suite_skip(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, match
                project = Project(
                    suites={"spark": {"select": ["tag=spark"]}},
                    rules=[match("a.test", tags=["spark"]), match("b.test", tags=["other"])],
                )
            """,
            "test/a.test": "",
            "test/b.test": "",
        },
    )
    _, why_not = select((tests, proj), skip=["suite=spark"])
    assert why_not["a.test::a"] == "--skip suite=spark"


def test_a_suite_may_refer_to_another_suite(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, match
                project = Project(
                    suites={
                        "core": {"select": ["tag=core"]},
                        "nightly": {"select": ["suite=core", "tag=extra"]},
                    },
                    rules=[match("a.test", tags=["core"]), match("b.test", tags=["extra"]), match("c.test", tags=["other"])],
                )
            """,
            "test/a.test": "",
            "test/b.test": "",
            "test/c.test": "",
        },
    )
    assert ids(select((tests, proj), suites=["nightly"])[0]) == ["a.test::a", "b.test::b"]


def test_a_suite_cycle_is_an_error_that_prints_it(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project
                project = Project(
                    suites={
                        "smoke": {"select": ["suite=nightly"]},
                        "nightly": {"select": ["suite=smoke"]},
                    },
                )
            """,
            "test/a.test": "",
        },
    )
    with pytest.raises(UsageError, match=r"smoke -> nightly -> smoke"):
        select((tests, proj), suites=["smoke"])


def test_a_test_in_two_suites_is_selected_once(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, match
                project = Project(
                    suites={"a": {"select": ["tag=x"]}, "b": {"select": ["tag=y"]}},
                    rules=[match("both.test", tags=["x", "y"])],
                )
            """,
            "test/both.test": "",
        },
    )
    selected, _ = select((tests, proj), suites=["a", "b"])
    assert ids(selected) == ["both.test::both"]  # not doubled


def test_a_suite_all_has_everything(project):
    tests, _ = project
    assert set(ids(select(project, suites=["all"])[0])) == set(ids(tests))


def test_suite_all_and_suite_default_are_ordinary_terms(project):
    assert ids(select(project, select=["suite=all"])[0]) == ids(select(project, suites=["all"])[0])
    assert ids(select(project, select=["suite=default"])[0]) == ids(select(project)[0])


def test_a_suite_may_refer_to_suite_all_or_suite_default(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project
                project = Project(suites={"everything": {"select": ["suite=all"]}, "base": {"select": ["suite=default"]}})
            """,
            "test/a.test": "",
            "test/b.test_slow": "",
        },
    )
    assert set(ids(select((tests, proj), suites=["everything"])[0])) == set(ids(tests))
    assert ids(select((tests, proj), suites=["base"])[0]) == ["a.test::a"]  # default: slow left out


def test_an_opt_in_tag_leaves_a_bare_run_and_suite_all_takes_it(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": "from ducktest import Project, match\nproject = Project(rules=[match('net.test', tags=['opt-in'])])",
            "test/a.test": "",
            "test/net.test": "",
            "test/manual.py": "from ducktest import test\n@test(tags=['opt-in'])\ndef manual(ctx): pass",
        },
    )
    selected, why_not = apply(tests, Selection(), proj)
    assert ids(selected) == ["a.test::a"]
    assert why_not == {
        "manual.py::manual": "not in suite 'default': tag opt-in",
        "net.test::net": "not in suite 'default': tag opt-in",
    }
    assert ids(apply(tests, Selection(suites=["all"]), proj)[0]) == ["a.test::a", "manual.py::manual", "net.test::net"]


def test_a_test_does_not_name_its_suites(tmp_path):
    with pytest.raises(UsageError, match=r"rule '\*\*': unknown option\(s\) \['suites'\]"):
        tree(
            tmp_path,
            {
                "test/ducktest_conf.py": "from ducktest import Project, default\nproject = Project(rules=[default(suites=['x'])])"
            },
        )
    body = "from ducktest import test\n@test(suites=['!default'])\ndef a(ctx): pass"
    with pytest.raises(UsageError, match=r"a.py::a: @test\(...\): unknown option\(s\) \['suites'\]"):
        tree(tmp_path / "2", {"test/a.py": body})


def test_a_suite_negates_in_its_select_and_its_skip(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest import Project, match
                project = Project(
                    suites={
                        "no_spark": {"select": ["!tag=spark"]},
                        "fast_only": {"skip": ["!tag=fast"]},
                        "fast_no_spark": {"select": ["tag=fast;!tag=spark"]},
                    },
                    rules=[match("s.test", tags=["spark", "fast"]), match("f.test", tags=["fast"])],
                )
            """,
            "test/s.test": "",
            "test/f.test": "",
            "test/plain.test": "",
        },
    )
    assert ids(apply(tests, Selection(suites=["no_spark"]), proj)[0]) == ["f.test::f", "plain.test::plain"]
    assert ids(apply(tests, Selection(suites=["fast_only"]), proj)[0]) == ["f.test::f", "s.test::s"]
    assert ids(apply(tests, Selection(suites=["fast_no_spark"]), proj)[0]) == ["f.test::f"]
    with pytest.raises(
        UsageError, match=r"no suite \['nope'\]; known suites: \['fast_no_spark', 'fast_only', 'no_spark'\]"
    ):
        apply(tests, Selection(suites=["nope"]), proj)


def test_a_directory_path_brings_in_a_slow_test(project):
    assert "a/y.test_slow::y" in ids(select(project, paths=["a"])[0])


def test_path_selects_a_directory_and_its_glob_form_agree(project):
    # a directory PATH and a `path=` glob agree, and both bring in the slow test under them
    directory = ids(select(project, select=["path=a"])[0])
    glob_form = ids(select(project, select=["path=a/**"])[0])
    assert directory == glob_form == ["a/x.test::x[backend=oss]", "a/x.test::x[backend=dbx]", "a/y.test_slow::y"]


def test_path_in_a_clause(project):
    assert ids(select(project, select=["path=a;backend=oss"])[0]) == ["a/x.test::x[backend=oss]"]


def test_path_with_no_match_is_an_error(project):
    with pytest.raises(UsageError, match="matches no scanned test"):
        select(project, select=["path=nowhere/at/all"])


def test_a_path_term_brings_in_a_slow_test(project):
    assert ids(select(project, select=["path=a/y.test_slow"])[0]) == ["a/y.test_slow::y"]


def test_a_term_with_no_key_is_a_path_term(project):
    assert ids(select(project, select=["a"])[0]) == ids(select(project, select=["path=a"])[0])
    assert ids(select(project, select=["!a"])[0]) == ids(select(project, select=["!path=a"])[0])
    # a bare path term sits in a clause like any other, and brings in the slow test under it
    assert ids(select(project, select=["type=sqllogic;a"])[0]) == [
        "a/x.test::x[backend=oss]",
        "a/x.test::x[backend=dbx]",
        "a/y.test_slow::y",
    ]


def test_a_bare_path_with_an_embedded_equals_is_an_error(project):
    with pytest.raises(UsageError, match="a bare path cannot contain '='"):
        select(project, select=["a/x.test=oss"])


def test_a_command_line_path_term_is_relative_to_the_working_directory(tmp_path):
    tree(tmp_path, {"test/a/x.test": "", "test/b/y.test": ""})
    root, sub = str(tmp_path / "test"), str(tmp_path / "test" / "a")
    tests_from_root, proj_from_root = scan(root, working_dir=root)
    tests_from_sub, proj_from_sub = scan(root, working_dir=sub)
    from_root = apply(tests_from_root, Selection(select=["a"]), proj_from_root, working_dir=root)[0]
    from_sub = apply(tests_from_sub, Selection(select=["."]), proj_from_sub, working_dir=sub)[0]
    assert ids(from_root) == ids(from_sub) == ["a/x.test::x"]


@pytest.mark.parametrize(
    "cwd, path, plain",
    [
        ("test", ".", ["a", "b"]),
        ("test/a", ".", ["a"]),
        ("test/a", "./x.test", ["a/x.test"]),
        ("test", "./a", ["a"]),
        ("test/b", "..", ["a", "b"]),
        ("test/b", "../a/", ["a"]),
    ],
)
def test_a_positional_path_names_what_its_plain_relative_form_names(tmp_path, cwd, path, plain):
    tree(tmp_path, {"test/a/x.test": "", "test/a/y.test": "", "test/b/z.test": ""})
    root, working_dir = str(tmp_path / "test"), str(tmp_path / cwd)
    tests, proj = scan(root, working_dir=working_dir)
    given = ids(apply(tests, Selection(paths=[path]), proj, working_dir=working_dir)[0])
    absolute = ids(apply(tests, Selection(paths=[str(tmp_path / cwd / path)]), proj, working_dir=working_dir)[0])
    from_root, root_proj = scan(root, working_dir=root)
    expected = ids(apply(from_root, Selection(paths=plain), root_proj, working_dir=root)[0])
    assert given == absolute == expected and given


def test_a_path_term_outside_the_project_root_is_an_error(tmp_path):
    tests, proj = tree(tmp_path, {"test/a.test": ""})
    with pytest.raises(UsageError, match="is outside the project root"):
        apply(tests, Selection(select=["../elsewhere"]), proj, working_dir=str(tmp_path / "test"))


def test_a_suites_path_term_is_not_affected_by_the_working_directory(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": 'from ducktest import Project\nproject = Project(suites={"onlya": {"select": ["path=a"]}})',
            "test/a/x.test": "",
            "test/b/y.test": "",
        },
    )
    from_root = apply(tests, Selection(suites=["onlya"]), proj, working_dir=str(tmp_path / "test"))[0]
    from_sub = apply(tests, Selection(suites=["onlya"]), proj, working_dir=str(tmp_path / "test" / "b"))[0]
    assert ids(from_root) == ids(from_sub) == ["a/x.test::x"]


def test_docstring_examples(dnf_project):
    all_ids = ids(dnf_project[0])

    python_on_s3, _ = select(dnf_project, select=["type=python;storage=s3"])
    assert ids(python_on_s3) == [i for i in all_ids if "py/d.py" in i and "storage=s3" in i]

    python_or_s3, _ = select(dnf_project, select=["type=python", "storage=s3"])
    assert set(ids(python_or_s3)) == {i for i in all_ids if "py/d.py" in i or "storage=s3" in i}

    dropped_both, _ = select(dnf_project, skip=["backend=oss;storage=s3"])
    assert set(ids(dropped_both)) == set(all_ids) - {i for i in all_ids if "backend=oss" in i and "storage=s3" in i}

    without_spark, _ = select(dnf_project, select=["!tag=spark"])
    same_as_skip, _ = select(dnf_project, skip=["tag=spark"])
    assert ids(without_spark) == ids(same_as_skip) == [i for i in all_ids if "spark/e.test" not in i]


def test_a_star_stays_inside_one_directory_and_two_stars_cross(tmp_path):
    tests, _ = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": "from ducktest import Project, match\nproject = Project(rules=[\n    match('*.test', tags=['top']),\n    match('**/*.test', tags=['any']),\n    match('sub/**', tags=['below']),\n])",
            "test/x.test": "",
            "test/sub/deep/y.test": "",
        },
    )
    tags = {c.definition.path: c.definition.tags for c in tests}
    assert tags == {"test/x.test": ["top", "any"], "test/sub/deep/y.test": ["any", "below"]}


def test_a_skip_while_a_test_file_is_imported_is_a_usage_error_that_says_where_to_skip(tmp_path):
    with pytest.raises(
        UsageError, match=r"early\.py called skip\('no CLI'\) while it was imported; skip inside the body"
    ):
        tree(tmp_path, {"test/early.py": "from ducktest import skip, test\nskip('no CLI')\n@test()\ndef t(ctx): pass"})


def test_only_a_file_with_test_bodies_is_imported_and_a_broken_one_says_which(tmp_path):
    tests, _ = tree(
        tmp_path, {"test/a.test": "", "test/helper.py": "raise RuntimeError('a helper must not run at scan')"}
    )
    assert ids(tests) == ["a.test::a"]
    with pytest.raises(UsageError, match=r"broken\.py could not be imported: ModuleNotFoundError"):
        tree(
            tmp_path / "2",
            {"test/broken.py": "import no_such_module\nfrom ducktest import test\n@test()\ndef t(ctx): pass"},
        )
    with pytest.raises(UsageError, match=r"unknown option\(s\) \['cases'\]"):
        tree(tmp_path / "3", {"test/old.py": "from ducktest import test\n@test(cases={'a': {}})\ndef t(ctx): pass"})


def test_a_test_can_import_a_helper_beside_it(tmp_path):
    tests, _ = tree(
        tmp_path,
        {
            "test/helper.py": "X = 1",
            "test/t.py": "from helper import X\nfrom ducktest import test\n@test()\ndef t(ctx): pass",
        },
    )
    assert ids(tests) == ["t.py::t"]


def test_two_tests_of_one_file_name_get_their_own_directory(tmp_path):
    body = "from ducktest import test\n@test()\ndef t(ctx): pass"
    tests, _ = tree(tmp_path, {"test/a/same.py": body, "test/b/same.py": body})
    assert len({c.dir_name for c in tests}) == 2


def test_a_path_names_its_paired_file_and_nothing_wider(project):
    assert ids(select(project, paths=["b/paired.test"])[0]) == [
        "b/paired.py::body[case=cmt]",
        "b/paired.py::body[case=plain]",
    ]
    assert ids(select(project, paths=["a/y.test_slow"])[0]) == ["a/y.test_slow::y"]  # named exactly: asked for
    with pytest.raises(UsageError, match=r"parameter 'case' has no value \['cmtt'\]"):
        select(project, select=["case=cmtt"])
    with pytest.raises(UsageError, match="unknown key 'cse'; keys are tag, type, path, suite, backend, case"):
        select(project, select=["cse=cmt"])


def test_a_pattern_that_matches_a_test_file_reaches_the_body_paired_with_it_and_not_the_other_way(tmp_path):
    conf = """
        from ducktest import Project, match
        project = Project(rules=[match("sql/*.test", tags=["by_test"]), match("sql/*.py", tags=["by_py"])])
    """
    tests, _ = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": conf,
            "test/sql/pp.test": "",
            "test/sql/pp.py": "from ducktest import test\n@test()\ndef pp(ctx): ctx.run_paired()\n",
            "test/sql/solo.test": "",
        },
    )
    tags = {c.name: [t for t in c.definition.tags if t.startswith("by_")] for c in tests}
    assert tags == {"sql/pp.py::pp": ["by_test", "by_py"], "sql/solo.test::solo": ["by_test"]}


def test_retries_is_a_rule_field_and_a_whole_number(tmp_path):
    files = {
        "test/ducktest_conf.py": "from ducktest import Project, match\nproject = Project(rules=[match('net.test', retries=2)])"
    }
    tests, _ = tree(tmp_path, {**files, "test/net.test": "", "test/plain.test": ""})
    assert [(c.definition.name, c.definition.retries) for c in tests] == [("net", 2), ("plain", 0)]
    with pytest.raises(UsageError, match="retries=-1; it is a whole number, 0 or more"):
        tree(
            tmp_path,
            {
                "test/ducktest_conf.py": "from ducktest import Project, default\nproject = Project(rules=[default(retries=-1)])"
            },
        )


BIND_CONF = """
    from ducktest import Project, StorageDef, default

    project = Project(
        storages=[StorageDef("a", "/tmp/a"), StorageDef("b", "/tmp/b")],
        rules=[default(storage="a", temp="a")],
    )
"""


def test_a_tests_own_binding_of_a_kind_replaces_the_rules(tmp_path):
    tests, _ = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": BIND_CONF,
            "test/ruled.test": "",
            "test/own.py": "from ducktest import test\n@test(temp='b')\ndef own(ctx): pass",
        },
    )
    by_name = {c.definition.name: c for c in tests}
    assert [d.name for d, _ in by_name["ruled"].inputs] == ["a"]
    assert [d.name for d, _ in by_name["own"].inputs] == ["a", "b"]
    assert by_name["own"].definition.why == [
        "storage='a'  <- ducktest_conf.py: default",
        "temp='a'  <- ducktest_conf.py: default",
        "temp='b'  <- @test",
    ]
    assert resolve(by_name["own"]).roots["temp_dir_root"].value == "/tmp/b/temp/<run_token>"
    assert by_name["own"].bind_lines() == [
        "bind: storage=a  <- ducktest_conf.py: default",
        "bind: temp=b  <- @test",
    ]


MATRIX_BIND_CONF = """
    from ducktest import Project, StorageDef, default, match

    project = Project(
        storages=[StorageDef("a", "/tmp/a"), StorageDef("b", "/tmp/b")],
        matrix={"store": {"b": {"storage": "b"}}},
        rules=RULES_HERE,
    )
"""


def test_a_matrix_value_replaces_what_a_default_binds_and_says_so(tmp_path):
    conf = MATRIX_BIND_CONF.replace("RULES_HERE", '[default(storage="a", matrix=["store"])]')
    tests, _ = tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})
    assert [d.name for d, _ in tests[0].inputs] == ["b"]
    assert tests[0].bind_lines() == ["bind: storage=b  <- matrix store=b"]
    assert tests[0].why == [
        "storage='a'  <- ducktest_conf.py: default",
        "matrix=['store']  <- ducktest_conf.py: default",
        "storage='b'  <- matrix store=b",
    ]


@pytest.mark.parametrize(
    "rules, own, source",
    [
        ('[default(matrix=["store"]), match("t.py", storage="a")]', "", "ducktest_conf.py: match 't.py'"),
        ('[default(storage="a", matrix=["store"])]', "storage='a'", "@test"),
    ],
)
def test_a_kind_a_matrix_value_and_a_match_or_test_both_bind_is_an_error(tmp_path, rules, own, source):
    body = f"from ducktest import test\n@test({own})\ndef t(ctx): pass"
    message = f"t.py::t[store=b]: storage= is 'a' by {source} and 'b' by matrix store=b; bind it in one place"
    with pytest.raises(UsageError, match=re.escape(message)):
        tree(tmp_path, {"test/ducktest_conf.py": MATRIX_BIND_CONF.replace("RULES_HERE", rules), "test/t.py": body})


def test_a_kind_two_matrix_values_of_one_test_both_bind_is_an_error(tmp_path):
    conf = """
        from ducktest import Project, StorageDef, default

        project = Project(
            storages=[StorageDef("s3", "s3://b"), StorageDef("proxied", "/tmp/p")],
            matrix={
                "storage": {"s3": "s3"},
                "via": {"direct": {}, "reverse-proxy": {"storage": "proxied"}},
            },
            rules=[default(matrix=["storage", "via"])],
        )
    """
    message = (
        "t.test::t[storage=s3,via=reverse-proxy]: storage= is given by matrix storage=s3 and by matrix "
        "via=reverse-proxy; bind it in one place"
    )
    with pytest.raises(UsageError, match=re.escape(message)):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})


def test_a_value_a_matrix_value_and_a_local_value_both_give_is_a_scan_error(tmp_path):
    conf = """
        from ducktest import Project, default

        project = Project(matrix={"backend": {"s3": {}}}, rules=[default(matrix=["backend"])])
    """
    body = "from ducktest import test\n@test(matrix=[{'case': {'wide': {'backend': 'x'}}}])\ndef t(ctx): pass"
    message = "t.py::t[backend=s3,case=wide]: 'backend' is given by both matrix backend=s3 and matrix case=wide"
    with pytest.raises(UsageError, match=re.escape(message)):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.py": body})


def test_a_binding_names_a_declared_storage_credential_or_service(tmp_path):
    with pytest.raises(UsageError, match=r"own.py:2: storage='c' is not declared; the declared ones are 'a', 'b'"):
        tree(
            tmp_path,
            {
                "test/ducktest_conf.py": BIND_CONF,
                "test/own.py": "from ducktest import test\n@test(storage='c')\ndef own(ctx): pass",
            },
        )


def test_a_binding_names_one_of_its_own_kind(tmp_path):
    conf = BIND_CONF.replace('rules=[default(storage="a", temp="a")]', 'rules=[default(credential="a")]')
    with pytest.raises(UsageError, match="ducktest_conf.py:6: credential='a' names a storage, not a credential"):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})


def test_bind_is_not_a_keyword_any_more_and_the_error_names_the_ones_there_are(tmp_path):
    with pytest.raises(
        UsageError,
        match=r"unknown option\(s\) \['bind'\]; it takes .*'storage', 'data', 'temp', 'credential', 'service'",
    ):
        tree(tmp_path, {"test/ducktest_conf.py": BIND_CONF.replace('storage="a"', 'bind={"storage": "a"}')})
    body = "from ducktest import test\n@test(bind={'storage': 'b'})\ndef own(ctx): pass"
    with pytest.raises(
        UsageError, match=r"own.py::own: @test\(...\): unknown option\(s\) \['bind'\]; it takes .*'storage'"
    ):
        tree(tmp_path / "2", {"test/ducktest_conf.py": BIND_CONF, "test/own.py": body})


def test_a_matrix_value_that_unsets_a_credentials_names_does_not_hide_it(tmp_path, monkeypatch):
    conf = """
        from ducktest import Project, S3CredentialDef, StorageDef, default

        project = Project(
            storages=[StorageDef("bucket", "s3://bucket")],
            credentials=[S3CredentialDef.from_aws_env("key", scopes=["bucket"])],
            matrix={"auth": {"key": {}, "none": {"process_env": {"AWS_ACCESS_KEY_ID": None}}}},
            rules=[default(storage="bucket", matrix=["auth"])],
        )
    """
    tests, _ = tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})
    assert [[d.name for d, _ in c.inputs] for c in tests] == [["bucket", "key"], ["bucket", "key"]]


CREDENTIAL_CONF = """
    from ducktest import Project, default, match
    from ducktest.resources import BootedEngineDef, S3CredentialDef, StorageDef, from_env

    KEYS = from_env({"DT_KEY": "key_id", "DT_SECRET": "secret"})
    project = Project(
        storages=[StorageDef("lake", "s3://lake"), StorageDef("blobs", "az://c/p"), StorageDef("other", "s3://other")],
        credentials=[S3CredentialDef("keys_a", sources=[KEYS], scopes={scopes_a}), S3CredentialDef("keys_b", sources=[KEYS], scopes=["other"])],
        engines={engines},
        rules=[default({bind}){rules}],
    )
"""


def credential_conf(scopes_a, bind="", engines="[]", rules=""):
    conf = CREDENTIAL_CONF
    for key, value in (("{scopes_a}", scopes_a), ("{bind}", bind), ("{engines}", engines), ("{rules}", rules)):
        conf = conf.replace(key, value)
    return conf


def test_binding_the_storage_renders_a_secret_and_binding_only_the_credential_gives_the_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DT_KEY", "k-value")
    monkeypatch.setenv("DT_SECRET", "s-value")
    conf = credential_conf(
        '["lake"]', rules=', match("stored.py", storage="lake"), match("keys_only.py", credential="keys_a")'
    )
    body = "from ducktest import test\n@test()\ndef {name}(ctx): pass\n"
    tests, project = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": conf,
            "test/stored.py": body.format(name="stored"),
            "test/keys_only.py": body.format(name="keys_only"),
        },
    )
    by_name = {t.definition.name: t for t in tests}
    stored, keys_only = resolve(by_name["stored"]), resolve(by_name["keys_only"])
    assert stored.secret_sql and not keys_only.secret_sql
    assert keys_only.environ({})["AWS_ACCESS_KEY_ID"] == "k-value"
    (engine,) = leases.bind(by_name["keys_only"], keys_only, project)
    assert [d.kind for entry in engine.inputs for d in entry] == ["credential"], "the engine renders no secret"


def test_a_credential_an_engine_of_the_test_cannot_render_is_a_scan_error(tmp_path):
    conf = """
        from dataclasses import dataclass
        from ducktest import Project, default
        from ducktest.resources import BootedEngineDef, CredentialDef, Field, StorageDef

        @dataclass(eq=False)
        class TokenDef(CredentialDef):
            schemes = ("s3",)
            fields = {"token": Field(required=True, secret=True)}
            env = {"token": ("DT_TOKEN",)}

        project = Project(
            storages=[StorageDef("lake", "s3://lake")],
            credentials=[TokenDef("tok", scopes=["lake"])],
            engines=[BootedEngineDef("spark", boot=lambda log: None, dialect="spark")],
            rules=[default(storage="lake")],
        )
    """
    body = "from ducktest import test\n@test(engines=['spark'])\ndef t(ctx): pass\n"
    message = (
        "t.py::t: engine 'duckdb' cannot render TokenDef 'tok' (duckdb renders: S3CredentialDef, AzureCredentialDef)"
    )
    with pytest.raises(UsageError, match=re.escape(message)):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.py": body})


def test_two_credentials_of_one_type_on_one_booted_engine_are_a_scan_error(tmp_path):
    conf = credential_conf(
        '["lake"]',
        engines='[BootedEngineDef("spark", boot=lambda log: None, dialect="spark")]',
        rules=', match("a.py", storage="lake"), match("b.py", storage="other")',
    )
    body = "from ducktest import test\n@test(engines=['spark'])\ndef t(ctx): pass\n"
    message = "engine 'spark': its tests hold S3CredentialDef 'keys_a' and 'keys_b' by env, and it boots once per run"
    with pytest.raises(UsageError, match=re.escape(message)):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/a.py": body, "test/b.py": body})


@pytest.mark.parametrize(
    "scopes_a, bind, message",
    [
        (
            '["blobs"]',
            'storage="blobs"',
            "S3CredentialDef('keys_a') is scoped to storage 'blobs' (az://c/p): an S3CredentialDef is for s3 storages only",
        ),
        (
            '["lake"]',
            'storage="lake", credential="keys_b"',
            "t.test::t: holds S3CredentialDef 'keys_b' and 'keys_a' by AWS_* env; one process has one set. "
            "Several credentials of one type in one test are not supported yet",
        ),
    ],
)
def test_a_credential_a_test_cannot_be_given_is_a_scan_error(tmp_path, scopes_a, bind, message):
    conf = credential_conf(scopes_a, bind)
    with pytest.raises(UsageError, match=re.escape(message)):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})


def test_an_engine_is_declared_among_the_engines_not_the_services(tmp_path):
    engine = "from ducktest import EngineDef\nE = EngineDef('fake', 'fake:1', lambda log: None)\n"
    _, project = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": engine + "from ducktest import Project\nproject = Project(engines=[E])",
            "test/a.test": "",
        },
    )
    assert (list(project.engines), list(project.services)) == (["fake"], [])
    with pytest.raises(
        UsageError, match=r"EngineDef\('fake'\) is an engine; declare engines in Project\(engines=...\)"
    ):
        tree(
            tmp_path / "2",
            {
                "test/ducktest_conf.py": engine + "from ducktest import Project\nproject = Project(services=[E])",
                "test/a.test": "",
            },
        )


def test_a_non_engine_service_is_declared_among_the_services_not_the_engines(tmp_path):
    service = "from ducktest.services.http import HttpServerDef\nS = HttpServerDef('fake')\n"
    with pytest.raises(
        UsageError, match=r"HttpServerDef\('fake'\) is not an engine; declare services in Project\(services=...\)"
    ):
        tree(
            tmp_path,
            {
                "test/ducktest_conf.py": service + "from ducktest import Project\nproject = Project(engines=[S])",
                "test/a.test": "",
            },
        )


# -----------------------------------------------------------------------------
# pythonpath: the repo's helper code
#


@pytest.fixture
def clean_path(monkeypatch):
    """What a scan puts on sys.path, and the helpers it imports, go with the test."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))


HELPER_BODY = """
    from ducktest import test
    from pp_helper import ANSWER

    @test()
    def uses_helper(ctx):
        assert ANSWER == 42
"""

PYPROJECT = """
    [tool.ducktest]
    pythonpath = ["scripts"]
"""


def test_pyproject_pythonpath_is_on_the_path_before_any_conf_runs(tmp_path, clean_path):
    (tmp_path / ".git").mkdir()
    files = {
        "pyproject.toml": PYPROJECT,
        "scripts/pp_kinds.py": "KIND = 'catalog'",
        "test/ducktest_conf.py": "from pp_kinds import KIND",
        "test/a.test": "",
    }
    tests, project = tree(tmp_path, files)
    assert project.pythonpath == (str(tmp_path / "scripts"),)
    assert sys.path[0] == str(tmp_path / "scripts")


def test_project_pythonpath_adds_helpers_from_that_point_on_for_the_file_and_every_body(tmp_path, clean_path):
    (tmp_path / ".git").mkdir()
    files = {
        "pyproject.toml": PYPROJECT,
        "scripts/pp_kinds.py": "KIND = 'catalog'",
        "test/ducktest_conf.py": """
            from ducktest import Project
            from pp_kinds import KIND
            project = Project(pythonpath=["py"])
            from pp_helper import ANSWER
        """,
        "test/py/pp_helper.py": "ANSWER = 42",
        "test/a/body.py": HELPER_BODY,
    }
    tests, project = tree(tmp_path, files)
    assert ids(tests) == ["a/body.py::uses_helper"]
    assert project.pythonpath == (str(tmp_path / "scripts"), str(tmp_path / "test" / "py"))


def test_without_pythonpath_a_helper_outside_the_body_s_directory_is_not_found(tmp_path, clean_path):
    with pytest.raises(UsageError, match=r"body\.py could not be imported: ModuleNotFoundError"):
        tree(
            tmp_path,
            {"test/ducktest_conf.py": "", "test/py/pp_helper.py": "ANSWER = 42", "test/a/body.py": HELPER_BODY},
        )


@pytest.mark.parametrize(
    "conf, text",
    [
        (
            "project = Project(pythonpath=['nope'])",
            r"ducktest_conf\.py: Project\(\.\.\.\): pythonpath: 'nope' is not a directory",
        ),
        ("project = Project(pythonpath=['../..'])", r"pythonpath: '\.\./\.\.' is outside the repository"),
        ("project = Project(pythonpath='py')", r"pythonpath='py'; it is a list"),
        ("project = Project(pythonpath=[''])", r"pythonpath = \[''\]; it is a list of paths"),
    ],
)
def test_a_pythonpath_that_cannot_be_used_is_a_usage_error_naming_the_file(tmp_path, clean_path, conf, text):
    files = {"test/ducktest_conf.py": "from ducktest import Project\n" + conf, "test/py/x.py": "", "test/a/x.test": ""}
    with pytest.raises(UsageError, match=text):
        tree(tmp_path, files)


@pytest.mark.parametrize(
    "pyproject, text",
    [
        (
            '[tool.ducktest]\npythonpath = ["nope"]',
            r"pyproject\.toml: \[tool\.ducktest\] pythonpath: 'nope' is not a directory",
        ),
        ('[tool.ducktest]\npythonpath = "py"', r"pyproject\.toml: \[tool\.ducktest\] pythonpath = 'py'; it is a list"),
        ("[tool.ducktest\n", r"pyproject\.toml: "),
    ],
)
def test_a_pyproject_pythonpath_that_cannot_be_used_is_a_usage_error_naming_the_file(
    tmp_path, clean_path, pyproject, text
):
    (tmp_path / ".git").mkdir()
    files = {"pyproject.toml": pyproject, "test/ducktest_conf.py": "", "test/a.test": ""}
    with pytest.raises(UsageError, match=text):
        tree(tmp_path, files)


def test_scan_env_says_where_helpers_come_from(tmp_path, clean_path, monkeypatch, capsys):
    from ducktest.cli import main

    (tmp_path / ".git").mkdir()
    tree(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "scripts/x.py": "",
            "test/ducktest_conf.py": "from ducktest import Project\nproject = Project(pythonpath=['py'])",
            "test/py/x.py": "",
            "test/a.test": "",
        },
    )
    monkeypatch.chdir(tmp_path)
    assert main(["scan", "--env"]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == [
        "pythonpath: ../scripts  <- pyproject.toml [tool.ducktest]",
        "pythonpath: py  <- ducktest_conf.py",
    ]


# -----------------------------------------------------------------------------
# One option set: a rule, @test and a matrix value take the same names
#

OPTIONS = (
    'requires=["json"], settings={"threads": 2}, init_sql="SELECT 1", process_env={"HTTP_PROXY": "p"}, '
    'values={"region": "eu"}, timeout=7, retries=1, mutex="m", tags=["t"]'
)


@pytest.mark.parametrize(
    "conf, body",
    [
        (f"from ducktest import Project\nproject = Project(rules=[default({OPTIONS})])", "@test()"),
        ("from ducktest import Project\nproject = Project()", f"@test({OPTIONS})"),
        (
            f'from ducktest import Project\nproject = Project(matrix={{"k": {{"v": dict({OPTIONS})}}}}, rules=[default(matrix=["k"])])',
            "@test()",
        ),
    ],
    ids=["rule", "@test", "matrix value"],
)
def test_every_option_works_on_a_rule_on_test_and_on_a_matrix_value(tmp_path, conf, body):
    head = "from ducktest import default\n"
    (test,) = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": head + conf,
            "test/t.py": f"from ducktest import test\n{body}\ndef t(ctx): pass\n",
        },
    )[0]
    env = resolve(test)
    assert env.extensions == ["json"]
    assert [e.value for e in env.init_sql] == ["SET threads = '2';", "SELECT 1;"]
    assert env.environ({}) == {"HTTP_PROXY": "p"}
    assert env.value_map["region"] == "eu"
    assert (test.timeout, test.retries, test.mutex) == (7, 1, ["m"])
    assert "t" in test.tags


def test_a_matrix_value_takes_no_matrix_and_an_unknown_slot_names_the_ones_there_are(tmp_path):
    where = r"matrix\['k'\]\['v'\]"
    for option, message in (
        ('{"matrix": ["k"]}', where + r": unknown option\(s\) \['matrix'\]; it takes \['tags', 'engines', 'mutex'"),
        ('{"roots": {"data_dir": "/d"}}', where + r": unknown option\(s\) \['roots'\]"),
        (
            '"default"',
            where
            + r" = 'default': a name or a declaration fills the slot the parameter is named after, and 'k' is no slot",
        ),
        ("7", where + r" = 7; a value is a dict of slots"),
    ):
        conf = f'from ducktest import Project\nproject = Project(matrix={{"k": {{"v": {option}}}}})\n'
        with pytest.raises(UsageError, match=message):
            tree(tmp_path, {"test/ducktest_conf.py": conf, "test/a.test": ""})


def test_a_string_value_fills_the_slot_the_parameter_is_named_after(tmp_path):
    conf = """
        from ducktest import Project, StorageDef, default

        project = Project(
            storages=[StorageDef("a", "/tmp/a"), StorageDef("b", "/tmp/b")],
            matrix={"storage": {"here": "a", "there": "b"}},
            rules=[default(matrix=["storage"])],
        )
    """
    tests, _ = tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})
    assert [(c.name, [d.name for d, _ in c.inputs]) for c in tests] == [
        ("t.test::t[storage=here]", ["a"]),
        ("t.test::t[storage=there]", ["b"]),
    ]


@pytest.mark.parametrize(
    "conf, body, message",
    [
        ("", "matrix='storage'", r"matrix='storage'; it is a list"),
        ("", "matrix=[{'tag': {'a': 1}}]", r"parameter 'tag'; a parameter name is a Python identifier and not one of"),
        ("", "matrix=[{'case': {}}]", r"parameter 'case' must be a non-empty dict of value name -> value"),
        ("", "matrix=[{'case': {'a b': 1}}]", r"value name 'a b'; a value name is a non-empty string without"),
        (
            "",
            "matrix=[{'case': {'a': 1}}, {'case': {'b': 2}}]",
            r"@test: matrix parameter 'case' is given by @test too; a parameter has one name",
        ),
        (
            'from ducktest import Project\nproject = Project(matrix={"storage": {"s3": {}}})',
            "matrix=[{'storage': {'a': 1}}]",
            r"t.py::t: @test defines parameter 'storage', which the project's matrix has; give it another name",
        ),
        (
            'from ducktest import Project\nproject = Project(matrix={"backend": {"oss": {}}}, rules=[default(matrix=["backend"])])',
            "matrix=[{'backend': {'a': 1}}]",
            r"@test: matrix parameter 'backend' is given by ducktest_conf.py: default too",
        ),
    ],
)
def test_a_matrix_that_cannot_make_cells_is_a_scan_error(tmp_path, conf, body, message):
    files = {
        "test/ducktest_conf.py": "from ducktest import default\n" + conf,
        "test/t.py": f"from ducktest import test\n@test({body})\ndef t(ctx): pass\n",
    }
    with pytest.raises(UsageError, match=message):
        tree(tmp_path, files)


OWN_BIND_CONF = """
    from ducktest import Project, S3CredentialDef, StorageDef, from_env

    KEYS = from_env({"DT_KEY": "key_id", "DT_SECRET": "secret"})
    project = Project(
        storages=[StorageDef("lake", "s3://lake")],
        credentials=[S3CredentialDef("keys", sources=[KEYS], scopes=["lake"])],
    )
"""


@pytest.mark.parametrize(
    "matrix, slot, value",
    [
        ("{'where': {'lake': {'storage': 'lake', 'credential': 'keys'}}}", "storage", "'lake'"),
        ("{'credential': {'k': 'keys'}}", "credential", "'keys'"),
    ],
)
def test_a_binding_slot_in_a_bodys_own_matrix_value_is_a_scan_error_not_a_plain_value(tmp_path, matrix, slot, value):
    body = f"from ducktest import test\n@test(matrix=[{matrix}])\ndef b(ctx): pass\n"
    message = (
        f"{slot!r} is a binding slot, and a body's own matrix value gives only plain values; bind the project's "
        f"matrix parameter (matrix=[{slot!r}]) or bind directly (@test({slot}={value}))"
    )
    with pytest.raises(UsageError, match=re.escape(message)):
        tree(tmp_path, {"test/ducktest_conf.py": OWN_BIND_CONF, "test/b.py": body})


def test_a_local_parameter_on_a_rule_runs_every_test_it_matches_over_its_values(tmp_path):
    conf = 'from ducktest import Project, match\nproject = Project(rules=[match("*.test", matrix=[{"size": {"small": 1, "big": 1000}}])])'
    tests, _ = tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})
    assert ids(tests) == ["t.test::t[size=small]", "t.test::t[size=big]"]
    assert resolve(tests[1]).value_map == {"size": 1000}


def test_init_sql_adds_up_from_the_rules_down_and_names_only_its_source(tmp_path):
    conf = 'from ducktest import Project, default, match\nproject = Project(rules=[default(init_sql="SELECT 1"), match("t.test", init_sql=["SELECT 2"])])'
    (test,) = tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})[0]
    assert [e.value for e in resolve(test).init_sql] == ["SELECT 1;", "SELECT 2;"]
    assert "init_sql  <- ducktest_conf.py: match 't.test'" in test.why


# -----------------------------------------------------------------------------
# match(requires_env=...): the .test files that gate themselves on a variable
#


def test_requires_env_selects_the_files_with_that_require_env_line_and_the_path_still_counts(tmp_path, caplog):
    conf = """
        from ducktest import Project, match
        project = Project(rules=[match("s3/**", requires_env="S3_TEST_SERVER", tags=["gated"])])
    """
    with caplog.at_level("INFO", logger="ducktest.conf"):
        tests, _ = tree(
            tmp_path,
            {
                "test/ducktest_conf.py": conf,
                "test/s3/bare.test": "require-env S3_TEST_SERVER\n\nquery I\nSELECT 1\n----\n1\n",
                "test/s3/valued.test": "# name: x\n\nrequire-env S3_TEST_SERVER 1\n",
                "test/s3/other.test": "require-env OTHER_NAME\n",
                "test/s3/plain.test": "SELECT 1;\n",
                "test/elsewhere/gated.test": "require-env S3_TEST_SERVER\n",
            },
        )
    gated = sorted(c.definition.name for c in tests if "gated" in c.tags)
    assert gated == ["bare", "valued"]
    assert [r.message for r in caplog.records].count(
        f"{tmp_path}/test/ducktest_conf.py: match(requires_env=...) is deprecated: a starting point for a repo "
        "that gates files with require-env; move to tags or paths"
    ) == 1
    (bare,) = [c for c in tests if c.definition.name == "bare"]
    assert bare.why == ["tags=['gated']  <- ducktest_conf.py: match 's3/**', requires_env='S3_TEST_SERVER'"]


def test_a_requires_env_rule_that_selects_nothing_is_a_typo(tmp_path):
    conf = "from ducktest import Project, match\nproject = Project(rules=[match('**', requires_env='NEVER_GATED', tags=['x'])])"
    with pytest.raises(UsageError, match=r"match '\*\*', requires_env='NEVER_GATED' matched no test"):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/a.test": "SELECT 1;\n"})


KEYS = "S3CredentialDef('keys', sources=[from_env({'DT_KEY': 'key_id', 'DT_SECRET': 'secret'})], scopes=[])"


# Each case: the name the rule and the file gate on, what else the rule says, whether the shell
# gives the credential's source (else it is pending at scan), and whether the scan warns.
@pytest.mark.parametrize(
    "name, rule, resolved, warned",
    [
        pytest.param("S3_TEST_SERVER", "", False, True, id="nothing-gives-it"),
        pytest.param("S3_TEST_SERVER", ", values={'S3_TEST_SERVER': '1'}", False, False, id="a-value"),
        pytest.param("S3_TEST_SERVER", ", process_env={'S3_TEST_SERVER': '1'}", False, False, id="process-env"),
        pytest.param("S3_TEST_SERVER", ", process_env={'S3_TEST_SERVER': None}", False, True, id="process-env-unset"),
        pytest.param("AWS_ACCESS_KEY_ID", ", credential=KEYS", True, False, id="a-credential"),
        pytest.param("AWS_ACCESS_KEY_ID", ", credential=KEYS", False, False, id="a-pending-credential"),
        pytest.param("S3_TEST_SERVER", ", credential=KEYS", True, True, id="a-credential-of-other-names"),
    ],
)
def test_a_requires_env_rule_warns_that_the_file_skips_when_nothing_gives_the_binary_the_name(
    tmp_path, caplog, monkeypatch, name, rule, resolved, warned
):
    if resolved:
        monkeypatch.setenv("DT_KEY", "k")
        monkeypatch.setenv("DT_SECRET", "s")
    else:
        monkeypatch.delenv("DT_KEY", raising=False)
    conf = f"""
        from ducktest import Project, S3CredentialDef, from_env, match

        KEYS = {KEYS}
        project = Project(credentials=[KEYS], rules=[match('**', requires_env={name!r}{rule})])
    """
    with caplog.at_level("WARNING", logger="ducktest.scan"):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/gated.test": f"require-env {name}\n"})
    message = (
        f"ducktest_conf.py: match(requires_env={name!r}) selects gated.test::gated but nothing gives "
        f"{name}; add values={{{name!r}: '1'}} or the test skips"
    )
    assert [r.message for r in caplog.records if r.name == "ducktest.scan"] == ([message] if warned else [])


# -----------------------------------------------------------------------------
# storage= gives values, data= and temp= give the roots
#

SLOTS_CONF = """
    from ducktest import Project, S3CredentialDef, StorageDef, default, from_env, match

    PRIVATE = StorageDef("private", "s3://private", endpoint="http://store:9000")
    WRITES = StorageDef("writes", "s3://writes", endpoint="http://store:9000")
    KEYS = S3CredentialDef("keys", sources=[from_env({"DT_KEY": "key_id", "DT_SECRET": "secret"})], scopes=[PRIVATE, WRITES])
    project = Project(
        storages=[PRIVATE, WRITES],
        credentials=[KEYS],
        matrix={"via": {"private": {"storage": PRIVATE, "temp": PRIVATE}}},
        rules=RULES_HERE,
    )
"""


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("DT_KEY", "k")
    monkeypatch.setenv("DT_SECRET", "s")


def slots_tree(tmp_path, rules, files=None):
    conf = SLOTS_CONF.replace("RULES_HERE", rules)
    return tree(tmp_path, {"test/ducktest_conf.py": conf, **(files or {"test/t.test": ""})})


def roots_of(test):
    return {name: (e.value, e.source) for name, e in resolve(test).roots.items()}


def test_storage_alone_gives_its_values_and_its_secret_and_leaves_both_roots_to_the_default(tmp_path, keys):
    (test,), project = slots_tree(tmp_path, '[default(storage="private")]')
    env = resolve(test, default=project.default_storage)
    assert env.value_map == {"S3_ENDPOINT": "store:9000", "S3_USE_SSL": "false"}
    assert [c.scope for c in env.credentials] == [["s3://private"]]
    assert {name: e.source for name, e in env.roots.items()} == {
        "temp_dir_root": "default storage",
        "data_dir": "default storage",
    }


def test_temp_alone_moves_the_temp_dir_and_neither_the_data_dir_nor_the_values(tmp_path, keys):
    (test,), _ = slots_tree(tmp_path, '[default(temp="writes")]')
    env = resolve(test)
    assert roots_of(test) == {"temp_dir_root": ("s3://writes/temp/<run_token>", "storage writes")}
    assert env.value_map == {}
    assert [c.scope for c in env.credentials] == [["s3://writes"]], "a storage temp= binds is in the secret's scope"


def test_data_alone_moves_the_data_dir_and_not_the_temp_dir_and_is_a_read_only_input(tmp_path):
    (test,), _ = slots_tree(tmp_path, '[default(data="private")]')
    assert roots_of(test) == {"data_dir": ("s3://private", "storage private")}
    assert [(d.name, mode) for d, mode in test.inputs] == [("private", "ro"), ("keys", "ro")]


def test_a_place_under_a_storage_is_a_root_its_storage_s_credential_covers(tmp_path, keys):
    rules = '[default(data=PRIVATE.at("fixtures/csv"), temp=WRITES.at("ducktest"))]'
    (test,), _ = slots_tree(tmp_path, rules)
    assert roots_of(test) == {
        "data_dir": ("s3://private/fixtures/csv", "storage private"),
        "temp_dir_root": ("s3://writes/ducktest/<run_token>", "storage writes"),
    }
    assert test.bind_lines() == [
        "bind: data=private.at('fixtures/csv')  <- ducktest_conf.py: default",
        "bind: temp=writes.at('ducktest')  <- ducktest_conf.py: default",
    ]
    (secret,) = resolve(test).secret_sql
    assert "SCOPE ['s3://private', 's3://writes']" in secret.value


def test_a_place_is_for_data_and_temp_only(tmp_path):
    with pytest.raises(UsageError, match=r"storage=private.at\('x'\); a place under a storage is for data= and temp="):
        slots_tree(tmp_path, '[default(storage=PRIVATE.at("x"))]')


def test_a_matrix_value_binds_the_slots_it_names_and_gives_way_to_a_default(tmp_path):
    tests, _ = slots_tree(tmp_path, '[default(temp=WRITES.at("ducktest"), matrix=["via"])]')
    (test,) = tests
    assert test.bind_lines() == [
        "bind: temp=private  <- matrix via=private",
        "bind: storage=private  <- matrix via=private",
    ]
    assert roots_of(test)["temp_dir_root"] == ("s3://private/temp/<run_token>", "storage private")


def test_temp_a_cell_and_a_match_both_bind_is_an_error(tmp_path):
    rules = '[default(matrix=["via"]), match("t.test", temp="writes")]'
    message = "t.test::t[via=private]: temp= is 'writes' by ducktest_conf.py: match 't.test' and 'private' by matrix via=private"
    with pytest.raises(UsageError, match=re.escape(message)):
        slots_tree(tmp_path, rules)


def test_a_test_s_own_temp_replaces_the_rules(tmp_path):
    body = "from ducktest import test\n@test(temp='writes')\ndef own(ctx): pass"
    (test,), _ = slots_tree(tmp_path, '[default(temp=PRIVATE.at("ducktest"))]', {"test/own.py": body})
    assert roots_of(test) == {"temp_dir_root": ("s3://writes/temp/<run_token>", "storage writes")}


def test_a_test_file_under_a_rule_and_a_body_under_test_with_the_same_slots_get_the_same_roots(
    tmp_path, capsys, monkeypatch
):
    from ducktest.cli import main

    rules = '[match("ruled.test", storage="private", data=PRIVATE.at("fixtures"), temp=WRITES.at("ducktest"))]'
    body = (
        "from ducktest import test\n"
        "@test(storage='private', data=PRIVATE.at('fixtures'), temp=WRITES.at('ducktest'))\n"
        "def own(ctx): pass"
    )
    conf = SLOTS_CONF.replace("RULES_HERE", rules)
    own = "from ducktest_conf_root import PRIVATE, WRITES\n" + body  # the loaded root conf
    tree(tmp_path, {"test/ducktest_conf.py": conf, "test/ruled.test": "", "test/own.py": own})
    monkeypatch.chdir(tmp_path)
    assert main(["scan", "--env", "test"]) == 0
    out = capsys.readouterr().out
    by_test, current = {}, None
    for line in out.splitlines():
        if not line.startswith(" "):
            current = by_test.setdefault(line.split("::")[0], [])
        elif line.lstrip().startswith(("root:", "bind:", "values:")):
            current.append(line.strip().split("  <- ")[0])
    assert by_test["own.py"] == by_test["ruled.test"]
    assert "root: temp_dir_root=s3://writes/ducktest/<run_token>" in by_test["own.py"]
    assert "root: data_dir=s3://private/fixtures" in by_test["own.py"]


def test_the_default_storage_has_no_places(tmp_path):
    conf = """
        from ducktest import Project, default, default_storage

        DEFAULT = default_storage()
        project = Project(storages=[DEFAULT], rules=[default(temp=DEFAULT.at("x"))])
    """
    with pytest.raises(UsageError, match=r"t.test::t: temp=default.at\('x'\) .* the default storage has no places"):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})
