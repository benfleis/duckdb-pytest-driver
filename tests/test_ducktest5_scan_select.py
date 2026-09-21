"""ducktest5's scan, configuration files and selection, on a small tree made for each test."""

import textwrap

import pytest

from ducktest5.binary import UsageError
from ducktest5.caseenv import resolve
from ducktest5.scan import scan
from ducktest5.select import Selection, apply

ROOT_CONF = """
    from ducktest5 import Variant, default, match

    AXES = {"backend": [Variant("oss", values={"catalog": "duck"}), Variant("dbx", tags=["needs-credentials"])]}
    SETS = {"smoke": {"paths": ["test/a", "test/b/one.test"]}, "cmt": {"select": ["case=cmt"]}}
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
            "test/ducktestconf.py": ROOT_CONF,
            "test/a/ducktestconf.py": """
                from ducktest5 import default, match
                RULES = [default(values={"region": "us"}), match("x.test", variants=["backend"], timeout=5)]
            """,
            "test/a/x.test": "",
            "test/a/y.test_slow": "",
            "test/b/one.test": "",
            "test/b/paired.test": "",
            "test/b/paired.py": """
                from ducktest5 import test
                @test(cases={"cmt": {}, "plain": {}}, tags=["mine"])
                def body(ctx): ctx.run_test()
            """,
            "test/b/solo.py": """
                from ducktest5 import test
                @test()
                def solo(ctx): pass
            """,
        },
    )


def ids(cases):
    return [c.id for c in cases]


def test_files_become_tests_by_name_only_and_cases_multiply(project):
    cases, _ = project
    assert ids(cases) == [
        "a/x.test::x[backend=oss]",
        "a/x.test::x[backend=dbx]",
        "a/y.test_slow::y",
        "b/one.test::one",
        "b/paired.py::body[cmt]",
        "b/paired.py::body[plain]",
        "b/solo.py::solo",
    ]
    by_id = {c.id: c for c in cases}
    assert by_id["b/paired.py::body[cmt]"].test.type == "paired"
    assert by_id["b/paired.py::body[cmt]"].test.body_file == "test/b/paired.test"
    assert by_id["b/solo.py::solo"].test.type == "python"


def test_an_id_is_the_same_from_every_working_directory(tmp_path, project):
    from_below = scan(str(tmp_path / "test"), working_dir=str(tmp_path / "test" / "a"))[0]
    assert ids(from_below) == ids(project[0])
    # the path the binary and the PATH arguments use does follow the working directory
    assert [c.test.path for c in from_below][:2] == ["x.test", "x.test"]


def test_the_axis_parts_of_an_id_come_in_the_order_of_axes(tmp_path):
    cases, _ = tree(
        tmp_path,
        {
            "test/ducktestconf.py": """
                from ducktest5 import Variant, default
                AXES = {"backend": [Variant("oss")], "storage": [Variant("s3")]}
                RULES = [default(variants=["storage", "backend"])]
            """,
            "test/a.test": "",
        },
    )
    assert ids(cases) == ["a.test::a[backend=oss,storage=s3]"]


def test_rules_apply_from_the_root_down_and_the_test_itself_last(project):
    x = next(c for c in project[0] if c.id == "a/x.test::x[backend=oss]")
    assert x.test.tags == ["all", "a"]
    # the deeper file replaced `region`, the variant added `catalog`, and the axis brings its own id
    assert resolve(x).value_map == {"backend": "oss", "region": "us", "catalog": "duck"}
    assert resolve(x).values["region"].source == "a/ducktestconf.py: default"
    assert resolve(x).values["catalog"].source == "AXES backend=oss"
    assert x.test.timeout == 5  # a single value: the last rule that set it
    assert any("a/ducktestconf.py: match 'x.test'" in line for line in x.test.why)
    paired = next(c for c in project[0] if c.id == "b/paired.py::body[cmt]")
    assert paired.test.tags == ["all", "mine"]
    assert "needs-credentials" in next(c for c in project[0] if c.id.endswith("[backend=dbx]")).tags


def test_a_rule_that_matches_nothing_is_an_error(tmp_path):
    with pytest.raises(UsageError, match="matched no test"):
        tree(
            tmp_path,
            {
                "test/ducktestconf.py": "from ducktest5 import match\nRULES = [match('s3/authentcated/**', tags=['x'])]",
                "test/s3/authenticated/a.test": "",
            },
        )


def test_an_unknown_rule_field_or_axis_is_an_error(tmp_path):
    with pytest.raises(UsageError, match="unknown field"):
        tree(
            tmp_path,
            {
                "test/ducktestconf.py": "from ducktest5 import default\nRULES = [default(needs_env=['X'])]",
                "test/a.test": "",
            },
        )
    with pytest.raises(UsageError, match="takes part in axis 'nope'"):
        tree(
            tmp_path / "2",
            {
                "test/ducktestconf.py": "from ducktest5 import default\nRULES = [default(variants=['nope'])]",
                "test/a.test": "",
            },
        )


def select(project, **kwargs):
    cases, proj = project
    return apply(cases, Selection(**kwargs), proj)


def test_a_bare_run_leaves_slow_out_and_says_why(project):
    selected, why_not = select(project)
    assert "a/y.test_slow::y" not in ids(selected) and len(selected) == 6
    assert "--slow" in why_not["a/y.test_slow::y"]
    assert "a/y.test_slow::y" in ids(select(project, slow=True)[0])
    assert ids(select(project, select=["tag=slow"])[0]) == ["a/y.test_slow::y"]


def test_paths_are_a_union_and_selects_must_all_hold(project):
    assert len(select(project, paths=["test/a/x.test", "test/b/one.test"])[0]) == 3
    assert ids(select(project, paths=["test/a"], select=["backend=oss"])[0]) == ["a/x.test::x[backend=oss]"]
    assert ids(select(project, select=["not:tag=a", "type=sqllogic"])[0]) == ["b/one.test::one"]
    assert ids(select(project, select=["case=cmt,plain"])[0]) == [
        "b/paired.py::body[cmt]",
        "b/paired.py::body[plain]",
    ]


def test_a_set_is_paths_and_selects_with_a_name(project):
    assert ids(select(project, set_name="smoke")[0]) == [
        "a/x.test::x[backend=oss]",
        "a/x.test::x[backend=dbx]",
        "b/one.test::one",
    ]
    assert ids(select(project, set_name="cmt")[0]) == ["b/paired.py::body[cmt]"]


def test_an_id_is_taken_verbatim(project):
    assert ids(select(project, ids=["b/paired.py::body[plain]"])[0]) == ["b/paired.py::body[plain]"]


def test_an_id_is_added_to_what_paths_and_selects_give(project):
    slow, plain = "a/y.test_slow::y", "b/paired.py::body[plain]"
    selected, why_not = select(project, paths=["test/b/one.test"], ids=[plain])
    assert ids(selected) == ["b/one.test::one", plain]
    assert why_not["b/solo.py::solo"] == "outside the given paths"
    # no select, path or tag takes out a case that is named by id
    assert ids(select(project, select=["type=python"], ids=[slow])[0]) == [slow, "b/solo.py::solo"]
    assert ids(select(project, set_name="cmt", ids=[slow])[0]) == [slow, "b/paired.py::body[cmt]"]
    # and the slow rule still holds for the cases that are not named
    assert ids(select(project, paths=["test/a"], select=["backend=oss"], ids=[plain])[0]) == [
        "a/x.test::x[backend=oss]",
        plain,
    ]
    assert slow in select(project, paths=["test/a"], ids=[plain])[1]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"select": ["tag=smok"]}, "no scanned case has tag"),
        ({"select": ["backend=gcs"]}, "axis 'backend' has no value"),
        ({"select": ["backnd=oss"]}, "unknown key 'backnd'"),
        ({"select": ["slow"]}, "write key=value"),
        ({"paths": ["test/nowhere"]}, "holds no test"),
        ({"ids": ["a/x.test::x"]}, "no such case"),
        ({"set_name": "nightly"}, "the project has sets"),
        ({"skip": ["tag=smok"]}, "no scanned case has tag"),
        ({"skip": ["backnd=oss"]}, "unknown key 'backnd'"),
        ({"skip": ["not:tag=a"]}, "a skip is already a negation"),
        ({"skip": ["!tag=a"]}, "a skip is already a negation"),
    ],
)
def test_a_selection_that_can_never_match_is_an_error(project, kwargs, message):
    with pytest.raises(UsageError, match=message):
        select(project, **kwargs)


def test_a_negated_select_is_translated_into_a_skip_at_parse_time(project):
    xid = "a/x.test::x[backend=oss]"
    for form in ("not:tag=a", "!tag=a"):
        assert ids(select(project, select=[form])[0]) == ids(select(project, skip=["tag=a"])[0])
    _, why_not = select(project, select=["not:tag=a"])
    assert why_not[xid] == "--skip tag=a (from --select not:tag=a)"
    _, why_not = select(project, select=["!tag=a"])
    assert why_not[xid] == "--skip tag=a (from --select !tag=a)"


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


def test_why_line_for_a_command_line_skip(project):
    _, why_not = select(project, skip=["tag=a"])
    assert why_not["a/x.test::x[backend=oss]"] == "--skip tag=a"


@pytest.mark.parametrize("expr", ["tag=a", "type=sqllogic", "case=cmt,plain", "backend=oss", "tag=mine"])
def test_a_negated_select_and_the_equivalent_skip_choose_the_same_cases(project, expr):
    assert ids(select(project, select=[f"not:{expr}"])[0]) == ids(select(project, skip=[expr])[0])


@pytest.mark.parametrize("expr", ["tag=a", "backend=oss", "case=cmt"])
def test_a_negated_select_and_skip_agree_on_top_of_a_positive_selection(project, expr):
    with_negation = select(project, paths=["test/a", "test/b"], select=[f"not:{expr}"])[0]
    with_skip = select(project, paths=["test/a", "test/b"], skip=[expr])[0]
    assert ids(with_negation) == ids(with_skip)


def test_a_lone_skip_keeps_a_default_set_as_the_base(tmp_path):
    cases, proj = tree(
        tmp_path,
        {
            "test/ducktestconf.py": """
                from ducktest5 import match
                SETS = {"default": {"paths": ["test/keep"], "skip": ["tag=spark"]}}
                RULES = [match("keep/spark.test", tags=["spark"]), match("keep/other.test", tags=["quiet"])]
            """,
            "test/keep/spark.test": "",
            "test/keep/other.test": "",
            "test/outside/far.test": "",
        },
    )
    bare, why_not = apply(cases, Selection(), proj)
    assert ids(bare) == ["keep/other.test::other"]  # the default set's own skip already removed spark
    assert why_not["keep/spark.test::spark"] == "skip tag=spark  <- set 'default'"
    only_skip, why_not = apply(cases, Selection(skip=["tag=quiet"]), proj)
    assert ids(only_skip) == []  # base stays the default set's paths, not every case
    assert why_not["outside/far.test::far"] == "outside the given paths"


def test_a_lone_skip_keeps_in_default_set_false_cases_out(tmp_path):
    cases, proj = tree(
        tmp_path,
        {
            "test/ducktestconf.py": """
                from ducktest5 import match
                RULES = [
                    match("special.test", in_default_set=False, tags=["special"]),
                    match("normal.test", tags=["normal"]),
                    match("quiet.test", tags=["quiet"]),
                ]
            """,
            "test/special.test": "",
            "test/normal.test": "",
            "test/quiet.test": "",
        },
    )
    selected, why_not = apply(cases, Selection(skip=["tag=quiet"]), proj)
    assert ids(selected) == ["normal.test::normal"]
    assert why_not["special.test::special"] == "its test is not in the default set (in_default_set=False)"


def test_a_named_sets_skip_applies_on_top_of_its_own_selection(tmp_path):
    cases, proj = tree(
        tmp_path,
        {
            "test/ducktestconf.py": """
                from ducktest5 import match
                SETS = {
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
    via_set, why_not = apply(cases, Selection(set_name="smoke"), proj)
    assert ids(via_set) == ["keep/plain.test::plain", "keep/spark.test::spark"]  # smoke's skip removes quiet, not spark
    assert why_not["keep/quiet.test::quiet"] == "skip tag=quiet  <- set 'smoke'"


def test_a_star_stays_inside_one_directory_and_two_stars_cross(tmp_path):
    cases, _ = tree(
        tmp_path,
        {
            "test/ducktestconf.py": "from ducktest5 import match\nRULES = [match('*.test', tags=['top']), match('**/*.test', tags=['any']), match('sub/**', tags=['below'])]",
            "test/x.test": "",
            "test/sub/deep/y.test": "",
        },
    )
    tags = {c.test.path: c.test.tags for c in cases}
    assert tags == {"test/x.test": ["top", "any"], "test/sub/deep/y.test": ["any", "below"]}


def test_an_excluded_directory_is_not_scanned_and_its_configuration_is_not_loaded(tmp_path):
    cases, project = tree(
        tmp_path,
        {
            "test/ducktestconf.py": "EXCLUDE = ['old']",
            "test/a.test": "",
            "test/old/b.test": "",
            "test/old/ducktestconf.py": "from ducktest5 import match\nRULES = [match('gone.test', tags=['x'])]",
        },
    )
    assert ids(cases) == ["a.test::a"]
    assert project.excluded_bodies(str(tmp_path)) == ["test/old/b.test"]


def test_only_a_file_with_test_bodies_is_imported_and_a_broken_one_says_which(tmp_path):
    cases, _ = tree(
        tmp_path, {"test/a.test": "", "test/helper.py": "raise RuntimeError('a helper must not run at scan')"}
    )
    assert ids(cases) == ["a.test::a"]
    with pytest.raises(UsageError, match=r"broken\.py could not be imported: ModuleNotFoundError"):
        tree(
            tmp_path / "2",
            {"test/broken.py": "import no_such_module\nfrom ducktest5 import test\n@test()\ndef t(ctx): pass"},
        )


def test_a_test_can_import_a_helper_beside_it(tmp_path):
    cases, _ = tree(
        tmp_path,
        {
            "test/helper.py": "X = 1",
            "test/t.py": "from helper import X\nfrom ducktest5 import test\n@test()\ndef t(ctx): pass",
        },
    )
    assert ids(cases) == ["t.py::t"]


def test_two_tests_of_one_file_name_get_their_own_directory(tmp_path):
    body = "from ducktest5 import test\n@test()\ndef t(ctx): pass"
    cases, _ = tree(tmp_path, {"test/a/same.py": body, "test/b/same.py": body})
    assert len({c.dir_name for c in cases}) == 2


def test_a_path_names_its_paired_file_and_nothing_wider(project):
    assert ids(select(project, paths=["test/b/paired.test"])[0]) == [
        "b/paired.py::body[cmt]",
        "b/paired.py::body[plain]",
    ]
    assert ids(select(project, paths=["test/a/y.test_slow"])[0]) == ["a/y.test_slow::y"]  # named exactly: asked for
    with pytest.raises(UsageError, match="no test has a case"):
        select(project, select=["case=cmtt"])


def test_retries_is_a_rule_field_and_a_whole_number(tmp_path):
    files = {"test/ducktestconf.py": "from ducktest5 import match\nRULES = [match('net.test', retries=2)]"}
    cases, _ = tree(tmp_path, {**files, "test/net.test": "", "test/plain.test": ""})
    assert [(c.test.name, c.test.retries) for c in cases] == [("net", 2), ("plain", 0)]
    with pytest.raises(UsageError, match="retries=-1; it is a whole number, 0 or more"):
        tree(tmp_path, {"test/ducktestconf.py": "from ducktest5 import default\nRULES = [default(retries=-1)]"})
