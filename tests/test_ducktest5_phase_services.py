"""What a service may do across invocations: which one `provision` may leave running, how `exec`
finds it again, and that a secret is never read back from a file."""

import json
import os
import textwrap

import pytest
import yaml

from ducktest5 import events, phases
from ducktest5.cli import main

FAKE = os.path.join(os.path.dirname(__file__), "ducktest5_fakes", "fake_unittest.py")

CONF = """
    import os
    from ducktest5 import Service, match

    MARK = os.environ["FAKE_SERVICE_MARK"]

    def block(**o):
        return {
            "endpoint": o.get("endpoint", "file://store"),
            "secret_key": o.get("secret_key", "from-the-environment"),
        }

    def stop(b):
        if os.path.exists(MARK):
            os.remove(MARK)

    SERVICES = [
        Service(
            "store",
            block=block,
            alive=lambda b: os.path.exists(MARK),
            start=lambda b: open(MARK, "w").close(),
            stop=stop,
            detached={DETACHED},
        )
    ]
    RULES = [match("s/**", services=["store"])]
"""


@pytest.fixture
def tree(tmp_path, monkeypatch):
    def make(*, detached=True):
        (tmp_path / "test/s").mkdir(parents=True)
        (tmp_path / "test/ducktestconf.py").write_text(textwrap.dedent(CONF).replace("{DETACHED}", str(detached)))
        (tmp_path / "test/s/x.test").write_text("")
        monkeypatch.setenv("FAKE_SERVICE_MARK", str(tmp_path / "store.up"))
        monkeypatch.chdir(tmp_path)
        return tmp_path

    return make


def binaries(*more):
    return ["--duckdb-bin", FAKE, "--unittest-bin", FAKE, "--no-binary-check", *more]


def provisioned(run_dir):
    return yaml.safe_load(open(os.path.join(run_dir, phases.PROVISION_FILE)))["services"]


def outcomes(run_dir):
    return {e["id"]: e["outcome"] for e in events.read(run_dir) if e["event"] == events.CASE_END}


def plan(run_dir="apart"):
    assert main(["scan", "--run-dir", run_dir]) == 0
    assert main(["plan", "--run-dir", run_dir]) == 0


# -----------------------------------------------------------------------------
# A service of its own invocation, and one that cannot be
#


def test_a_service_that_lives_in_the_process_may_not_be_provisioned_apart(tree, capsys):
    tree(detached=False)
    plan()
    assert main(["provision", "--run-dir", "apart"]) == 2
    said = capsys.readouterr().err
    assert "lives in the process that starts it" in said and "exec --run-dir DIR --provision" in said


def test_provision_leaves_a_service_up_and_exec_finds_it_again(tree, tmp_path):
    tree()
    plan()
    assert main(["provision", "--run-dir", "apart"]) == 0
    assert (tmp_path / "store.up").exists()
    record = provisioned("apart")["store"]
    assert (record["mode"], record["owned"], record["detached"]) == ("kept", True, True)
    # the endpoint finds it again; the secret is not an override here, and would be masked if it were
    assert record["attach"] == {"endpoint": "file://store"}
    assert record["block"]["secret_key"] == "***"  # the service did not declare it: the name did

    assert main(["exec", "--run-dir", "apart", *binaries()]) == 0
    assert outcomes("apart") == {"s/x.test::x": "passed"}
    assert (tmp_path / "store.up").exists()  # exec attaches; it never stops what it did not start


def test_cleanup_stops_what_provision_started_and_may_run_twice(tree, tmp_path, capsys):
    tree()
    plan()
    assert main(["provision", "--run-dir", "apart"]) == 0
    assert main(["exec", "--run-dir", "apart", *binaries()]) == 0
    assert main(["cleanup", "--run-dir", "apart"]) == 0
    assert not (tmp_path / "store.up").exists()
    assert "stopped: store" in capsys.readouterr().out
    assert main(["cleanup", "--run-dir", "apart"]) == 0


def test_exec_with_no_service_provisioned_blocks_the_cases_and_says_what_to_run(tree, capsys):
    tree()
    plan()
    assert main(["exec", "--run-dir", "apart", *binaries()]) == 1
    assert outcomes("apart") == {"s/x.test::x": "blocked"}
    said = capsys.readouterr().out
    assert "ducktest5 provision --run-dir DIR" in said and "exec --provision" in said


def test_exec_can_provision_here_as_run_does(tree, tmp_path):
    tree(detached=False)
    plan()
    assert main(["exec", "--run-dir", "apart", "--provision", *binaries()]) == 0
    assert outcomes("apart") == {"s/x.test::x": "passed"}
    assert provisioned("apart")["store"]["mode"] == "managed"
    assert not (tmp_path / "store.up").exists()  # what exec started, exec stops


# -----------------------------------------------------------------------------
# A secret is never written down
#


def test_a_secret_is_masked_in_the_file_and_asked_for_again(tree, tmp_path, capsys):
    tree()
    plan()
    (tmp_path / "store.up").write_text("")  # it is already running: this run does not own it
    named = ["--existing-service", json.dumps({"endpoint": "file://store", "secret_key": "hunter2"})]
    named[1] = "store=" + named[1]
    assert main(["provision", "--run-dir", "apart", *named]) == 0
    text = open(os.path.join("apart", phases.PROVISION_FILE)).read()
    assert "hunter2" not in text and "***" in text
    assert provisioned("apart")["store"]["owned"] is False

    assert main(["exec", "--run-dir", "apart", *binaries()]) == 1  # the value is not in the file
    assert outcomes("apart") == {"s/x.test::x": "blocked"}
    assert "secret_key" in capsys.readouterr().out

    assert main(["exec", "--run-dir", "apart", "--force", *binaries(), *named]) == 0
    assert outcomes("apart")["s/x.test::x"] == "passed"
