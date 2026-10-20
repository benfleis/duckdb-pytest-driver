"""The object-style conf: `Project(...)` and `Rules(...)` found by a scan of a module, names, reference
slots that take a name or the object, and the checks over the names of one load."""

import logging
import sys
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


def test_a_template_in_a_value_reaches_what_the_test_binds(tmp_path):
    body = 'HTTP = HttpServerDef("http")\nproject = Project(rules=[default(service=HTTP, values={"U": "{http.url}"})])'
    _, project = tree(tmp_path, conf(body), strict=True)
    assert declared(project)["services"] == ["http"] and project.unused == []


def test_a_template_is_held_through_a_credential_of_its_service(tmp_path):
    body = CHAIN + 'project = Project(rules=[default(credential=LOCAL_KEYS, values={"E": "{local_s3.host_port}"})])'
    tests, _ = tree(tmp_path, conf(body))
    assert [t.name for t in tests] == ["a.test::a"]


@pytest.mark.parametrize(
    "given, shown",
    [
        ('values={"U": "{http.url}"}', "values U"),
        ('settings={"s3_endpoint": "{http.url}"}', "settings s3_endpoint"),
        ("init_sql=\"SELECT '{http.url}'\"", r"init_sql\[0\]"),
    ],
)
def test_a_template_that_names_what_the_test_does_not_hold_is_an_error(tmp_path, given, shown):
    with pytest.raises(
        UsageError,
        match=rf"^a.test::a: ducktest_conf.py: default: {shown} names '\{{http.url\}}' but the test does not hold "
        r"service 'http'; bind it \(service=http\) or bind a storage or credential of it$",
    ):
        tree(tmp_path, conf(f'HTTP = HttpServerDef("http")\nproject = Project(rules=[default({given})])'))


@pytest.mark.parametrize(
    "given, label",
    [
        ('settings={"s3_endpoint": "{htp.url}"}', r"settings\['s3_endpoint'\]"),
        ("init_sql=\"SELECT '{htp.url}'\"", r"init_sql\[0\]"),
    ],
)
def test_an_unknown_name_in_settings_or_init_sql_is_an_error(tmp_path, given, label):
    body = f'HTTP = HttpServerDef("http")\nproject = Project(rules=[default(service=HTTP, {given})])'
    with pytest.raises(
        UsageError, match=rf"default: {label}: \{{htp.url\}}: no resource is named 'htp'; did you mean 'http'\?"
    ):
        tree(tmp_path, conf(body))


def test_a_storage_template_reaches_an_input_given_by_name(tmp_path):
    body = """
        HTTP = HttpServerDef("http")
        SERVED = StorageDef("served", "{http.url}", inputs=["http"])
        project = Project(rules=[default(storage=SERVED, values={"U": "{http.url}"})])
    """
    _, project = tree(tmp_path, conf(body), strict=True)
    assert declared(project) == {"services": ["http"], "storages": ["served"], "credentials": []}


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


def cleanup_project(tmp_path, monkeypatch):
    from ducktest import phases
    from ducktest.conf import load_project

    monkeypatch.chdir(tmp_path)
    assert main(["scan", "--run-dir", "rd"]) == 0
    project = load_project(str(tmp_path / "test"))
    project.declare_by_name(*phases.used_by("rd"))
    return project


def test_cleanup_takes_what_the_run_used_from_its_records_also_what_only_a_body_binds(tmp_path, monkeypatch):
    body = "from ducktest import test\n\n@test(storage='other_lake')\ndef own(ctx):\n    pass\n"
    tree(
        tmp_path,
        conf(CHAIN + 'project = Project(rules=[match("a.test", storage="local_lake")])', **{"test/own.py": body}),
    )
    project = cleanup_project(tmp_path, monkeypatch)
    assert declared(project) == {"services": ["local_s3"], "storages": ["local_lake", "other_lake"], "credentials": []}


def test_cleanup_runs_no_check_on_a_declaration_the_run_did_not_use(tmp_path, monkeypatch, capsys):
    body = """
        LOCAL = StorageDef("local_dir", "/l")
        KEYS = S3CredentialDef("keys", sources=[from_env({"K": "key_id", "S": "secret"})], scopes=[LOCAL])
        project = Project()
    """
    _, project = tree(tmp_path, conf(body))
    assert [w.split(": ", 1)[1].split(";")[0] for w in project.unused] == [
        "storage 'local_dir' is declared and nothing reaches it",
        "credential 'keys' is declared and nothing reaches it",
    ]
    assert declared(cleanup_project(tmp_path, monkeypatch)) == {"services": [], "storages": [], "credentials": []}
    assert main(["cleanup", "--run-dir", "rd"]) == 0


def test_cleanup_logs_a_name_the_run_used_that_no_conf_declares_now(tmp_path, monkeypatch, capsys):
    tree(tmp_path, conf(CHAIN + 'project = Project(rules=[default(storage="other_lake")])'))
    monkeypatch.chdir(tmp_path)
    assert main(["scan", "--run-dir", "rd"]) == 0
    (tmp_path / "test/ducktest_conf.py").write_text(textwrap.dedent(HEAD) + "project = Project()\n")
    capsys.readouterr()
    assert main(["cleanup", "--run-dir", "rd"]) == 0
    said = capsys.readouterr().err
    assert "storage 'other_lake': the run used it and no ducktest_conf.py declares it now; not cleaned" in said


