"""ducktest5's scan, configuration files and selection, on a small tree made for each test."""

import re
import sys
import textwrap

import pytest

from ducktest5.binary import UsageError
from ducktest5.scan import scan
from ducktest5.select import Selection, apply
from ducktest5 import leases
from ducktest5.testenv import resolve

ROOT_CONF = """
    from ducktest5 import Variant, default, match

    AXES = {"backend": [Variant("oss", values={"catalog": "duck"}), Variant("dbx", tags=["needs-credentials"])]}
    SUITES = {
        "smoke": {"paths": ["test/a", "test/b/one.test"]},
        "cmt": {"select": ["params=cmt"]},
        "combo": {"select": ["tag=mine", "backend=oss"]},
        "trimmed": {"paths": ["test/a", "test/b"], "skip": ["backend=oss;tag=a"]},
    }
    RULES = [default(tags=["all"], values={"region": "eu"}), match("a/**", tags=["a"], timeout=30)]
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
                from ducktest5 import default, match
                RULES = [default(values={"region": "us"}), match("x.test", axes=["backend"], timeout=5)]
            """,
            "test/a/x.test": "",
            "test/a/y.test_slow": "",
            "test/b/one.test": "",
            "test/b/paired.test": "",
            "test/b/paired.py": """
                from ducktest5 import test
                @test(params={"cmt": {}, "plain": {}}, tags=["mine"])
                def body(ctx): ctx.run_paired()
            """,
            "test/b/solo.py": """
                from ducktest5 import test
                @test()
                def solo(ctx): pass
            """,
        },
    )


@pytest.fixture
def dnf_project(tmp_path):
    """A tree with two axes, a python test and a tagged one -- for the disjunctive-normal-form and
    `path=` selection rules, which need more shape than `project`'s single axis gives."""
    return tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest5 import Variant, default, match
                AXES = {"backend": [Variant("oss"), Variant("dbx")], "storage": [Variant("s3"), Variant("gcs")]}
                RULES = [default(axes=["backend", "storage"]), match("spark/**", tags=["spark"])]
            """,
            "test/sql/cloud/a.test": "",
            "test/sql/cloud/sub/b.test": "",
            "test/sql/local/c.test": "",
            "test/py/d.py": """
                from ducktest5 import test
                @test()
                def d(ctx): pass
            """,
            "test/spark/e.test": "",
        },
    )


def ids(tests):
    return [c.id for c in tests]


def test_files_become_tests_by_name_only_and_tests_multiply(project):
    tests, _ = project
    assert ids(tests) == [
        "a/x.test::x[backend=oss]",
        "a/x.test::x[backend=dbx]",
        "a/y.test_slow::y",
        "b/one.test::one",
        "b/paired.py::body[cmt]",
        "b/paired.py::body[plain]",
        "b/solo.py::solo",
    ]
    by_id = {c.id: c for c in tests}
    assert by_id["b/paired.py::body[cmt]"].definition.type == "paired"
    assert by_id["b/paired.py::body[cmt]"].definition.body_file == "test/b/paired.test"
    assert by_id["b/solo.py::solo"].definition.type == "python"


def test_an_id_is_the_same_from_every_working_directory(tmp_path, project):
    from_below = scan(str(tmp_path / "test"), working_dir=str(tmp_path / "test" / "a"))[0]
    assert ids(from_below) == ids(project[0])
    # the path the binary and the PATH arguments use does follow the working directory
    assert [c.definition.path for c in from_below][:2] == ["x.test", "x.test"]


def test_the_axis_parts_of_an_id_come_in_the_order_of_axes(tmp_path):
    tests, _ = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest5 import Variant, default
                AXES = {"backend": [Variant("oss")], "storage": [Variant("s3")]}
                RULES = [default(axes=["storage", "backend"])]
            """,
            "test/a.test": "",
        },
    )
    assert ids(tests) == ["a.test::a[backend=oss,storage=s3]"]


