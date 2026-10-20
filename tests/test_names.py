"""The object-style conf: `Project(...)` and `Rules(...)` found by a scan of a module, names, reference
slots that take a name or the object, and the checks over the names of one load."""

import logging
import textwrap

import pytest

from ducktest.binary import UsageError
from ducktest.cli import main
from ducktest.conf import Project, Rules, match
from ducktest.scan import scan

HEAD = """
    from ducktest import AzureServiceDef, DuckDBEngineDef, HttpServerDef, Project, Rules, S3CredentialDef
    from ducktest import S3ServiceDef, StorageDef, bucket, container, default, from_env, match
"""


def tree(tmp_path, files, strict=False):
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(HEAD) + textwrap.dedent(body) if name.endswith("ducktest_conf.py") else body)
    return scan(str(tmp_path / "test"), working_dir=str(tmp_path), strict=strict)


def conf(body, **more):
    return {"test/ducktest_conf.py": body, "test/a.test": "", **more}


# -----------------------------------------------------------------------------
# Finding the declaration
#


def test_the_root_project_is_found_under_any_name(tmp_path):
    _, project = tree(tmp_path, conf('whatever = Project(storages=[StorageDef("lake", "/tmp/lake")])'))
    assert list(project.storages) == ["lake"]


def test_two_declarations_in_one_file_are_an_error(tmp_path):
    with pytest.raises(
        UsageError, match=r"holds one = Project\(...\), two = Rules\(...\); a ducktest_conf.py holds one"
    ):
        tree(tmp_path, conf("one = Project()\ntwo = Rules()"))


def test_a_project_below_the_root_is_an_error_naming_the_fix(tmp_path):
    files = conf("project = Project()", **{"test/a/ducktest_conf.py": "project = Project()", "test/a/b.test": ""})
    with pytest.raises(
        UsageError, match=r"a/ducktest_conf.py: project = Project\(...\) belongs in the root .*rules = Rules\("
    ):
        tree(tmp_path, files)


def test_rules_in_the_root_are_an_error_naming_the_fix(tmp_path):
    with pytest.raises(
        UsageError,
        match=r"test/ducktest_conf.py: rules = Rules\(...\) belongs in a file below the root; .*Project\(rules=\[",
    ):
        tree(tmp_path, conf('rules = Rules(default(tags=["a"]))'))


def test_rules_below_the_root_apply_to_the_tests_below_that_file(tmp_path):
    files = conf(
        "project = Project()", **{"test/a/ducktest_conf.py": 'rules = Rules(default(tags=["a"]))', "test/a/b.test": ""}
    )
    tests, _ = tree(tmp_path, files)
    assert {t.name: "a" in t.tags for t in tests} == {"a.test::a": False, "a/b.test::b": True}


def test_a_file_without_a_declaration_gives_nothing_and_says_so(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="ducktest"):
        tree(tmp_path, conf("X = 1"))
    assert "holds no Project(...) or Rules(...); it gives the tests nothing" in caplog.text


def test_a_misspelled_field_fails_where_it_is_written():
    with pytest.raises(TypeError, match="unexpected keyword argument 'credential'"):
        Project(credential=[])


def test_rules_take_the_rules_themselves_not_a_list():
    with pytest.raises(UsageError, match=r"Rules\(...\) takes the rules themselves"):
        Rules([match("x.test")])


# -----------------------------------------------------------------------------
# Reference slots: a name or the object, a plain string past the call
#


