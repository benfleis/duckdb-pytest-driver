"""ducktest's text handling: statement splitting, and the one text form rows compare in."""

import datetime
import decimal

import pytest

from ducktest.binary import UsageError
from ducktest.resources.client import Client, ExpectationFailed, SetupFailed, SqlError, Values
from ducktest.sqltext import L, parse_rows, render_rows, split_statements, sql_values


@pytest.mark.parametrize(
    "block, statements",
    [
        ("SELECT 1", ["SELECT 1"]),
        ("SELECT 1; SELECT 2;", ["SELECT 1", "SELECT 2"]),
        ("SELECT 'a;b'; SELECT 2", ["SELECT 'a;b'", "SELECT 2"]),
        ("SELECT 'it''s;'; SELECT 2", ["SELECT 'it''s;'", "SELECT 2"]),
        ('SELECT 1 AS "x;y"; SELECT 2', ['SELECT 1 AS "x;y"', "SELECT 2"]),
        ("SELECT * FROM delta.`/a;b`; SELECT 2", ["SELECT * FROM delta.`/a;b`", "SELECT 2"]),
        ("SELECT 1 -- c;d\n; SELECT 2", ["SELECT 1 -- c;d", "SELECT 2"]),
        ("SELECT 1 /* ; */; SELECT 2", ["SELECT 1 /* ; */", "SELECT 2"]),
        ("SELECT $$;$$; SELECT 2", ["SELECT $$;$$", "SELECT 2"]),
        ("SELECT $body$ a; $$ b; $body$; SELECT 2", ["SELECT $body$ a; $$ b; $body$", "SELECT 2"]),
        ("SELECT $1; SELECT $2", ["SELECT $1", "SELECT $2"]),
        ("SELECT 1;\n\n  -- only a comment\n;", ["SELECT 1"]),
    ],
)
def test_split_statements(block, statements):
    assert split_statements(block) == statements


def test_both_spellings_of_rows_are_the_same_text():
    typed = [
        (1, None, "", True, 1.0, 1.5, datetime.date(2026, 1, 1), decimal.Decimal("2.50"), [1, None], {"a": 1, "b": "ü"})
    ]
    text = '1\tNULL\t(empty)\ttrue\t1.0\t1.5\t2026-01-01\t2.50\t[1,null]\t{"a":1,"b":"ü"}'
    assert render_rows(typed) == parse_rows(text)


def test_values_are_the_root_of_both_the_insert_and_the_rows():
    values = [(1, "it's", datetime.date(2026, 1, 1), None)]
    assert sql_values(values) == "(1, 'it''s', DATE '2026-01-01', NULL)"
    assert render_rows(values) == parse_rows("1\tit's\t2026-01-01\tNULL")


def test_a_hand_written_literal_goes_in_as_it_is_and_rows_must_then_be_given():
    values = [(L("1::TINYINT"), "a")]
    assert sql_values(values) == "(1::TINYINT, 'a')"
    with pytest.raises(TypeError, match="define the rows by hand"):
        render_rows(values)


def test_a_list_of_rows_in_a_placeholder_is_a_values_list():
    seen = []
    client = Client("fake", lambda statements: seen.extend(statements) or ([], []), values=Values({"t": "cat.t"}))
    client.oks("INSERT INTO {t} VALUES {seed_values}", seed_values=[(1, "a"), (2, None)])
    assert seen == ["INSERT INTO cat.t VALUES (1, 'a'), (2, NULL)"]


def test_a_bare_value_is_a_row_of_one_value():
    assert render_rows([1, 3]) == parse_rows("1\n3") == render_rows([(1,), (3,)])


def test_text_rows_ignore_indentation_and_blank_lines():
    assert parse_rows("\n        1\ta b\n        2\tc\n        ") == [("1", "a b"), ("2", "c")]


def _client(result=None, fail_at=None):
    def run(statements):
        if fail_at is not None:
            raise SqlError("boom", statement=fail_at)
        return ["i"], result

    return Client("fake", run, values=Values({"t": "cat.t"}))


def test_a_list_of_statements_is_not_split():
    seen = []
    client = Client("fake", lambda statements: seen.extend(statements) or ([], []), values=Values({"t": "cat.t"}))
    client.oks(["BEGIN SELECT 1; SELECT 2; END", "SELECT * FROM {t}"])
    assert seen == ["BEGIN SELECT 1; SELECT 2; END", "SELECT * FROM cat.t"]


