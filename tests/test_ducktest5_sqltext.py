"""ducktest5's text handling: statement splitting, and the one text form rows compare in."""

import datetime
import decimal

import pytest

from ducktest5.client import Client, ExpectationFailed, SqlError
from ducktest5.sqltext import parse_rows, render_rows, split_statements


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
        ("SELECT 1;\n\n  -- only a comment\n;", ["SELECT 1"]),
    ],
)
def test_split_statements(block, statements):
    assert split_statements(block) == statements


def test_both_spellings_of_rows_are_the_same_text():
    typed = [(1, None, "", True, 1.0, 1.5, datetime.date(2026, 1, 1), decimal.Decimal("2.50"), [1, None])]
    text = "1\tNULL\t(empty)\ttrue\t1.0\t1.5\t2026-01-01\t2.50\t[1, NULL]"
    assert render_rows(typed) == parse_rows(text)


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
    client.ok(["BEGIN SELECT 1; SELECT 2; END", "SELECT * FROM {t}"])
    assert seen == ["BEGIN SELECT 1; SELECT 2; END", "SELECT * FROM cat.t"]


def test_unknown_braces_are_left_alone():
    seen = []
    client = Client("fake", lambda statements: seen.extend(statements) or ([], []), table="cat.t")
    client.ok("SELECT {'a': 1} FROM {t} WHERE x = {x}", x=3)
    assert seen == ["SELECT {'a': 1} FROM cat.t WHERE x = 3"]


def test_a_failure_in_a_block_names_the_statement():
    with pytest.raises(ExpectationFailed) as e:
        _client(fail_at=1).ok("SELECT 1; SELECT nope; SELECT 3")
    assert "SELECT nope" in str(e.value) and "SELECT 3" not in str(e.value)


def test_a_wrong_result_prints_the_actual_rows_as_pasteable_text():
    with pytest.raises(ExpectationFailed) as e:
        _client(result=[(1, "a"), (2, None)]).expect("SELECT 1", rows="1\ta\n2\tb")
    assert "  actual:\n    1\ta\n    2\tNULL\n" in str(e.value)


def test_a_column_count_mismatch_says_so():
    with pytest.raises(ExpectationFailed) as e:
        _client(result=[(1, 2)]).expect("SELECT 1, 2", rows="1  2")
    assert "TAB between values?" in str(e.value)


def test_error_in_an_earlier_statement_is_not_the_expected_error():
    with pytest.raises(ExpectationFailed):
        _client(fail_at=0).error("SELECT nope; SELECT also_nope", "boom")
    _client(fail_at=1).error("SELECT 1; SELECT nope", "boom")