def test_every_reference_slot_takes_the_object_and_gives_its_name_as_a_plain_string(tmp_path):
    tests, project = tree(
        tmp_path,
        {
            "test/ducktest_conf.py": """
                AZ = AzureServiceDef.azurite(containers=[container("data")])
                DATA = AZ.parts()[0]
                BLOBS = AZ.storage("blobs", path="lake", container=DATA)
                LAKE = StorageDef("lake", "s3://lake")
                KEYS = S3CredentialDef("keys", sources=[from_env({"K": "key_id", "S": "secret"})], scopes=[(LAKE, "ro")])
                HTTP = HttpServerDef("http")
                DUCK = DuckDBEngineDef("duck")
                project = Project(
                    engines=[DUCK],
                    services=[HTTP],
                    storages=[LAKE, BLOBS],
                    credentials=[KEYS],
                    matrix={"storage": {"lake": LAKE, "blobs": {"storage": BLOBS, "engines": [DUCK]}}},
                    rules=[match("a.test", matrix=["storage"], credential=KEYS, service=HTTP, engines=[DUCK])],
                )
            """,
            "test/a.test": "",
        },
    )
    (rule,) = project._rules_by_dir[str(tmp_path / "test")]
    given = rule.options.given
    assert (given["credential"], given["service"], given["engines"]) == ("keys", "http", ("duck",))
    assert all(type(v) is str for v in (given["credential"], given["service"], *given["engines"]))
    assert project.matrix["storage"]["lake"].given == {"storage": "lake"}
    assert project.matrix["storage"]["blobs"].given == {"storage": "blobs", "engines": ("duck",)}
    assert project.credentials["keys"].scopes == [("lake", "ro")]
    assert project.storages["blobs"].root == "az://data/lake"
    assert [t.name for t in tests] == ["a.test::a[storage=lake]", "a.test::a[storage=blobs]"]


def test_a_slot_given_an_object_of_another_kind_is_an_error(tmp_path):
    with pytest.raises(
        UsageError, match=r"storage S3CredentialDef\('keys'\); give the name of a storage, or the storage"
    ):
        tree(
            tmp_path,
            conf(
                'KEYS = S3CredentialDef("keys")\nproject = Project(credentials=[KEYS], rules=[default(storage=KEYS)])'
            ),
        )


def test_a_name_that_names_another_kind_is_an_error(tmp_path):
    with pytest.raises(UsageError, match=r"ducktest_conf.py:\d+: credential='lake' names a storage, not a credential"):
        tree(
            tmp_path, conf('project = Project(storages=[StorageDef("lake", "/l")], rules=[default(credential="lake")])')
        )


# -----------------------------------------------------------------------------
# The checks over the names of one load
#


def test_two_declarations_of_one_name_are_an_error(tmp_path):
    body = 'project = Project(storages=[StorageDef("lake", "/a")], services=[HttpServerDef("lake")])'
    with pytest.raises(
        UsageError, match=r"the name 'lake' is declared 2 times: storage at ducktest_conf.py:\d+, service at"
    ):
        tree(tmp_path, conf(body))


def test_a_derived_declaration_takes_the_place_of_the_one_it_came_from(tmp_path):
    body = 'S3 = S3ServiceDef.rustfs()\nproject = Project(services=[S3.as_existing(address="http://h:1")])'
    _, project = tree(tmp_path, conf(body))
    assert project.services["s3"].address == "http://h:1"


def test_a_reference_to_an_undeclared_name_is_an_error_with_the_closest_one(tmp_path):
    body = 'project = Project(storages=[StorageDef("s3_lake", "/l")], rules=[default(storage="s3_lak")])'
    with pytest.raises(
        UsageError, match=r"ducktest_conf.py:\d+: storage='s3_lak' is not declared; did you mean 's3_lake'\?"
    ):
        tree(tmp_path, conf(body))


def test_a_reference_by_name_to_a_declaration_that_is_not_bound_says_where_to_bind_it(tmp_path):
    body = 'LAKE = StorageDef("lake", "/l")\nproject = Project(rules=[default(storage="lake")])'
    with pytest.raises(
        UsageError,
        match=r"storage='lake' \(declared at ducktest_conf.py:\d+\) is not bound; add it to Project\(storages=\[...\]\)",
    ):
        tree(tmp_path, conf(body))