def test_a_struct_literal_is_left_alone_and_a_value_cannot_shadow_a_verb_option():
    seen = []
    row = {"x": 3, "sort": "this is a field of a matrix value, not the option", "on_fail": "nor this"}
    client = Client(
        "fake", lambda statements: seen.extend(statements) or (["x"], [(3,)]), values=Values({"t": "cat.t", **row})
    )
    client.expects("SELECT {'a': 1}, {{literal}} FROM {t} WHERE x = {x}", [3])
    assert seen == ["SELECT {'a': 1}, {literal} FROM cat.t WHERE x = 3"]


def test_a_placeholder_with_no_value_is_an_error_that_names_what_is_known():
    client = Client("fake", lambda statements: ([], []), values=Values({"catalog": "duck", "t": "cat.t"}))
    with pytest.raises(UsageError, match=r"no value for \{seed_vlaues\}; known: \['catalog', 'seed_values', 't'\]"):
        client.oks("INSERT INTO {t} VALUES {seed_vlaues}", seed_values=[(1,)])


def test_a_failure_in_a_block_names_the_statement():
    with pytest.raises(ExpectationFailed) as e:
        _client(fail_at=1).oks("SELECT 1; SELECT nope; SELECT 3")
    assert "SELECT nope" in str(e.value) and "SELECT 3" not in str(e.value)


def test_a_wrong_result_prints_the_actual_rows_as_pasteable_text():
    with pytest.raises(ExpectationFailed) as e:
        _client(result=[(1, "a"), (2, None)]).expects("SELECT 1", rows="1\ta\n2\tb")
    assert "  actual:\n    1\ta\n    2\tNULL\n" in str(e.value)


def test_a_column_count_mismatch_says_so():
    with pytest.raises(ExpectationFailed) as e:
        _client(result=[(1, 2)]).expects("SELECT 1, 2", rows="1  2")
    assert "TAB between values?" in str(e.value)


def test_error_in_an_earlier_statement_is_not_the_expected_error():
    with pytest.raises(ExpectationFailed):
        _client(fail_at=0).errs("SELECT nope; SELECT also_nope", "boom")
    _client(fail_at=1).errs("SELECT 1; SELECT nope", "boom")


def test_a_failure_aborts_by_default():
    client = Client("fake", lambda statements: (["i"], [(1,)]), record=[].append)
    with pytest.raises(ExpectationFailed):
        client.expects("SELECT 1", rows="2")


def test_on_fail_continue_records_and_the_body_goes_on():
    recorded = []
    client = Client("fake", lambda statements: (["i"], [(1,)]), record=recorded.append)
    client.expects("SELECT 1", rows="2", on_fail="continue")
    client.expects("SELECT 1", rows="3", on_fail="continue")
    assert len(recorded) == 2
    with pytest.raises(ValueError):
        client.expects("SELECT 1", rows="1", on_fail="ignore")


def test_a_failed_setup_is_not_a_failed_check():
    with pytest.raises(SetupFailed):
        _client(fail_at=0).setup("CREATE TABLE x (i INT)")


def test_a_teardown_is_deferred():
    deferred, seen = [], []
    client = Client("fake", lambda statements: seen.extend(statements) or ([], []), defer=deferred.append)
    client.teardown("DROP TABLE x")
    assert seen == [] and len(deferred) == 1
    deferred[0]()
    assert seen == ["DROP TABLE x"]


def test_braces_that_a_value_or_a_nested_struct_brings_are_not_touched():
    seen = []
    client = Client("fake", lambda statements: seen.extend(statements) or ([], []), values=Values({"t": "cat.t"}))
    client.oks("SELECT {'a': {'b': 1}} AS s, {n} AS n, '{j}' AS j", n=3, j='{"a":{"b":1}}')
    assert seen == ["SELECT {'a': {'b': 1}} AS s, 3 AS n, '{\"a\":{\"b\":1}}' AS j"]


def test_not_a_number_and_infinity_render_and_insert():
    rows = [(float("nan"), float("inf"), float("-inf"))]
    assert render_rows(rows) == [("nan", "inf", "-inf")]
    assert sql_values(rows) == "('nan'::DOUBLE, 'inf'::DOUBLE, '-inf'::DOUBLE)"


def test_errs_does_not_take_a_failure_of_the_client_for_the_error_it_asked_for():
    def broken(statements):
        raise SqlError("no answer from the shell: boom")

    with pytest.raises(ExpectationFailed, match="not a statement of this call"):
        Client("fake", broken).errs("SELECT 1", "boom")


@pytest.mark.parametrize(
    "rows, spellings",
    [
        (1, "rows=[1], or text with a TAB between values, rows='1'"),
        (None, "rows=[None], or text with a TAB between values, rows='NULL'"),
        ((1, "a"), "rows=[(1, 'a')], or text with a TAB between values, rows='1\\ta'"),
    ],
)
def test_rows_that_are_a_scalar_or_one_tuple_are_refused_with_the_two_valid_spellings(rows, spellings):
    client = _client(result=[(1, "a")])
    for verb in (client.expects, client.expects_contains):
        with pytest.raises(UsageError, match="is not rows") as e:
            verb("SELECT 1", rows)
        assert spellings in str(e.value)


