"""Sessions: a live engine handle that takes a test's inputs, its identity, and its lifecycle."""

import pytest

from ducktest5.binary import UsageError
from ducktest5.resources import RW, CredentialDef, Opener, SessionDef, StorageDef, check_contract, env


class Live:
    """An engine handle that records what it was told, in order."""

    def __init__(self):
        self.calls, self.conf = [], {}

    def begin_test(self):
        self.calls.append("begin_test")

    def add_preamble(self, sql):
        self.calls.append(sql)

    def set_conf(self, key, value):
        self.conf[key] = value

    def close(self):
        self.calls.append("close")


def opened(definition, dialect="duckdb", inputs=None):
    """The session and every live handle it opened, newest last."""
    made = []

    def open_live(d):
        made.append(Live())
        return made[-1]

    return definition.make(inputs, opener=Opener(dialect, open_live)), made


# -----------------------------------------------------------------------------
# Identity
#


def test_identity_is_the_content_not_the_label_scope_or_timeout():
    one = SessionDef("duck", requires=["delta"])
    assert one.identity() == SessionDef("other", requires=["delta"], scope="test", timeout=5).identity()
    assert one.identity() != SessionDef("duck", requires=["httpfs"]).identity()
    assert one.identity() != SessionDef("duck", engine="spark", requires=["delta"]).identity()


def test_identity_follows_the_inputs_and_the_env_it_is_bound_to(tmp_path):
    storage = StorageDef("local", str(tmp_path))
    bound = SessionDef("duck", inputs=[storage])
    assert bound.identity({"local": "a"}) != bound.identity({"local": "b"})
    assert SessionDef("duck").identity() != SessionDef("duck").with_(env={"AWS_PROFILE": None}).identity()


def test_an_unknown_scope_is_a_usage_error():
    with pytest.raises(UsageError, match="scope 'run'"):
        SessionDef("duck", scope="run")


# -----------------------------------------------------------------------------
# Lifecycle
#


def test_acquire_opens_then_every_test_gets_a_fresh_instance_and_the_preamble_again():
    session, made = opened(SessionDef("duck", requires=["delta"]))
    for _ in range(2):
        session.release(session.acquire(RW), RW)
    assert len(made) == 1
    assert made[0].calls == ["begin_test", "LOAD delta;"] * 2


def test_after_a_failed_test_the_session_is_closed_and_opened_again():
    session, made = opened(SessionDef("duck"))
    session.release(session.acquire(RW), RW, failed=True)
    session.release(session.acquire(RW), RW)
    assert [live.calls for live in made] == [["begin_test", "close"], ["begin_test"]]


def test_a_preamble_that_fails_leaves_the_session_free_for_the_next_test():
    session, made = opened(SessionDef("duck", requires=["nope"]))
    fail = [True]

    def add_preamble(sql):
        if fail.pop():
            raise RuntimeError("no such extension")

    session.start()
    made[0].add_preamble = add_preamble
    with pytest.raises(RuntimeError):
        session.acquire(RW)
    fail.append(False)
    assert session.acquire(RW) is session


def test_session_contract():
    check_contract(lambda run_token: opened(SessionDef("duck"))[0])


# -----------------------------------------------------------------------------
# The preamble, delivered
#


@pytest.fixture
def minio_inputs(monkeypatch):
    monkeypatch.setenv("DT_KEY", "admin")
    monkeypatch.setenv("DT_SECRET", "password")
    storage = StorageDef("minio", "s3://ducktest/delta", endpoint="http://172.18.0.1:9999")
    keys = {"AWS_ACCESS_KEY_ID": "DT_KEY", "AWS_SECRET_ACCESS_KEY": "DT_SECRET"}
    credential = CredentialDef("minio-key", sources=[env(keys)], scope="minio")
    return [storage, credential], {"minio": storage.make(), "minio-key": credential.make()}


def test_a_duckdb_session_sends_its_scoped_secret_as_sql(minio_inputs):
    defs, inputs = minio_inputs
    session, made = opened(SessionDef("duck", requires=["httpfs"], inputs=defs), "duckdb", inputs)
    session.acquire(RW)
    assert made[0].calls == [
        "begin_test",
        "LOAD httpfs;\n"
        "CREATE SECRET ducktest_minio (TYPE 's3', KEY_ID 'admin', SECRET 'password', ENDPOINT '172.18.0.1:9999', "
        "USE_SSL false, URL_STYLE 'path', SCOPE 's3://ducktest/delta');",
    ]


def test_a_spark_session_sets_the_buckets_settings_at_runtime(minio_inputs):
    defs, inputs = minio_inputs
    session, made = opened(SessionDef("spark", engine="spark", inputs=defs), "spark", inputs)
    session.acquire(RW)
    assert made[0].calls == ["begin_test"]
    assert made[0].conf["fs.s3a.bucket.ducktest.secret.key"] == "password"
    assert made[0].conf["fs.s3a.bucket.ducktest.endpoint"] == "http://172.18.0.1:9999"
