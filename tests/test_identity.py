"""Self-tests for the driver-owned CATALOG/SCHEMA/TABLE env vocabulary (`ducktest.identity`,
RESOURCE-PLANNING.md phase 7) -- promoted verbatim from UC's `test/py/uc/identity.py`. Pure,
offline: no pytest run needed.
"""

from ducktest.identity import TableRef, build_env, env_key


def test_table_ref_fqn():
    ref = TableRef(key="simple_table", catalog="cat", schema="sch", table="simple_table")
    assert ref.fqn == "cat.sch.simple_table"
    assert ref.access == "ro"  # default


def test_env_key_normalizes_non_alnum_and_uppercases():
    assert env_key("simple_table") == "SIMPLE_TABLE"
    assert env_key("my-table.v2") == "MY_TABLE_V2"
    assert env_key("already_UPPER") == "ALREADY_UPPER"


def test_build_env_empty_refs():
    assert build_env([]) == {}


def test_build_env_single_ref_gets_bare_and_namespaced_vars():
    ref = TableRef(key="simple_table", catalog="cat", schema="sch", table="simple_table")
    env = build_env([ref])
    assert env == {
        "SIMPLE_TABLE_CATALOG": "cat",
        "SIMPLE_TABLE_SCHEMA": "sch",
        "SIMPLE_TABLE_TABLE": "simple_table",
        "SIMPLE_TABLE": "cat.sch.simple_table",
        "CATALOG": "cat",
        "SCHEMA": "sch",
        "TABLE": "simple_table",
    }


def test_build_env_multiple_refs_first_is_primary_by_default():
    a = TableRef(key="a", catalog="cat", schema="sch", table="a")
    b = TableRef(key="b", catalog="cat", schema="sch", table="b")
    env = build_env([a, b])
    # both namespaced
    assert env["A"] == "cat.sch.a"
    assert env["B"] == "cat.sch.b"
    # primary defaults to the first ref
    assert (env["CATALOG"], env["SCHEMA"], env["TABLE"]) == ("cat", "sch", "a")


def test_build_env_primary_selectable_by_ref_or_key():
    a = TableRef(key="a", catalog="cat", schema="sch", table="a")
    b = TableRef(key="b", catalog="cat", schema="sch", table="b")
    by_ref = build_env([a, b], primary=b)
    by_key = build_env([a, b], primary="b")
    assert by_ref["TABLE"] == by_key["TABLE"] == "b"


def test_build_env_key_with_dots_and_dashes_is_env_safe():
    ref = TableRef(key="my-table.v2", catalog="cat", schema="sch", table="my-table.v2")
    env = build_env([ref])
    assert "MY_TABLE_V2" in env
    assert env["MY_TABLE_V2"] == "cat.sch.my-table.v2"  # the FQN value itself is untouched