# -----------------------------------------------------------------------------
# A declaration belongs to a conf file
#


def test_a_def_made_in_a_body_is_an_error_at_its_line(tmp_path):
    body = "from ducktest import S3ServiceDef, test\n\nBODY_S3 = S3ServiceDef.rustfs('body_s3')\n"
    with pytest.raises(
        UsageError,
        match=r"^own.py:3: S3ServiceDef\('body_s3'\) is declared in a test file; "
        r"declare it in ducktest_conf.py and bind it with @test\(...\)$",
    ):
        tree(tmp_path, conf("project = Project()", **{"test/own.py": body + "\n@test()\ndef own(ctx):\n    pass\n"}))


def test_a_def_made_in_a_helper_a_body_imports_is_an_error_at_the_helpers_line(tmp_path):
    helper = "from ducktest import StorageDef\n\nSHARED = StorageDef('shared_lake', '/s')\n"
    body = "import helper_makes_a_def\nfrom ducktest import test\n\n@test()\ndef own(ctx):\n    pass\n"
    files = conf("project = Project()", **{"test/helper_makes_a_def.py": helper, "test/own.py": body})
    with pytest.raises(
        UsageError, match=r"^helper_makes_a_def.py:3: StorageDef\('shared_lake'\) is declared in a test"
    ):
        tree(tmp_path, files)


def test_a_body_that_imports_the_conf_is_told_to_name_what_it_needs(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "ducktest_conf", raising=False)
    body = "from ducktest import test\nfrom ducktest_conf import LOCAL_LAKE\n\n@test(storage=LOCAL_LAKE)\ndef own(ctx):\n    pass\n"
    with pytest.raises(
        UsageError,
        match=r"^own.py:2: imports ducktest_conf.py; a body gets the project's objects by name, not by import$",
    ):
        tree(tmp_path, conf(CHAIN + "project = Project()", **{"test/own.py": body}))


def test_a_helper_that_makes_no_def_is_shared_by_two_bodies(tmp_path):
    helper = "ROWS = 3\n"
    body = "from shared_rows import ROWS\nfrom ducktest import test\n\n@test(storage='local_lake')\ndef {0}(ctx):\n    pass\n"
    files = conf(
        CHAIN + "project = Project()",
        **{"test/shared_rows.py": helper, "test/one.py": body.format("one"), "test/two.py": body.format("two")},
    )
    tests, _ = tree(tmp_path, files)
    assert {"one.py::one", "two.py::two"} <= {t.name for t in tests}


# -----------------------------------------------------------------------------
# Two credentials on one storage: chosen by the mode the test needs
#

PAIR = """
LOCAL_S3 = S3ServiceDef.rustfs("local_s3")
LOCAL_LAKE = LOCAL_S3.storage("local_lake", path="lake")
RW_KEYS = LOCAL_S3.credential("rw_keys", scopes=[LOCAL_LAKE])
RO_KEYS = LOCAL_S3.credential("ro_keys", scopes=[(LOCAL_LAKE, "ro")])
"""


def test_a_test_that_reads_a_storage_gets_its_ro_credential_and_one_that_writes_its_rw_one(tmp_path):
    rules = 'rules=[match("read.test", data=LOCAL_LAKE), match("write.test", temp=LOCAL_LAKE)]'
    files = {"test/ducktest_conf.py": PAIR + f"project = Project({rules})", "test/read.test": "", "test/write.test": ""}
    tests, _ = tree(tmp_path, files, strict=True)
    held = {t.name: [d.name for d, _ in t.inputs if d.kind == "credential"] for t in tests}
    assert held == {"read.test::read": ["ro_keys"], "write.test::write": ["rw_keys"]}


def test_two_credentials_that_may_write_a_storage_are_an_error_naming_each_and_the_fix(tmp_path):
    body = """
        LOCAL_S3 = S3ServiceDef.rustfs("local_s3")
        LOCAL_LAKE = LOCAL_S3.storage("local_lake", path="lake")
        KEYS = LOCAL_S3.credential("keys", scopes=[LOCAL_LAKE])
        MORE_KEYS = LOCAL_S3.credential("more_keys", scopes=[LOCAL_LAKE])
        project = Project(credentials=[KEYS], rules=[default(storage=LOCAL_LAKE)])
    """
    with pytest.raises(
        UsageError,
        match=r"^a.test::a: storage 'local_lake' <- ducktest_conf.py: default: credentials "
        r"'keys' \(ducktest_conf.py:\d+\), 'more_keys' \(ducktest_conf.py:\d+, pulled in by its scopes=\) are scoped "
        r"rw to it and the test needs to write to it; name one with credential= on the rule, "
        r"or set default=True on one$",
    ):
        tree(tmp_path, conf(body))