def test_a_test_directory_name_has_no_glob_or_hive_partition_characters(tmp_path):
    tests, _ = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest5 import Variant, default
                AXES = {"storage": [Variant("s3")]}
                RULES = [default(axes=["storage"])]
            """,
            "test/a.py": """
                from ducktest5 import test
                @test(params={"plain": {}})
                def body(ctx): pass
            """,
        },
    )
    assert re.fullmatch(r"body_plain,storage_s3_a\.py-[0-9a-f]{6}", tests[0].dir_name)


def test_rules_apply_from_the_root_down_and_the_test_itself_last(project):
    x = next(c for c in project[0] if c.id == "a/x.test::x[backend=oss]")
    assert x.definition.tags == ["all", "a"]
    # the deeper file replaced `region`, the variant added `catalog`, and the axis brings its own id
    assert resolve(x).value_map == {"backend": "oss", "region": "us", "catalog": "duck"}
    assert resolve(x).values["region"].source == "a/ducktest_conf.py: default"
    assert resolve(x).values["catalog"].source == "AXES backend=oss"
    assert x.definition.timeout == 5  # a single value: the last rule that set it
    assert any("a/ducktest_conf.py: match 'x.test'" in line for line in x.definition.why)
    paired = next(c for c in project[0] if c.id == "b/paired.py::body[cmt]")
    assert paired.definition.tags == ["all", "mine"]
    assert "needs-credentials" in next(c for c in project[0] if c.id.endswith("[backend=dbx]")).tags


def test_a_rule_that_matches_nothing_is_an_error(tmp_path):
    with pytest.raises(UsageError, match="matched no test"):
        tree(
            tmp_path,
            {
                "test/ducktest_conf.py": "from ducktest5 import match\nRULES = [match('s3/authentcated/**', tags=['x'])]",
                "test/s3/authenticated/a.test": "",
            },
        )


def test_an_unknown_rule_field_or_axis_is_an_error(tmp_path):
    with pytest.raises(UsageError, match=r"unknown option\(s\) \['needs_env'\]"):
        tree(
            tmp_path,
            {
                "test/ducktest_conf.py": "from ducktest5 import default\nRULES = [default(needs_env=['X'])]",
                "test/a.test": "",
            },
        )
    with pytest.raises(UsageError, match="takes part in axis 'nope'"):
        tree(
            tmp_path / "2",
            {
                "test/ducktest_conf.py": "from ducktest5 import default\nRULES = [default(axes=['nope'])]",
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
    # ';' inside one --select is and: type sqllogic and params cmt/plain both hold for nothing here
    assert ids(select(project, select=["type=paired;params=cmt"])[0]) == ["b/paired.py::body[cmt]"]
    assert ids(select(project, select=["params=cmt,plain"])[0]) == [
        "b/paired.py::body[cmt]",
        "b/paired.py::body[plain]",
    ]


def test_repeated_select_ors_its_clauses(project):
    # under the old AND-across-options rule this pair would be empty: no test is both backend=oss and tag=mine
    assert ids(select(project, select=["backend=oss", "tag=mine"])[0]) == [
        "a/x.test::x[backend=oss]",
        "b/paired.py::body[cmt]",
        "b/paired.py::body[plain]",
    ]


def test_a_suite_is_paths_and_selects_with_a_name(project):
    # the smoke suite's paths are directories/files, and a directory brings in the slow test under it
    assert ids(select(project, suites=["smoke"])[0]) == [
        "a/x.test::x[backend=oss]",
        "a/x.test::x[backend=dbx]",
        "a/y.test_slow::y",
        "b/one.test::one",
    ]
    assert ids(select(project, suites=["cmt"])[0]) == ["b/paired.py::body[cmt]"]


def test_an_id_is_taken_verbatim(project):
    assert ids(select(project, ids=["b/paired.py::body[plain]"])[0]) == ["b/paired.py::body[plain]"]


def test_an_id_is_added_to_what_paths_and_selects_give(project):
    slow, plain = "a/y.test_slow::y", "b/paired.py::body[plain]"
    selected, why_not = select(project, paths=["b/one.test"], ids=[plain])
    assert ids(selected) == ["b/one.test::one", plain]
    assert why_not["b/solo.py::solo"] == "outside the given paths"
    # no select, path or tag takes out a test that is named by id
    assert ids(select(project, select=["type=python"], ids=[slow])[0]) == [slow, "b/solo.py::solo"]
    assert ids(select(project, suites=["cmt"], ids=[slow])[0]) == [slow, "b/paired.py::body[cmt]"]
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
        ({"select": ["backend=gcs"]}, "axis 'backend' has no value"),
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


@pytest.mark.parametrize("expr", ["tag=a", "type=sqllogic", "params=cmt,plain", "backend=oss", "tag=mine"])
def test_a_negated_select_and_the_equivalent_skip_choose_the_same_tests(project, expr):
    assert ids(select(project, select=[f"!{expr}"])[0]) == ids(select(project, skip=[expr])[0])


@pytest.mark.parametrize("expr", ["tag=a", "backend=oss", "params=cmt"])
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
    assert why_not["b/paired.py::body[cmt]"] == "--skip tag=mine (from --select !tag=a;!tag=mine)"


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
            "a/y.test_slow::y",  # a --skip given: the base is `all`; y has no `backend` axis, so the clause never holds for it
            "b/one.test::one",
            "b/paired.py::body[cmt]",
            "b/paired.py::body[plain]",
            "b/solo.py::solo",
        ]
    )


def test_a_bare_run_uses_a_project_default_suite(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest5 import match
                SUITES = {"default": {"paths": ["test/keep"], "skip": ["tag=spark"]}}
                RULES = [match("keep/spark.test", tags=["spark"]), match("keep/other.test", tags=["quiet"])]
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
                from ducktest5 import match
                SUITES = {"default": {"paths": ["test/keep"], "skip": ["tag=spark"]}}
                RULES = [match("keep/spark.test", tags=["spark"]), match("keep/other.test", tags=["quiet"])]
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
                from ducktest5 import match
                SUITES = {
                    "default": {"paths": ["test/keep"], "skip": ["tag=spark"]},
                    "smoke": {"paths": ["test/keep"], "skip": ["tag=quiet"]},
                }
                RULES = [
                    match("keep/spark.test", tags=["spark"]),
                    match("keep/quiet.test", tags=["quiet"]),
                    match("keep/plain.test", tags=["plain"]),
                ]
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
                from ducktest5 import match
                SUITES = {"default": {"paths": ["test/keep"]}}
                RULES = [match("keep/spark.test", tags=["spark"]), match("keep/other.test", tags=["quiet"])]
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
        "b/paired.py::body[cmt]",
        "b/paired.py::body[plain]",
    ]


def test_a_suites_skip_entry_may_be_a_clause(project):
    # trimmed scopes to test/a + test/b (directories, so the slow test under a is in scope), then drops
    # only tests that are both backend=oss and tag=a -- y has no backend axis, so it is not dropped
    assert ids(select(project, suites=["trimmed"])[0]) == [
        "a/x.test::x[backend=dbx]",
        "a/y.test_slow::y",
        "b/one.test::one",
        "b/paired.py::body[cmt]",
        "b/paired.py::body[plain]",
        "b/solo.py::solo",
    ]


def test_suites_is_read_and_an_unrelated_top_level_name_is_ignored(tmp_path):
    tests, proj = tree(tmp_path, {"test/ducktest_conf.py": "SETS = {}", "test/a.test": ""})
    assert proj.suites == {}
    tests, proj = tree(tmp_path / "ok", {"test/ducktest_conf.py": 'SUITES = {"x": {}}', "test/a.test": ""})
    assert proj.suites == {"x": {}}


def test_a_project_suite_named_all_is_an_error(tmp_path):
    with pytest.raises(UsageError, match="may not define a suite named 'all'"):
        tree(tmp_path, {"test/ducktest_conf.py": 'SUITES = {"all": {}}', "test/a.test": ""})


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
                from ducktest5 import match
                SUITES = {"default": {"paths": ["test/keep"]}}
                RULES = [match("keep/a.test", tags=["a"]), match("outside/b.test", tags=["b"])]
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
                from ducktest5 import match
                SUITES = {"smoke": {"select": ["tag=smoke"]}, "databricks": {"select": ["tag=databricks"]}}
                RULES = [
                    match("a.test", tags=["smoke"]),
                    match("b.test", tags=["smoke", "databricks"]),
                    match("c.test", tags=["databricks"]),
                ]
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
                from ducktest5 import match
                SUITES = {"default": {"paths": ["test/keep"]}, "spark": {"select": ["tag=spark"]}}
                RULES = [match("keep/spark.test", tags=["spark"]), match("keep/other.test", tags=["quiet"])]
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
                from ducktest5 import match
                SUITES = {"spark": {"select": ["tag=spark"]}}
                RULES = [match("a.test", tags=["spark"]), match("b.test", tags=["other"])]
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
                from ducktest5 import match
                SUITES = {
                    "core": {"select": ["tag=core"]},
                    "nightly": {"select": ["suite=core", "tag=extra"]},
                }
                RULES = [match("a.test", tags=["core"]), match("b.test", tags=["extra"]), match("c.test", tags=["other"])]
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
                SUITES = {
                    "smoke": {"select": ["suite=nightly"]},
                    "nightly": {"select": ["suite=smoke"]},
                }
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
                from ducktest5 import match
                SUITES = {"a": {"select": ["tag=x"]}, "b": {"select": ["tag=y"]}}
                RULES = [match("both.test", tags=["x", "y"])]
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
                SUITES = {"everything": {"select": ["suite=all"]}, "base": {"select": ["suite=default"]}}
            """,
            "test/a.test": "",
            "test/b.test_slow": "",
        },
    )
    assert set(ids(select((tests, proj), suites=["everything"])[0])) == set(ids(tests))
    assert ids(select((tests, proj), suites=["base"])[0]) == ["a.test::a"]  # default: slow left out


