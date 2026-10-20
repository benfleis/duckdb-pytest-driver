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


def test_a_reference_to_a_body_name_nothing_declares_is_an_error_where_the_body_names_it(tmp_path):
    body = "from ducktest import test\n\n@test(storage='s3_lak')\ndef own(ctx):\n    pass\n"
    with pytest.raises(UsageError, match=r"own.py:3: storage='s3_lak' is not declared; did you mean 's3_lake'\?"):
        tree(tmp_path, conf('S3_LAKE = StorageDef("s3_lake", "/l")\nproject = Project()', **{"test/own.py": body}))


def test_an_engine_is_listed_not_reached(tmp_path):
    body = 'SPARE = DuckDBEngineDef("spare")\nproject = Project(rules=[default(engines=[SPARE])])'
    with pytest.raises(
        UsageError,
        match=r"engine 'spare' \(declared at ducktest_conf.py:\d+\) is not one of the project's engines; "
        r"list it in Project\(engines=\[...\]\)",
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
        "storage 'spare' is declared and nothing reaches it; name it in a rule, a matrix value or @test(...), "
        "or remove it",
        "container 'loose' is declared and not used; bind it in AzureServiceDef(containers=[...]), or remove it",
        "storage 'lake' is listed in Project(storages=[...]) and nothing reaches it; name it in a rule, "
        "a matrix value or @test(...), or take it out of the list",
        "storage 'blobs' is listed in Project(storages=[...]) and nothing reaches it; name it in a rule, "
        "a matrix value or @test(...), or take it out of the list",
    ]
    assert "storage 'spare' is declared and nothing reaches it" in caplog.text


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


# -----------------------------------------------------------------------------
# The declared set: what the rules, the matrix values and the bodies reach
#

CHAIN = """
LOCAL_S3 = S3ServiceDef.rustfs("local_s3")
LOCAL_LAKE = LOCAL_S3.storage("local_lake", path="lake")
LOCAL_KEYS = LOCAL_S3.credential("local_keys", scopes=[LOCAL_LAKE])
OTHER_LAKE = LOCAL_S3.storage("other_lake", path="other")
"""


def declared(project):
    return {kind: sorted(getattr(project, kind)) for kind in ("services", "storages", "credentials")}


@pytest.mark.parametrize("given", ["LOCAL_LAKE", '"local_lake"'], ids=["object", "name"])
def test_a_storage_a_rule_names_is_declared_and_pulls_its_service_and_its_credential(tmp_path, given):
    _, project = tree(tmp_path, conf(CHAIN + f"project = Project(rules=[default(storage={given})])"))
    assert declared(project) == {"services": ["local_s3"], "storages": ["local_lake"], "credentials": ["local_keys"]}
    assert [w.split(": ", 1)[1].split(";")[0] for w in project.unused] == [
        "storage 'other_lake' is declared and nothing reaches it"
    ]


def test_a_matrix_value_reaches_what_it_names(tmp_path):
    body = CHAIN + 'project = Project(matrix={"storage": {"s3": OTHER_LAKE}}, rules=[default(matrix=["storage"])])'
    _, project = tree(tmp_path, conf(body))
    assert declared(project) == {"services": ["local_s3"], "storages": ["other_lake"], "credentials": []}


def test_a_credential_pulls_the_services_among_its_inputs(tmp_path):
    body = CHAIN + 'project = Project(rules=[default(credential="local_keys")])'
    _, project = tree(tmp_path, conf(body))
    assert declared(project) == {"services": ["local_s3"], "storages": [], "credentials": ["local_keys"]}


def test_a_template_in_a_value_reaches_what_it_names(tmp_path):
    body = 'HTTP = HttpServerDef("http")\nproject = Project(rules=[default(values={"U": "{http.url}"})])'
    _, project = tree(tmp_path, conf(body))
    assert declared(project)["services"] == ["http"] and project.unused == []


def test_a_body_reaches_a_storage_no_conf_names_and_it_is_not_unused(tmp_path, monkeypatch, capsys):
    body = "from ducktest import test\n\n@test(storage='other_lake')\ndef own(ctx):\n    pass\n"
    lake = 'LOCAL_S3 = S3ServiceDef.rustfs("local_s3")\nOTHER_LAKE = LOCAL_S3.storage("other_lake")\n'
    tests, project = tree(tmp_path, conf(lake + "project = Project()", **{"test/own.py": body}), strict=True)
    assert declared(project) == {"services": ["local_s3"], "storages": ["other_lake"], "credentials": []}
    assert project.unused == []
    assert [t.inputs[0][0].name for t in tests if t.name == "own.py::own"] == ["other_lake"]


def test_a_listed_storage_nothing_reaches_still_loads_and_warns(tmp_path):
    _, project = tree(tmp_path, conf('project = Project(storages=[StorageDef("lake", "/l")])'))
    assert list(project.storages) == ["lake"]
    assert [w.split(": ", 1)[1].split(";")[0] for w in project.unused] == [
        "storage 'lake' is listed in Project(storages=[...]) and nothing reaches it"
    ]


def test_strict_makes_a_listed_storage_nothing_reaches_an_error(tmp_path):
    with pytest.raises(UsageError, match=r"--strict: .*\n  ducktest_conf.py:\d+: storage 'lake' is listed"):
        tree(tmp_path, conf('project = Project(storages=[StorageDef("lake", "/l")])'), strict=True)


def test_a_matrix_parameter_no_test_runs_over_is_a_warning(tmp_path):
    body = 'project = Project(matrix={"client": {"curl": {"values": {"C": "curl"}}}})'
    _, project = tree(tmp_path, conf(body))
    assert project.unused == [
        "ducktest_conf.py: matrix parameter 'client': no rule or test runs over it; "
        "give matrix=['client'] where tests need it, or remove it"
    ]


def test_a_listed_engine_no_test_drives_is_a_warning(tmp_path):
    body = 'project = Project(engines=[DuckDBEngineDef("duck"), DuckDBEngineDef("spare")], rules=[default(engines=["duck"])])'
    _, project = tree(tmp_path, conf(body))
    assert [w.split(": ", 1)[1].split(";")[0] for w in project.unused] == [
        "engine 'spare' is listed and no test drives it"
    ]


@pytest.mark.parametrize("given", ['settings={"s3_endpoint": "{http.url}"}', 'init_sql="SELECT \'{http.url}\'"'])
def test_a_template_in_settings_or_init_sql_reaches_what_it_names(tmp_path, given):
    _, project = tree(tmp_path, conf(f'HTTP = HttpServerDef("http")\nproject = Project(rules=[default({given})])'))
    assert declared(project)["services"] == ["http"]


def test_cleanup_reads_no_body_so_every_declaration_counts(tmp_path):
    from ducktest.conf import load_project

    tree(tmp_path, conf(CHAIN + "project = Project()"))
    project = load_project(str(tmp_path / "test"))
    project.declare(everything=True)
    assert declared(project) == {
        "services": ["local_s3"],
        "storages": ["local_lake", "other_lake"],
        "credentials": ["local_keys"],
    }
    assert project.unused == []
