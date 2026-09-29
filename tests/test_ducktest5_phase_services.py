"""What a service may do across invocations: which one `provision` may leave running, how `exec`
adopts it again, and that a secret is never written to a file."""

import os
import textwrap

import pytest
import yaml

from ducktest5 import events, phases
from ducktest5.cli import main

FAKE = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_unittest.py")

CONF = """
    import os
    from dataclasses import dataclass, field
    from ducktest5 import ServiceDef, match
    from ducktest5.resources import Service

    PLACE = os.environ["FAKE_SERVICE_PLACE"]  # one file for each running instance, named for its run

    @dataclass(eq=False)
    class StoreDef(ServiceDef):
        kind = "store"
        detached = {DETACHED}
        attributes = ("url",)
        secret_attributes = ("key",)

        id: str = "store"
        address: str | None = None
        inputs: list = field(default_factory=list)
        retain: str = "never"

    class Store(Service):
        @property
        def address(self):
            return self.definition.address or os.path.join(PLACE, self.run_token)

        def start(self):
            open(self.address, "w").close()

        def stop(self):
            if os.path.exists(self.address):
                os.remove(self.address)

        def read(self):
            return {"url": self.address, "key": "from-the-environment"} if os.path.exists(self.address) else None

    StoreDef.instance = Store

    SERVICES = [StoreDef()]
    RULES = [match("s/**", bind={"store": "store"})]
"""


@pytest.fixture
def tree(tmp_path, monkeypatch):
    def make(*, detached=True):
        (tmp_path / "test/s").mkdir(parents=True)
        (tmp_path / "test/ducktest_conf.py").write_text(textwrap.dedent(CONF).replace("{DETACHED}", str(detached)))
        (tmp_path / "test/s/x.test").write_text("")
        (tmp_path / "up").mkdir()
        monkeypatch.setenv("FAKE_SERVICE_PLACE", str(tmp_path / "up"))
        monkeypatch.chdir(tmp_path)
        return tmp_path

    return make


def binaries(*more):
    return ["--duckdb-bin", FAKE, "--unittest-bin", FAKE, "--no-binary-check", *more]


def provisioned(run_dir):
    return yaml.safe_load(open(os.path.join(run_dir, phases.PROVISION_FILE)))["services"]


def outcomes(run_dir):
    return {e["id"]: e["outcome"] for e in events.read(run_dir) if e["event"] == events.TEST_END}


def plan(run_dir="apart"):
    assert main(["scan", "--run-dir", run_dir]) == 0
    assert main(["plan", "--run-dir", run_dir]) == 0


def running(tmp_path):
    return sorted(os.listdir(tmp_path / "up"))


# -----------------------------------------------------------------------------
# A service of its own invocation, and one that cannot be
#


def test_a_service_that_lives_in_the_process_may_not_be_provisioned_apart(tree, capsys):
    tree(detached=False)
    plan()
    assert main(["provision", "--run-dir", "apart"]) == 2
    said = capsys.readouterr().err
    assert "lives in the process that starts it" in said and "exec --run-dir DIR --provision" in said


def test_provision_leaves_a_service_up_and_exec_adopts_it_at_its_address(tree, tmp_path):
    tree()
    plan()
    assert main(["provision", "--run-dir", "apart"]) == 0
    assert running(tmp_path) == ["apart"]
    record = provisioned("apart")["store"]
    assert (record["mode"], record["owned"], record["detached"]) == ("managed", True, True)
    assert record["address"] == str(tmp_path / "up" / "apart")
    assert record["attributes"] == {"url": record["address"], "key": "***"}

    assert main(["exec", "--run-dir", "apart", *binaries()]) == 0
    assert outcomes("apart") == {"s/x.test::x": "passed"}
    assert running(tmp_path) == ["apart"]  # exec adopts; it never ends what it did not make
    assert provisioned("apart")["store"] == record  # and cleanup still owns it


def test_cleanup_ends_what_provision_started_and_may_run_twice(tree, tmp_path, capsys):
    tree()
    plan()
    assert main(["provision", "--run-dir", "apart"]) == 0
    assert main(["exec", "--run-dir", "apart", *binaries()]) == 0
    assert main(["cleanup", "--run-dir", "apart"]) == 0
    assert running(tmp_path) == []
    assert "stopped: store" in capsys.readouterr().out
    assert main(["cleanup", "--run-dir", "apart"]) == 0


def test_exec_with_no_service_provisioned_blocks_the_tests_and_says_what_to_run(tree, capsys):
    tree()
    plan()
    assert main(["exec", "--run-dir", "apart", *binaries()]) == 1
    assert outcomes("apart") == {"s/x.test::x": "blocked"}
    said = capsys.readouterr().out
    assert "ducktest5 provision --run-dir DIR" in said and "exec --provision" in said


def test_exec_can_provision_here_as_run_does(tree, tmp_path):
    tree()
    plan()
    assert main(["exec", "--run-dir", "apart", "--provision", *binaries()]) == 0
    assert outcomes("apart") == {"s/x.test::x": "passed"}
    assert (provisioned("apart")["store"]["mode"], provisioned("apart")["store"]["owned"]) == ("managed", False)
    assert running(tmp_path) == []  # what exec started, exec ends


def test_exec_makes_a_service_of_its_own_process_and_records_it(tree, tmp_path):
    tree(detached=False)
    plan()
    assert main(["exec", "--run-dir", "apart", *binaries()]) == 0
    assert outcomes("apart") == {"s/x.test::x": "passed"}
    assert provisioned("apart")["store"]["mode"] == "managed"
    assert running(tmp_path) == []


def test_an_existing_service_is_recorded_as_not_owned_and_adopted_by_exec(tree, tmp_path):
    tree()
    plan()
    address = tmp_path / "up" / "theirs"
    address.write_text("")
    assert main(["provision", "--run-dir", "apart", "--existing-service", f"store={address}"]) == 0
    record = provisioned("apart")["store"]
    assert (record["mode"], record["owned"], record["address"]) == ("existing", False, str(address))
    assert main(["exec", "--run-dir", "apart", *binaries()]) == 0
    assert main(["cleanup", "--run-dir", "apart"]) == 0
    assert running(tmp_path) == ["theirs"]


# -----------------------------------------------------------------------------
# A secret is never written down
#


def test_a_secret_attribute_is_masked_in_the_file(tree):
    tree()
    plan()
    assert main(["provision", "--run-dir", "apart"]) == 0
    text = open(os.path.join("apart", phases.PROVISION_FILE)).read()
    assert "from-the-environment" not in text and "***" in text