def test_a_reference_by_object_to_a_declaration_that_is_not_bound_says_where_to_bind_it(tmp_path):
    body = 'KEYS = S3CredentialDef("keys")\nproject = Project(rules=[default(credential=KEYS)])'
    with pytest.raises(
        UsageError, match=r"credential='keys' \(declared at .*\) is not bound; add it to Project\(credentials=\[...\]\)"
    ):
        tree(tmp_path, conf(body))


def test_a_container_that_is_not_given_to_its_server_says_where_to_give_it(tmp_path):
    body = 'EXTRA = container("extra")\nAZ = AzureServiceDef.azurite()\nproject = Project(storages=[AZ.storage("x", container=EXTRA)])'
    with pytest.raises(
        UsageError,
        match=r"service 'az': container 'extra' is not one of its containers; add it to AzureServiceDef\(containers=\[...\]\)",
    ):
        tree(tmp_path, conf(body))


def test_a_bucket_is_named_within_its_server(tmp_path):
    body = """
        S3 = S3ServiceDef.rustfs(buckets=[bucket("data")])
        project = Project(storages=[S3.storage("a", bucket="data"), S3.storage("b", bucket="nope")])
    """
    with pytest.raises(UsageError, match="service 's3': 'nope' is not one of its buckets"):
        tree(tmp_path, conf(body))


@pytest.mark.parametrize(
    "value, message",
    [
        ("{lake.rooot}", r"\{lake.rooot\}: a storage has no attribute 'rooot'; lake has root, scheme"),
        ("{http.port}", r"\{http.port\}: a service has no attribute 'port'; http has url, dir"),
        ("{htp.url}", r"\{htp.url\}: no resource is named 'htp'; did you mean 'http'\?"),
    ],
)
def test_an_attribute_a_value_names_is_one_its_kind_has(tmp_path, value, message):
    body = f"""
        project = Project(
            storages=[StorageDef("lake", "/l")],
            services=[HttpServerDef("http")],
            rules=[default(values={{"X": "{value}"}})],
        )
    """
    with pytest.raises(UsageError, match=r"ducktest_conf.py: default: values\['X'\]: " + message):
        tree(tmp_path, conf(body))


def test_a_storage_template_names_an_attribute_its_input_has(tmp_path):
    body = 'H = HttpServerDef("h")\nproject = Project(storages=[StorageDef("s", "{h.uri}", inputs=[H])])'
    with pytest.raises(UsageError, match=r"storage 's': root: \{h.uri\}: a service has no attribute 'uri'"):
        tree(tmp_path, conf(body))


UNUSED = """
    SPARE = StorageDef("spare", "/s")
    AZ = AzureServiceDef.azurite(containers=[container("used")])
    LOOSE = container("loose")
    project = Project(storages=[StorageDef("lake", "/l"), AZ.storage("blobs")])
"""


def test_a_declaration_nothing_uses_is_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="ducktest"):
        _, project = tree(tmp_path, conf(UNUSED))
    assert [w.split(": ", 1)[1] for w in project.unused] == [
        "storage 'spare' is declared and not used; bind it in Project(storages=[...]), or remove it",
        "container 'loose' is declared and not used; bind it in AzureServiceDef(containers=[...]), or remove it",
    ]
    assert "storage 'spare' is declared and not used" in caplog.text


def test_strict_makes_a_declaration_nothing_uses_an_error(tmp_path, monkeypatch, capsys):
    tree(tmp_path, conf(UNUSED))
    monkeypatch.chdir(tmp_path)
    assert main(["scan", "--strict"]) == 2
    err = capsys.readouterr().err
    assert "--strict: declarations that nothing uses:" in err and "storage 'spare'" in err
    for command in ("select", "run"):
        assert "--strict" in _help(command, capsys)


def _help(command, capsys):
    with pytest.raises(SystemExit):
        main([command, "--help"])
    return capsys.readouterr().out
