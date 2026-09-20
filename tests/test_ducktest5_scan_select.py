"""ducktest5's scan, configuration files and selection, on a small tree made for each test."""

import textwrap

import pytest

from ducktest5.binary import UsageError
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
        "test/a/x.test::x[backend=oss]",
        "test/a/x.test::x[backend=dbx]",
        "test/a/y.test_slow::y",
        "test/b/one.test::one",
        "test/b/paired.py::body[cmt]",
        "test/b/paired.py::body[plain]",
        "test/b/solo.py::solo",
    ]
    by_id = {c.id: c for c in cases}
    assert by_id["test/b/paired.py::body[cmt]"].test.type == "paired"
    assert by_id["test/b/paired.py::body[cmt]"].test.body_file == "test/b/paired.test"
    assert by_id["test/b/solo.py::solo"].test.type == "python"


def test_rules_apply_from_the_root_down_and_the_test_itself_last(project):
    x = next(c for c in project[0] if c.id == "test/a/x.test::x[backend=oss]")
    assert x.test.tags == ["all", "a"]
    assert x.values == {"region": "us", "catalog": "duck"}  # the deeper file replaced `region`; the variant added
    assert x.test.timeout == 5  # a single value: the last rule that set it
    assert any("a/ducktestconf.py: match 'x.test'" in line for line in x.test.why)
    paired = next(c for c in project[0] if c.id == "test/b/paired.py::body[cmt]")
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
    assert "test/a/y.test_slow::y" not in ids(selected) and len(selected) == 6
    assert "--slow" in why_not["test/a/y.test_slow::y"]
    assert "test/a/y.test_slow::y" in ids(select(project, slow=True)[0])
    assert ids(select(project, select=["tag=slow"])[0]) == ["test/a/y.test_slow::y"]


def test_paths_are_a_union_and_selects_must_all_hold(project):
    assert len(select(project, paths=["test/a/x.test", "test/b/one.test"])[0]) == 3
    assert ids(select(project, paths=["test/a"], select=["backend=oss"])[0]) == ["test/a/x.test::x[backend=oss]"]
    assert ids(select(project, select=["not:tag=a", "type=sqllogic"])[0]) == ["test/b/one.test::one"]
    assert ids(select(project, select=["case=cmt,plain"])[0]) == [
        "test/b/paired.py::body[cmt]",
        "test/b/paired.py::body[plain]",
    ]


def test_a_set_is_paths_and_selects_with_a_name(project):
    assert ids(select(project, set_name="smoke")[0]) == [
        "test/a/x.test::x[backend=oss]",
        "test/a/x.test::x[backend=dbx]",
        "test/b/one.test::one",
    ]
    assert ids(select(project, set_name="cmt")[0]) == ["test/b/paired.py::body[cmt]"]


def test_an_id_is_taken_verbatim(project):
    assert ids(select(project, ids=["test/b/paired.py::body[plain]"])[0]) == ["test/b/paired.py::body[plain]"]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"select": ["tag=smok"]}, "no scanned case has tag"),
        ({"select": ["backend=gcs"]}, "axis 'backend' has no value"),
        ({"select": ["backnd=oss"]}, "unknown key 'backnd'"),
        ({"select": ["slow"]}, "write key=value"),
        ({"paths": ["test/nowhere"]}, "holds no test"),
        ({"ids": ["test/a/x.test::x"]}, "no such case"),
        ({"set_name": "nightly"}, "the project has sets"),
    ],
)
def test_a_selection_that_can_never_match_is_an_error(project, kwargs, message):
    with pytest.raises(UsageError, match=message):
        select(project, **kwargs)


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
    assert ids(cases) == ["test/a.test::a"]
    assert project.excluded_bodies(str(tmp_path)) == ["test/old/b.test"]


def test_only_a_file_with_test_bodies_is_imported_and_a_broken_one_says_which(tmp_path):
    cases, _ = tree(
        tmp_path, {"test/a.test": "", "test/helper.py": "raise RuntimeError('a helper must not run at scan')"}
    )
    assert ids(cases) == ["test/a.test::a"]
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
    assert ids(cases) == ["test/t.py::t"]


def test_two_tests_of_one_file_name_get_their_own_directory(tmp_path):
    body = "from ducktest5 import test\n@test()\ndef t(ctx): pass"
    cases, _ = tree(tmp_path, {"test/a/same.py": body, "test/b/same.py": body})
    assert len({c.dir_name for c in cases}) == 2


def test_a_path_names_its_paired_file_and_nothing_wider(project):
    assert ids(select(project, paths=["test/b/paired.test"])[0]) == [
        "test/b/paired.py::body[cmt]",
        "test/b/paired.py::body[plain]",
    ]
    assert ids(select(project, paths=["test/a/y.test_slow"])[0]) == [
        "test/a/y.test_slow::y"
    ]  # named exactly: asked for
    with pytest.raises(UsageError, match="no test has a case"):
        select(project, select=["case=cmtt"])


def test_retries_is_a_rule_field_and_a_whole_number(tmp_path):
    files = {"test/ducktestconf.py": "from ducktest5 import match\nRULES = [match('net.test', retries=2)]"}
    cases, _ = tree(tmp_path, {**files, "test/net.test": "", "test/plain.test": ""})
    assert [(c.test.name, c.test.retries) for c in cases] == [("net", 2), ("plain", 0)]
    with pytest.raises(UsageError, match="retries=-1; it is a whole number, 0 or more"):
        tree(tmp_path, {"test/ducktestconf.py": "from ducktest5 import default\nRULES = [default(retries=-1)]"})