def test_contains_counts_how_often_a_row_is_expected():
    client = _client(result=[(1,), (2,), (2,)])
    client.expects_contains("SELECT 1", [2, 2])
    client.expects_contains("SELECT 1", "2\n1")
    with pytest.raises(ExpectationFailed) as e:
        client.expects_contains("SELECT 1", [1, 1, 2])
    assert "  missing:\n    1\n  actual:" in str(e.value)


# -----------------------------------------------------------------------------
# exec: the answer instead of a verdict
#


def test_exec_gives_back_the_result_of_a_statement_that_ran():
    executed = _client(result=[(1, "a")]).exec("SELECT i, name FROM {t}")
    assert executed
    assert (executed.error, executed.columns, executed.rows) == (None, ["i"], [(1, "a")])
    assert repr(executed) == "[fake] exec, ok, 1 row(s)\n    SELECT i, name FROM cat.t"


def test_an_answer_names_its_client_by_name_and_its_message_by_label():
    executed = Client("duck", lambda statements: (["i"], [(1,)]), label="duck/duck-shell").exec("SELECT 1")
    assert executed.client == "duck"
    assert repr(executed) == "[duck/duck-shell] exec, ok, 1 row(s)\n    SELECT 1"


def test_a_statement_that_failed_is_an_answer_not_a_failed_check():
    recorded = []
    client = Client("fake", _fails_at(0), values=Values({"t": "cat.t"}), record=recorded.append)
    executed = client.exec("INSERT INTO {t} VALUES (1)")
    assert not executed
    assert executed.error == "Conflicting commit"
    assert recorded == []
    assert repr(executed) == "[fake] exec, failed: Conflicting commit\n    INSERT INTO cat.t VALUES (1)"


def test_only_the_last_statement_of_a_try_answers_for_the_rest():
    client = Client("fake", _fails_at(0), values=Values({"t": "cat.t"}))
    with pytest.raises(ExpectationFailed, match="statement failed"):
        client.exec("ATTACH 'x'; INSERT INTO {t} VALUES (1)")


def test_a_timeout_or_a_dead_client_stays_an_exception():
    def dead(statements):
        raise SqlError("the statement was not done in 5s; the duckdb shell was killed")

    with pytest.raises(ExpectationFailed, match="not a statement of this call"):
        Client("fake", dead).exec("SELECT 1")


def test_a_try_is_counted_in_the_time_the_client_spent():
    client = _client(result=[(1,)])
    client.exec("SELECT 1")
    assert client.seconds > 0


def _fails_at(statement):
    def run(statements):
        raise SqlError("Conflicting commit", statement=statement)

    return run


@pytest.mark.parametrize(
    "verb, args", [("oks", ()), ("setup", ()), ("exec", ()), ("errs", ("x",)), ("expects", ([1],))]
)
def test_params_are_given_by_keyword_to_every_verb(verb, args):
    client = Client("fake", lambda statements: (["i"], [(1,)]))
    with pytest.raises(TypeError):
        getattr(client, verb)("SELECT {n}", *args, {"n": 1})


def test_a_value_that_names_a_root_is_filled_before_the_sql():
    seen = []
    client = Client(
        "spark", lambda statements: seen.extend(statements) or ([], []), values=Values({"TEMP_DIR": "/run/x"})
    )
    client.oks("SELECT * FROM {t}", t="delta.`{TEMP_DIR}/t`")
    assert seen == ["SELECT * FROM delta.`/run/x/t`"]


def test_a_chain_of_values_is_filled_bottom_up_and_a_filled_value_is_not_read_again():
    seen = []
    values = Values({"root": "/r", "table_dir": "{root}/tables", "literal": "{{root}}"})
    client = Client("fake", lambda statements: seen.extend(statements) or ([], []), values=values)
    client.oks("SELECT '{t}', '{literal}'", t="{table_dir}/t")
    assert seen == ["SELECT '/r/tables/t', '{root}'"]


def test_values_that_refer_to_each_other_in_a_cycle_are_an_error_that_names_the_cycle():
    client = Client("fake", lambda statements: ([], []), values=Values({"a": "{b}/x", "b": "{A}/y"}))
    with pytest.raises(UsageError, match=r"a cycle: \{a\} -> \{b\} -> \{a\}"):
        client.oks("SELECT '{a}'")
    with pytest.raises(UsageError, match=r"no value for \{nope\} \(in the value of \{t\}\)"):
        client.oks("SELECT '{t}'", t="{nope}")
