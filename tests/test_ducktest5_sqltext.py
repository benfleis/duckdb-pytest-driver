"""ducktest5's text handling: statement splitting, and the one text form rows compare in."""

import datetime
import decimal

import pytest

from ducktest5.binary import UsageError
from ducktest5.client import Client, ExpectationFailed, SetupFailed, SqlError
from ducktest5.sqltext import L, parse_rows, render_rows, split_statements, sql_values


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
    client = Client("fake", lambda statements: seen.extend(statements) or ([], []), table="cat.t")
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

    return Client("fake", run, table="cat.t")


def test_a_list_of_statements_is_not_split():
    seen = []
    client = Client("fake", lambda statements: seen.extend(statements) or ([], []), table="cat.t")
    client.oks(["BEGIN SELECT 1; SELECT 2; END", "SELECT * FROM {t}"])
    assert seen == ["BEGIN SELECT 1; SELECT 2; END", "SELECT * FROM cat.t"]


def test_a_struct_literal_is_left_alone_and_a_case_table_cannot_shadow_a_verb_option():
    seen = []
    client = Client("fake", lambda statements: seen.extend(statements) or (["x"], [(3,)]), table="cat.t")
    case = {"x": 3, "sort": "this is a column of the case table, not the option", "on_fail": "nor this"}
    client.expects("SELECT {'a': 1}, {{literal}} FROM {t} WHERE x = {x}", [3], case)
    assert seen == ["SELECT {'a': 1}, {literal} FROM cat.t WHERE x = 3"]


def test_a_placeholder_with_no_value_is_an_error_that_names_what_is_known():
    client = Client("fake", lambda statements: ([], []), table="cat.t", values={"catalog": "duck"})
    with pytest.raises(UsageError, match=r"no value for \{seed_vlaues\}; known: \['catalog', 'seed_values', 't'\]"):
        client.oks("INSERT INTO {t} VALUES {seed_vlaues}", {"seed_values": [(1,)]})


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
    client = Client("fake", lambda statements: seen.extend(statements) or ([], []), table="cat.t")
    client.oks("SELECT {'a': {'b': 1}} AS s, {n} AS n, '{j}' AS j", {"n": 3, "j": '{"a":{"b":1}}'})
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
# tries: the answer instead of a verdict
#


def test_tries_gives_back_the_result_of_a_statement_that_ran():
    tried = _client(result=[(1, "a")]).tries("SELECT i, name FROM {t}")
    assert tried
    assert (tried.error, tried.columns, tried.rows) == (None, ["i"], [(1, "a")])
    assert repr(tried) == "[fake] tried, ok, 1 row(s)\n    SELECT i, name FROM cat.t"


def test_a_statement_that_failed_is_an_answer_not_a_failed_check():
    recorded = []
    client = Client("fake", _fails_at(0), table="cat.t", record=recorded.append)
    tried = client.tries("INSERT INTO {t} VALUES (1)")
    assert not tried
    assert tried.error == "Conflicting commit"
    assert recorded == []
    assert repr(tried) == "[fake] tried, failed: Conflicting commit\n    INSERT INTO cat.t VALUES (1)"


def test_only_the_last_statement_of_a_try_answers_for_the_rest():
    client = Client("fake", _fails_at(0), table="cat.t")
    with pytest.raises(ExpectationFailed, match="statement failed"):
        client.tries("ATTACH 'x'; INSERT INTO {t} VALUES (1)")


def test_a_timeout_or_a_dead_client_stays_an_exception():
    def dead(statements):
        raise SqlError("the statement was not done in 5s; the duckdb shell was killed")

    with pytest.raises(ExpectationFailed, match="not a statement of this call"):
        Client("fake", dead).tries("SELECT 1")


def test_a_try_is_counted_in_the_time_the_client_spent():
    client = _client(result=[(1,)])
    client.tries("SELECT 1")
    assert client.seconds > 0


def _fails_at(statement):
    def run(statements):
        raise SqlError("Conflicting commit", statement=statement)

    return run