@pytest.fixture
def declared(tmp_path):
    """A tree whose tests name their own suites: `quick`, which only they name, and `smoke`, which the
    project file defines as well -- and which one of them declares itself out of."""
    return tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest5 import match
                SUITES = {"smoke": {"select": ["tag=smoke"]}}
                RULES = [match("a.test", tags=["smoke"])]
            """,
            "test/a.test": "",
            "test/b.py": """
                from ducktest5 import test
                @test(suites=["quick"])
                def b(ctx): pass
            """,
            "test/c.py": """
                from ducktest5 import test
                @test(tags=["smoke"], suites=["!smoke"])
                def c(ctx): pass
            """,
        },
    )


def test_a_test_declares_itself_into_a_suite_no_project_file_defines(declared):
    assert ids(select(declared, suites=["quick"])[0]) == ["b.py::b"]
    assert ids(select(declared, select=["suite=quick"])[0]) == ["b.py::b"]
    assert ids(select(declared, select=["suite=quick;type=python"])[0]) == ["b.py::b"]
    assert select(declared, suites=["quick"])[1]["a.test::a"] == "not in suite 'quick'"


def test_a_test_declared_out_beats_the_suites_own_select(declared):
    # c carries tag smoke, which is all the suite asks for -- and is out of it all the same
    selected, why_not = select(declared, suites=["smoke"])
    assert ids(selected) == ["a.test::a"]
    assert why_not["c.py::c"] == "not in suite 'smoke': declared out  <- @test"


def test_an_id_beats_a_declaration_out_of_a_suite(declared):
    assert ids(select(declared, suites=["smoke"], ids=["c.py::c"])[0]) == ["a.test::a", "c.py::c"]


def test_a_declared_suite_is_a_known_name_and_an_undeclared_one_is_not(declared):
    with pytest.raises(UsageError, match=r"no suite \['nope'\]; known suites: \['quick', 'smoke'\]"):
        select(declared, suites=["nope"])


def test_declaring_a_test_out_of_the_default_suite_leaves_a_bare_run(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest5 import match
                RULES = [match("slow.test_slow", suites=["default"])]
            """,
            "test/a.test": "",
            "test/b.py": """
                from ducktest5 import test
                @test(suites=["!default"])
                def b(ctx): pass
            """,
            "test/slow.test_slow": "",
        },
    )
    selected, why_not = apply(tests, Selection(), proj)
    # declared in, so the bare run takes it although it is slow; declared out, so it takes neither b
    assert ids(selected) == ["a.test::a", "slow.test_slow::slow"]
    assert why_not["b.py::b"] == "not in suite 'default': declared out  <- @test"


