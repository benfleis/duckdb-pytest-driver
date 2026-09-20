"""ducktest5's service modes: managed, existing, keep -- and what each does with start and stop."""

import pytest

from ducktest5.declare import Service
from ducktest5.provision import EXISTING, KEPT, MANAGED, attach_argument, existing_overrides, provision, release


class Fake:
    """A service that records what was done to it. `up` is whether an instance answers."""

    def __init__(self, *, up=False, comes_up=True):
        self.up, self.comes_up, self.calls = up, comes_up, []

    def service(self, **kwargs):
        def start(block):
            self.calls.append("start")
            self.up = self.comes_up

        def stop(block):
            self.calls.append("stop")
            self.up = False

        return Service(
            "store",
            block=lambda **o: {"endpoint": o.get("endpoint", "http://default"), "secret_key": "s3cr3t"},
            alive=lambda block: self.up,
            start=start,
            stop=stop,
            setup=lambda block: self.calls.append("setup"),
            secrets=("secret_key",),
            **kwargs,
        )


def test_managed_starts_sets_up_and_stops():
    fake = Fake()
    p = provision(fake.service())
    assert (p.mode, fake.calls) == (MANAGED, ["start", "setup"])
    release(p)
    assert fake.calls == ["start", "setup", "stop"]


def test_existing_is_found_set_up_and_never_stopped():
    fake = Fake(up=True)
    p = provision(fake.service(), existing={"endpoint": "http://elsewhere"})
    assert (p.mode, p.block["endpoint"], fake.calls) == (EXISTING, "http://elsewhere", ["setup"])
    release(p)
    assert "stop" not in fake.calls


def test_existing_that_does_not_answer_fails_loud_and_is_never_started():
    fake = Fake(up=False)
    with pytest.raises(RuntimeError, match="does not answer at http://elsewhere"):
        provision(fake.service(), existing={"endpoint": "http://elsewhere"})
    assert fake.calls == []


def test_keep_finds_a_running_instance_or_starts_one_and_leaves_both_running():
    found = Fake(up=True)
    p = provision(found.service(), keep=True)
    release(p)
    assert (p.mode, found.calls) == (KEPT, ["setup"])

    started = Fake(up=False)
    p = provision(started.service(), keep=True)
    release(p)
    assert (p.mode, started.calls) == (KEPT, ["start", "setup"])
    assert attach_argument(p) == """--existing-service 'store={"endpoint": "http://default"}'"""


def test_a_service_with_no_start_is_always_existing():
    fake = Fake(up=True)
    external = Service("cloud", block=lambda **o: {"endpoint": "https://cloud"}, alive=lambda block: fake.up)
    assert provision(external).mode == EXISTING


def test_an_instance_that_never_comes_up_is_stopped_again():
    fake = Fake(comes_up=False)
    with pytest.raises(RuntimeError, match="did not come up"):
        provision(fake.service(), ready_timeout_s=0.2)
    assert fake.calls == ["start", "stop"]


def test_secrets_do_not_reach_a_report():
    p = provision(Fake().service())
    assert p.redacted() == {"endpoint": "http://default", "secret_key": "***"}


@pytest.mark.parametrize(
    "entries, env, expected",
    [
        ([], None, None),
        (["store"], None, {}),
        (["store=http://h:9000"], None, {"endpoint": "http://h:9000"}),
        (['store={"endpoint": "http://h", "bucket": "b"}'], None, {"endpoint": "http://h", "bucket": "b"}),
        (["other=http://x"], None, None),
        ([], "http://from-env", {"endpoint": "http://from-env"}),
        (["store=http://cli"], "http://from-env", {"endpoint": "http://cli"}),
    ],
)
def test_how_an_existing_service_is_named(monkeypatch, entries, env, expected):
    monkeypatch.delenv("DUCKTEST_EXISTING_SERVICE_STORE", raising=False)
    if env:
        monkeypatch.setenv("DUCKTEST_EXISTING_SERVICE_STORE", env)
    assert existing_overrides("store", entries) == expected


def test_a_started_minio_is_published_and_reached_on_the_service_host(monkeypatch):
    from ducktest5.services.minio import minio_block

    assert minio_block()["endpoint"] == "http://127.0.0.1:9000"
    monkeypatch.setenv("DUCKTEST_SERVICE_HOST", "172.18.0.1")
    block = minio_block()
    assert (block["endpoint"], block["publish"], block["s3_endpoint"]) == (
        "http://172.18.0.1:9000",
        "172.18.0.1:9000",
        "172.18.0.1:9000",
    )


def test_managed_refuses_to_replace_an_instance_that_already_answers():
    fake = Fake(up=True)
    with pytest.raises(RuntimeError, match="already answers.*--existing-service store.*--keep-services"):
        provision(fake.service())
    assert fake.calls == []


def test_a_setup_that_fails_does_not_leave_a_started_instance_behind():
    fake = Fake()

    def setup(block):
        fake.calls.append("setup")
        raise RuntimeError("no boto3")

    service = Service(
        "store",
        block=lambda **o: {},
        alive=lambda b: fake.up,
        start=lambda b: (fake.calls.append("start"), setattr(fake, "up", True)),
        stop=lambda b: fake.calls.append("stop"),
        setup=setup,
    )
    with pytest.raises(RuntimeError, match="no boto3"):
        provision(service)
    assert fake.calls == ["start", "setup", "stop"]