def test_a_rule_declares_a_suite_for_a_directory_and_one_test_declares_itself_out(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                from ducktest5 import match
                RULES = [match("cloud/**", suites=["cloud"])]
            """,
            "test/cloud/a.test": "",
            "test/cloud/b.py": """
                from ducktest5 import test
                @test(suites=["!cloud"])
                def b(ctx): pass
            """,
            "test/local/c.test": "",
        },
    )
    selected, why_not = apply(tests, Selection(suites=["cloud"]), proj)
    assert ids(selected) == ["cloud/a.test::a"]
    assert why_not["cloud/b.py::b"] == "not in suite 'cloud': declared out  <- @test"
    assert why_not["local/c.test::c"] == "not in suite 'cloud'"


def test_the_last_word_on_a_suite_name_wins(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": "from ducktest5 import default\nRULES = [default(suites=['x'])]",
            "test/sub/ducktest_conf.py": "from ducktest5 import default\nRULES = [default(suites=['!x'])]",
            "test/sub/in.py": "from ducktest5 import test\n@test(suites=['x'])\ndef t(ctx): pass",
            "test/sub/out.test": "",
            "test/top.test": "",
        },
    )
    selected, why_not = apply(tests, Selection(suites=["x"]), proj)
    assert ids(selected) == ["sub/in.py::t", "top.test::top"]
    assert why_not["sub/out.test::out"] == "not in suite 'x': declared out  <- sub/ducktest_conf.py: default"


def test_the_scan_record_carries_the_declared_suites(tmp_path):
    tests, proj = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": "from ducktest5 import default\nRULES = [default(suites=['nightly', 'quick'])]",
            "test/a.py": "from ducktest5 import test\n@test(suites=['!nightly'])\ndef a(ctx): pass",
            "test/b.test": "",
        },
    )
    records = {c.id: c.record(proj)["selection"] for c in tests}
    assert records["a.py::a"]["suites"] == ["!nightly", "quick"]  # the name keeps the place it was first said
    assert records["b.test::b"]["suites"] == ["nightly", "quick"]
    plain, plain_proj = tree(tmp_path / "2", {"test/a.test": ""})
    assert "suites" not in plain[0].record(plain_proj)["selection"]


@pytest.mark.parametrize(
    "files, message",
    [
        (
            {"test/ducktest_conf.py": "from ducktest5 import default\nRULES = [default(suites=['!all'])]"},
            r"rule '\*\*': '!all'",
        ),
        (
            {"test/a.py": "from ducktest5 import test\n@test(suites=['!all'])\ndef a(ctx): pass"},
            r"a\.py::a: @test\(\.\.\.\): '!all'",
        ),
    ],
)
def test_declaring_a_test_out_of_suite_all_is_an_error(tmp_path, files, message):
    with pytest.raises(UsageError, match=message):
        tree(tmp_path, {"test/a.test": "", **files})


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
            "test/ducktest_conf.py": 'SUITES = {"onlya": {"select": ["path=a"]}}',
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
            "test/ducktest_conf.py": "from ducktest5 import match\nRULES = [match('*.test', tags=['top']), match('**/*.test', tags=['any']), match('sub/**', tags=['below'])]",
            "test/x.test": "",
            "test/sub/deep/y.test": "",
        },
    )
    tags = {c.definition.path: c.definition.tags for c in tests}
    assert tags == {"test/x.test": ["top", "any"], "test/sub/deep/y.test": ["any", "below"]}


def test_an_excluded_directory_is_not_scanned_and_its_configuration_is_not_loaded(tmp_path):
    tests, project = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": "EXCLUDE = ['old']",
            "test/a.test": "",
            "test/old/b.test": "",
            "test/old/ducktest_conf.py": "from ducktest5 import match\nRULES = [match('gone.test', tags=['x'])]",
        },
    )
    assert ids(tests) == ["a.test::a"]


def test_a_skip_while_a_test_file_is_imported_is_a_usage_error_that_says_where_to_skip(tmp_path):
    with pytest.raises(
        UsageError, match=r"early\.py called skip\('no CLI'\) while it was imported; skip inside the body"
    ):
        tree(tmp_path, {"test/early.py": "from ducktest5 import skip, test\nskip('no CLI')\n@test()\ndef t(ctx): pass"})


def test_only_a_file_with_test_bodies_is_imported_and_a_broken_one_says_which(tmp_path):
    tests, _ = tree(
        tmp_path, {"test/a.test": "", "test/helper.py": "raise RuntimeError('a helper must not run at scan')"}
    )
    assert ids(tests) == ["a.test::a"]
    with pytest.raises(UsageError, match=r"broken\.py could not be imported: ModuleNotFoundError"):
        tree(
            tmp_path / "2",
            {"test/broken.py": "import no_such_module\nfrom ducktest5 import test\n@test()\ndef t(ctx): pass"},
        )
    with pytest.raises(UsageError, match=r"unknown option\(s\) \['cases'\]"):
        tree(tmp_path / "3", {"test/old.py": "from ducktest5 import test\n@test(cases={'a': {}})\ndef t(ctx): pass"})


def test_a_test_can_import_a_helper_beside_it(tmp_path):
    tests, _ = tree(
        tmp_path,
        {
            "test/helper.py": "X = 1",
            "test/t.py": "from helper import X\nfrom ducktest5 import test\n@test()\ndef t(ctx): pass",
        },
    )
    assert ids(tests) == ["t.py::t"]


def test_two_tests_of_one_file_name_get_their_own_directory(tmp_path):
    body = "from ducktest5 import test\n@test()\ndef t(ctx): pass"
    tests, _ = tree(tmp_path, {"test/a/same.py": body, "test/b/same.py": body})
    assert len({c.dir_name for c in tests}) == 2


def test_a_path_names_its_paired_file_and_nothing_wider(project):
    assert ids(select(project, paths=["b/paired.test"])[0]) == [
        "b/paired.py::body[cmt]",
        "b/paired.py::body[plain]",
    ]
    assert ids(select(project, paths=["a/y.test_slow"])[0]) == ["a/y.test_slow::y"]  # named exactly: asked for
    with pytest.raises(UsageError, match="no test definition has a params row"):
        select(project, select=["params=cmtt"])
    with pytest.raises(UsageError, match="unknown key 'case'"):
        select(project, select=["case=cmt"])


def test_retries_is_a_rule_field_and_a_whole_number(tmp_path):
    files = {"test/ducktest_conf.py": "from ducktest5 import match\nRULES = [match('net.test', retries=2)]"}
    tests, _ = tree(tmp_path, {**files, "test/net.test": "", "test/plain.test": ""})
    assert [(c.definition.name, c.definition.retries) for c in tests] == [("net", 2), ("plain", 0)]
    with pytest.raises(UsageError, match="retries=-1; it is a whole number, 0 or more"):
        tree(tmp_path, {"test/ducktest_conf.py": "from ducktest5 import default\nRULES = [default(retries=-1)]"})


BIND_CONF = """
    from ducktest5 import default
    from ducktest5.resources import StorageDef

    STORAGES = [StorageDef("a", "/tmp/a"), StorageDef("b", "/tmp/b")]
    RULES = [default(storage="a")]
"""


def test_a_tests_own_binding_of_a_kind_replaces_the_rules(tmp_path):
    tests, _ = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": BIND_CONF,
            "test/ruled.test": "",
            "test/own.py": "from ducktest5 import test\n@test(storage='b')\ndef own(ctx): pass",
        },
    )
    by_name = {c.definition.name: c for c in tests}
    assert [d.id for d, _ in by_name["ruled"].inputs] == ["a"]
    assert [d.id for d, _ in by_name["own"].inputs] == ["b"]
    assert by_name["own"].definition.why == [
        "storage='a'  <- ducktest_conf.py: default",
        "storage='b'  <- @test",
    ]
    assert resolve(by_name["own"]).roots["temp_dir_root"].value == "/tmp/b/temp/<run_token>"
    assert by_name["own"].bind_lines() == ["bind: storage=b  <- @test"]


AXIS_BIND_CONF = """
    from ducktest5 import Variant, default, match
    from ducktest5.resources import StorageDef

    STORAGES = [StorageDef("a", "/tmp/a"), StorageDef("b", "/tmp/b")]
    AXES = {"store": [Variant("b", storage="b")]}
    RULES = RULES_HERE
"""


def test_an_axis_value_replaces_what_a_default_binds_and_says_so(tmp_path):
    conf = AXIS_BIND_CONF.replace("RULES_HERE", '[default(storage="a", axes=["store"])]')
    tests, _ = tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})
    assert [d.id for d, _ in tests[0].inputs] == ["b"]
    assert tests[0].bind_lines() == ["bind: storage=b  <- AXES store=b"]
    assert tests[0].why == [
        "storage='a'  <- ducktest_conf.py: default",
        "axes=['store']  <- ducktest_conf.py: default",
        "storage='b'  <- AXES store=b",
    ]


@pytest.mark.parametrize(
    "rules, own, source",
    [
        ('[default(axes=["store"]), match("t.py", storage="a")]', "", "ducktest_conf.py: match 't.py'"),
        ('[default(storage="a", axes=["store"])]', "storage='a'", "@test"),
    ],
)
def test_a_kind_an_axis_value_and_a_match_or_test_both_bind_is_an_error(tmp_path, rules, own, source):
    body = f"from ducktest5 import test\n@test({own})\ndef t(ctx): pass"
    message = f"t.py::t[store=b]: storage= is 'a' by {source} and 'b' by AXES store=b; bind it in one place"
    with pytest.raises(UsageError, match=re.escape(message)):
        tree(tmp_path, {"test/ducktest_conf.py": AXIS_BIND_CONF.replace("RULES_HERE", rules), "test/t.py": body})


def test_a_kind_two_axis_values_of_one_test_both_bind_is_an_error(tmp_path):
    conf = """
        from ducktest5 import Variant, default
        from ducktest5.resources import StorageDef

        STORAGES = [StorageDef("s3", "s3://b"), StorageDef("proxied", "/tmp/p")]
        AXES = {
            "storage": [Variant("s3", storage="s3")],
            "via": [Variant("direct"), Variant("reverse-proxy", storage="proxied")],
        }
        RULES = [default(axes=["storage", "via"])]
    """
    message = (
        "t.test::t[storage=s3,via=reverse-proxy]: storage= is given by AXES storage=s3 and by AXES via=reverse-proxy; "
        "bind it in one place"
    )
    with pytest.raises(UsageError, match=re.escape(message)):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})


def test_a_value_an_axis_value_and_a_params_row_both_give_is_a_scan_error(tmp_path):
    conf = """
        from ducktest5 import Variant, default

        AXES = {"storage": [Variant("s3")]}
        RULES = [default(axes=["storage"])]
    """
    body = "from ducktest5 import test\n@test(params={'wide': {'storage': 'x'}})\ndef t(ctx): pass"
    message = (
        "t.py::t[wide,storage=s3]: the value 'storage' is given by AXES storage=s3 and by the params row 'wide'; "
        "give it in one place"
    )
    with pytest.raises(UsageError, match=re.escape(message)):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.py": body})


def test_a_binding_names_a_declared_storage_credential_or_service(tmp_path):
    with pytest.raises(UsageError, match="own.py::own: storage='c' is not in STORAGES"):
        tree(
            tmp_path,
            {
                "test/ducktest_conf.py": BIND_CONF,
                "test/own.py": "from ducktest5 import test\n@test(storage='c')\ndef own(ctx): pass",
            },
        )


def test_a_binding_names_one_of_its_own_kind(tmp_path):
    conf = BIND_CONF + '    RULES = [default(credential="a")]\n'
    with pytest.raises(UsageError, match="t.test::t: credential='a' is not in CREDENTIALS"):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})


def test_bind_is_not_a_keyword_any_more_and_the_error_names_the_ones_there_are(tmp_path):
    with pytest.raises(
        UsageError, match=r"unknown option\(s\) \['bind'\]; it takes .*'storage', 'credential', 'service'"
    ):
        tree(tmp_path, {"test/ducktest_conf.py": BIND_CONF.replace('storage="a"', 'bind={"storage": "a"}')})
    body = "from ducktest5 import test\n@test(bind={'storage': 'b'})\ndef own(ctx): pass"
    with pytest.raises(
        UsageError, match=r"own.py::own: @test\(...\): unknown option\(s\) \['bind'\]; it takes .*'storage'"
    ):
        tree(tmp_path / "2", {"test/ducktest_conf.py": BIND_CONF, "test/own.py": body})


def test_an_axis_value_that_unsets_a_credentials_names_does_not_hide_it(tmp_path, monkeypatch):
    conf = """
        from ducktest5 import Variant, default
        from ducktest5.resources import S3CredentialDef, StorageDef

        STORAGES = [StorageDef("bucket", "s3://bucket")]
        CREDENTIALS = [S3CredentialDef.from_aws_env("key", scopes=["bucket"])]
        AXES = {"auth": [Variant("key"), Variant("none", process_env={"AWS_ACCESS_KEY_ID": None})]}
        RULES = [default(storage="bucket", axes=["auth"])]
    """
    tests, _ = tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})
    assert [[d.id for d, _ in c.inputs] for c in tests] == [["bucket", "key"], ["bucket", "key"]]


CREDENTIAL_CONF = """
    from ducktest5 import default
    from ducktest5.resources import S3CredentialDef, StorageDef, from_env

    STORAGES = [StorageDef("lake", "s3://lake"), StorageDef("blobs", "az://c/p"), StorageDef("other", "s3://other")]
    KEYS = from_env({"DT_KEY": "key_id", "DT_SECRET": "secret"})
    CREDENTIALS = [S3CredentialDef("keys_a", sources=[KEYS], scopes={scopes_a}), S3CredentialDef("keys_b", sources=[KEYS], scopes=["other"])]
    RULES = [default({bind})]
"""


def test_binding_the_storage_renders_a_secret_and_binding_only_the_credential_gives_the_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DT_KEY", "k-value")
    monkeypatch.setenv("DT_SECRET", "s-value")
    conf = CREDENTIAL_CONF.replace("{scopes_a}", '["lake"]').replace("{bind}", "")
    conf += """
    from ducktest5 import match
    RULES += [match("stored.py", storage="lake"), match("keys_only.py", credential="keys_a")]
"""
    body = "from ducktest5 import test\n@test()\ndef {name}(ctx): pass\n"
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
        from ducktest5 import default
        from ducktest5.resources import BootedEngineDef, CredentialDef, Field, StorageDef

        @dataclass(eq=False)
        class TokenDef(CredentialDef):
            schemes = ("s3",)
            fields = {"token": Field(required=True, secret=True)}
            env = {"token": ("DT_TOKEN",)}

        STORAGES = [StorageDef("lake", "s3://lake")]
        CREDENTIALS = [TokenDef("tok", scopes=["lake"])]
        ENGINES = [BootedEngineDef("spark", boot=lambda log: None, dialect="spark")]
        RULES = [default(storage="lake")]
    """
    body = "from ducktest5 import test\n@test(engines=['spark'])\ndef t(ctx): pass\n"
    message = (
        "t.py::t: engine 'duckdb' cannot render TokenDef 'tok' (duckdb renders: S3CredentialDef, AzureCredentialDef)"
    )
    with pytest.raises(UsageError, match=re.escape(message)):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.py": body})


def test_two_credentials_of_one_type_on_one_booted_engine_are_a_scan_error(tmp_path):
    conf = (
        CREDENTIAL_CONF.replace("{scopes_a}", '["lake"]').replace("{bind}", "")
        + """
    from ducktest5.resources import BootedEngineDef
    from ducktest5 import match
    ENGINES = [BootedEngineDef("spark", boot=lambda log: None, dialect="spark")]
    RULES = [match("a.py", storage="lake"), match("b.py", storage="other")]
"""
    )
    body = "from ducktest5 import test\n@test(engines=['spark'])\ndef t(ctx): pass\n"
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
    conf = CREDENTIAL_CONF.replace("{scopes_a}", scopes_a).replace("{bind}", bind)
    with pytest.raises(UsageError, match=re.escape(message)):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})


def test_an_engine_is_declared_among_the_engines_not_the_services(tmp_path):
    engine = "from ducktest5 import EngineDef\nE = EngineDef('fake', 'fake:1', lambda log: None)\n"
    _, project = tree(tmp_path, {"test/ducktest_conf.py": engine + "ENGINES = [E]", "test/a.test": ""})
    assert (list(project.engines), list(project.services)) == (["fake"], [])
    with pytest.raises(UsageError, match="declare engines in ENGINES"):
        tree(tmp_path / "2", {"test/ducktest_conf.py": engine + "SERVICES = [E]", "test/a.test": ""})


def test_a_non_engine_service_is_declared_among_the_services_not_the_engines(tmp_path):
    service = "from ducktest5.services.http import HttpServerDef\nS = HttpServerDef('fake')\n"
    with pytest.raises(UsageError, match="declare services in SERVICES"):
        tree(tmp_path, {"test/ducktest_conf.py": service + "ENGINES = [S]", "test/a.test": ""})


# -----------------------------------------------------------------------------
# PYTHONPATH: the repo's helper code
#


@pytest.fixture
def clean_path(monkeypatch):
    """What a scan puts on sys.path, and the helpers it imports, go with the test."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))


HELPER_BODY = """
    from ducktest5 import test
    from pp_helper import ANSWER

    @test()
    def uses_helper(ctx):
        assert ANSWER == 42
"""


def test_pythonpath_puts_helpers_beside_the_tree_on_the_path_before_the_project_file_runs(tmp_path, clean_path):
    (tmp_path / ".git").mkdir()
    files = {
        "test/ducktest_conf.py": "from pp_kinds import KIND\nPYTHONPATH = ['py', '../scripts']\nKNOWN = KIND",
        "test/py/pp_helper.py": "ANSWER = 42",
        "scripts/pp_kinds.py": "KIND = 'catalog'",
        "test/a/body.py": HELPER_BODY,
    }
    tests, project = tree(tmp_path, files)
    assert ids(tests) == ["a/body.py::uses_helper"]
    assert project.pythonpath == (str(tmp_path / "test" / "py"), str(tmp_path / "scripts"))
    assert sys.path[:2] == list(project.pythonpath)


def test_without_pythonpath_a_helper_outside_the_body_s_directory_is_not_found(tmp_path, clean_path):
    with pytest.raises(UsageError, match=r"body\.py could not be imported: ModuleNotFoundError"):
        tree(
            tmp_path,
            {"test/ducktest_conf.py": "", "test/py/pp_helper.py": "ANSWER = 42", "test/a/body.py": HELPER_BODY},
        )


@pytest.mark.parametrize(
    "conf, text",
    [
        ("PYTHONPATH = ['nope']", r"PYTHONPATH entry 'nope' is not a directory"),
        ("PYTHONPATH = ['../..']", r"PYTHONPATH entry '../..' is outside the repository"),
        ("import os\nPYTHONPATH = [os.path.join('py')]", r"ducktest_conf\.py:2: PYTHONPATH must be a literal list"),
        ("PYTHONPATH = 'py'", r"PYTHONPATH must be a literal list"),
        ("PYTHONPATH = ['py']\nPYTHONPATH.append('a')", r"PYTHONPATH is read from the source before the file runs"),
    ],
)
def test_a_pythonpath_that_cannot_be_read_or_used_is_a_usage_error_naming_the_file(tmp_path, clean_path, conf, text):
    files = {"test/ducktest_conf.py": conf, "test/py/x.py": "", "test/a/x.test": ""}
    with pytest.raises(UsageError, match=text):
        tree(tmp_path, files)


def test_pythonpath_belongs_in_the_root_project_file_only(tmp_path, clean_path):
    files = {"test/ducktest_conf.py": "", "test/a/ducktest_conf.py": "PYTHONPATH = ['.']", "test/a/x.test": ""}
    with pytest.raises(UsageError, match=r"a/ducktest_conf\.py: PYTHONPATH belongs in the root"):
        tree(tmp_path, files)


def test_scan_env_says_where_helpers_come_from(tmp_path, clean_path, monkeypatch, capsys):
    from ducktest5.cli import main

    tree(tmp_path, {"test/ducktest_conf.py": "PYTHONPATH = ['py']", "test/py/x.py": "", "test/a.test": ""})
    monkeypatch.chdir(tmp_path)
    assert main(["scan", "--env"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "pythonpath: py  <- ducktest_conf.py"


# -----------------------------------------------------------------------------
# One option set: a rule, @test and an axis value take the same names
#

OPTIONS = (
    'requires=["json"], settings={"threads": 2}, init_sql="SELECT 1", process_env={"HTTP_PROXY": "p"}, '
    'values={"region": "eu"}, timeout=7, retries=1, mutex="m", tags=["t"]'
)


@pytest.mark.parametrize(
    "conf, body",
    [
        (f"RULES = [default({OPTIONS})]", "@test()"),
        ("RULES = []", f"@test({OPTIONS})"),
        (f'AXES = {{"k": [Variant("v", {OPTIONS})]}}\nRULES = [default(axes=["k"])]', "@test()"),
    ],
    ids=["rule", "@test", "axis value"],
)
def test_every_option_works_on_a_rule_on_test_and_on_an_axis_value(tmp_path, conf, body):
    head = "from ducktest5 import Variant, default\n"
    (test,) = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": head + conf,
            "test/t.py": f"from ducktest5 import test\n{body}\ndef t(ctx): pass\n",
        },
    )[0]
    env = resolve(test)
    assert env.extensions == ["json"]
    assert [e.value for e in env.init_sql] == ["SET threads = '2';", "SELECT 1;"]
    assert env.environ({}) == {"HTTP_PROXY": "p"}
    assert env.value_map["region"] == "eu"
    assert (test.timeout, test.retries, test.mutex) == (7, 1, ["m"])
    assert "t" in test.tags


def test_an_axis_value_takes_no_axes_and_an_unknown_option_names_the_ones_there_are(tmp_path):
    for option, message in (
        ('axes=["k"]', r"Variant 'v': unknown option\(s\) \['axes'\]; it takes \['tags', 'engines', 'mutex'"),
        ('roots={"data_dir": "/d"}', r"Variant 'v': unknown option\(s\) \['roots'\]"),
        ('inputs=["a"]', r"Variant 'v': unknown option\(s\) \['inputs'\]"),
    ):
        conf = f'from ducktest5 import Variant\nAXES = {{"k": [Variant("v", {option})]}}\n'
        with pytest.raises(UsageError, match=message):
            tree(tmp_path, {"test/ducktest_conf.py": conf, "test/a.test": ""})


def test_init_sql_adds_up_from_the_rules_down_and_names_only_its_source(tmp_path):
    conf = 'from ducktest5 import default, match\nRULES = [default(init_sql="SELECT 1"), match("t.test", init_sql=["SELECT 2"])]'
    (test,) = tree(tmp_path, {"test/ducktest_conf.py": conf, "test/t.test": ""})[0]
    assert [e.value for e in resolve(test).init_sql] == ["SELECT 1;", "SELECT 2;"]
    assert "init_sql  <- ducktest_conf.py: match 't.test'" in test.why


# -----------------------------------------------------------------------------
# match(requires_env=...): the .test files that gate themselves on a variable
#


def test_requires_env_selects_the_files_with_that_require_env_line_and_the_path_still_counts(tmp_path, caplog):
    conf = """
        from ducktest5 import match
        RULES = [match("s3/**", requires_env="S3_TEST_SERVER", tags=["gated"])]
    """
    with caplog.at_level("INFO", logger="ducktest5.conf"):
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
    conf = "from ducktest5 import match\nRULES = [match('**', requires_env='NEVER_GATED', tags=['x'])]"
    with pytest.raises(UsageError, match=r"match '\*\*', requires_env='NEVER_GATED' matched no test"):
        tree(tmp_path, {"test/ducktest_conf.py": conf, "test/a.test": "SELECT 1;\n"})
