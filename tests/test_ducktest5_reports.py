"""The renderers, against one synthetic run that holds every outcome.

`ducktest5_goldens/events.jsonl` is that run: a passed case, a failed one with rows, a skipped one
with its reason, an error in a Python body, a blocked case, and one that passed on its second
attempt. The expected text of each renderer is a file beside it, so the goldens read as a sample of
what a run looks like. Re-make them with `UPDATE_GOLDENS=1 pytest tests/test_ducktest5_reports.py`.
"""

import io
import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from ducktest5 import events
from ducktest5.reports import Report, document, duck, junit, render
from ducktest5.reports.live import Live

GOLDENS = os.path.join(os.path.dirname(__file__), "ducktest5_goldens")
EVENTS = os.path.join(GOLDENS, "events.jsonl")


@pytest.fixture
def report():
    return Report.of(events.read(EVENTS))


def golden(name, produced):
    path = os.path.join(GOLDENS, name)
    if os.environ.get("UPDATE_GOLDENS"):
        with open(path, "w") as f:
            f.write(produced)
    with open(path) as f:
        assert produced == f.read(), f"{name} differs; UPDATE_GOLDENS=1 to re-make it"


def rendered(report, **kwargs):
    out = io.StringIO()
    render(report, out, **kwargs)
    return out.getvalue()


# -----------------------------------------------------------------------------
# The stream itself
#


def test_every_outcome_is_in_the_synthetic_run(report):
    assert report.counts == {"passed": 2, "failed": 1, "error": 1, "blocked": 1, "skipped": 1}
    assert [c["id"] for c in report.retried()] == ["sql/flaky.test::flaky"]
    assert report.exit_code == 1


def test_a_half_written_last_line_is_dropped(tmp_path):
    path = tmp_path / "04-events.jsonl"
    path.write_text(open(EVENTS).read() + '{"event": "case_end", "id": "cut off her')
    assert len(events.read(str(path))) == len(events.read(EVENTS))


def test_a_credential_in_the_command_line_is_masked():
    assert events.redact_command(["ducktest5", "--secret-key=hunter2", "test/sql"]) == [
        "ducktest5",
        "--secret-key=***",
        "test/sql",
    ]


@pytest.mark.parametrize(
    "text, parts",
    [
        (
            "Wrong result in query! (a.test:4)!\nExpected result:\n2\nActual result:\n1",
            {"summary": "Wrong result in query! (a.test:4)!", "expected": "2", "actual": "1", "where": "a.test:4"},
        ),
        (
            "b.test:18\nFilters: *\n1. b.test:18\n====\nQuery failed! (b.test:18)!\n====\nSELECT 1;\n"
            "====\nActual result:\n====\nIO Error: nope\n~~~~~~~~~~~~~~~~~~~~\nunittest is a Catch v2 host application.",
            {
                "summary": "Query failed! (b.test:18)!",
                "sql": "SELECT 1;",
                "actual": "IO Error: nope",
                "where": "b.test:18",
            },
        ),
        ("", {}),
    ],
)
def test_what_the_binary_printed_comes_apart_into_message_statement_and_results(text, parts):
    assert events.binary_failure(text) == parts


# -----------------------------------------------------------------------------
# text: live, normal, verbose, terse
#


def test_the_live_output_is_the_events_as_they_arrive():
    out = io.StringIO()
    live = Live(out, color=False)
    for event in events.read(EVENTS):
        live(event)
    live.finish(Report.of(events.read(EVENTS)))
    golden("live.txt", out.getvalue())


def test_normal_is_the_live_output_without_the_progress_count(report):
    golden("normal.txt", rendered(report, format="text", level="normal"))


def test_verbose_adds_the_statements_a_body_ran(report):
    produced = rendered(report, format="text", level="verbose")
    golden("verbose.txt", produced)
    assert "0.51s  [duckdb] CREATE TABLE u (id INTEGER);" in produced


def test_terse_has_no_banner_and_no_passed_case(report):
    produced = rendered(report, format="text", level="terse")
    golden("terse.txt", produced)
    assert "sql/read.test::read" not in produced  # the passed case
    assert produced.splitlines()[0].startswith("case: ")


def test_the_terse_grammar_is_what_the_docstring_says(report):
    """Every line is a key, a key with continuation lines, or a continuation line, in a fixed order."""
    from ducktest5.reports.text import TERSE_KEYS

    lines = rendered(report, format="text", level="terse").splitlines()
    assert lines[-1].startswith("summary: ")
    assert sum(line.startswith("summary: ") for line in lines) == 1
    seen, blocks = [], 0
    for line in lines[:-2]:
        if not line:
            seen = []
            continue
        if line.startswith("  | "):
            assert seen, "a continuation line before any key"
            continue
        key, _, _ = line.partition(":")
        assert key in TERSE_KEYS, key
        assert not seen or TERSE_KEYS.index(key) > TERSE_KEYS.index(seen[-1]), f"{key} out of order"
        if not seen:
            assert key == "case"
            blocks += 1
        seen.append(key)
    assert blocks == 4  # every case that did not pass


def test_colour_is_only_asked_for_on_a_terminal(monkeypatch, report):
    monkeypatch.delenv("NO_COLOR", raising=False)
    painted = rendered(report, format="text", level="normal", color=True)
    assert "\033[31m" in painted  # failed, in red
    assert "\033[" not in rendered(report, format="text", level="normal", color=False)


def test_no_color_turns_it_off_on_a_terminal(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    from ducktest5.reports.live import wants_colour

    assert wants_colour(Terminal()) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert wants_colour(Terminal()) is False
    assert wants_colour(io.StringIO()) is False


# -----------------------------------------------------------------------------
# json
#


def test_the_json_format_is_the_event_lines_as_they_are(report):
    produced = rendered(report, format="json").splitlines()
    assert [json.loads(line) for line in produced] == events.read(EVENTS)


def test_the_yaml_and_the_json_document_say_the_same_thing(tmp_path, report):
    import yaml

    document.write_json(report, str(tmp_path / "05-report.json"))
    document.write_yaml(report, str(tmp_path / "05-report.yaml"))
    as_json = json.loads((tmp_path / "05-report.json").read_text())
    assert as_json == yaml.safe_load((tmp_path / "05-report.yaml").read_text())
    assert as_json["counts"] == report.counts
    assert as_json["exit_code"] == 1
    assert [c["id"] for c in as_json["cases"]] == [c["id"] for c in report.cases]


# -----------------------------------------------------------------------------
# junit
#


def test_the_junit_xml_parses_back_into_the_common_shape(report):
    out = io.StringIO()
    junit.render(report, out)
    golden("report.junit.xml", out.getvalue())
    root = ET.fromstring(out.getvalue())
    assert root.tag == "testsuites"
    assert root.attrib["tests"] == "6" and root.attrib["failures"] == "1" and root.attrib["errors"] == "2"
    # the container is the test FILE
    assert sorted(s.attrib["name"] for s in root) == [
        "test/py/spark.py",
        "test/py/write.py",
        "test/sql/flaky.test",
        "test/sql/needs_env.test",
        "test/sql/read.test",
        "test/sql/rows.test",
    ]
    by_name = {c.attrib["name"]: c for s in root for c in s.findall("testcase")}
    assert by_name["sql/read.test::read"].find("failure") is None
    assert by_name["sql/rows.test::rows"].find("failure").attrib["type"] == "FAILED"
    assert by_name["py/write.py::write_then_read"].find("error").attrib["type"] == "ERROR"
    assert by_name["py/spark.py::roundtrip[backend=oss]"].find("error").attrib["type"] == "BLOCKED"
    assert by_name["sql/needs_env.test::needs_env"].find("skipped").attrib["message"] == "require-env DAT_PATH"
    assert all(c.attrib["classname"] == _file_of(c.attrib["name"]) for c in by_name.values())


def _file_of(case_id):
    return "test/" + case_id.split("::")[0]


def test_a_case_that_passed_on_a_second_attempt_says_so_as_a_property(report):
    root = ET.fromstring(io.StringIO(_junit(report)).read())
    (flaky,) = [c for s in root for c in s.findall("testcase") if c.attrib["name"] == "sql/flaky.test::flaky"]
    properties = {p.attrib["name"]: p.attrib["value"] for p in flaky.iter("property")}
    assert properties["attempts"] == "2" and properties["note"] == "passed on attempt 2 of 2"
    assert flaky.find("failure") is None and flaky.find("error") is None


def _junit(report):
    out = io.StringIO()
    junit.render(report, out)
    return out.getvalue()


# -----------------------------------------------------------------------------
# duckdb tables
#


def shell():
    return os.environ.get("DUCKDB_SHELL") or shutil.which("duckdb")


needs_duckdb = pytest.mark.skipif(shell() is None, reason="no duckdb shell (set $DUCKDB_SHELL or PATH)")


def query(db, sql):
    done = subprocess.run(
        [shell(), "-batch", "-noheader", "-list", db, "-c", sql], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    return [line.split("|") for line in done.stdout.strip().splitlines() if line]


def test_the_example_queries_are_the_ones_the_docstring_shows():
    for sql in duck.EXAMPLES.values():
        assert sql in duck.__doc__


@needs_duckdb
def test_the_events_load_into_tables_that_answer_the_example_questions(tmp_path):
    db = str(tmp_path / "results.duckdb")
    duck.load(db, [EVENTS], duckdb_bin=shell())
    assert query(db, "SELECT count(*) FROM cases") == [["6"]]
    assert query(db, "SELECT count(*) FROM runs") == [["1"]]
    assert sorted(query(db, "SELECT service_id, state FROM services")) == [
        ["spark", "failed"],
        ["spark", "starting"],
        ["store", "starting"],
        ["store", "up"],
    ]
    assert query(db, "SELECT client, seconds FROM clients") == [["duckdb", "0.72"]]
    assert query(db, "SELECT case_id FROM failures ORDER BY case_id") == [
        ["py/spark.py::roundtrip[backend=oss]"],
        ["py/write.py::write_then_read"],
        ["sql/needs_env.test::needs_env"],
        ["sql/rows.test::rows"],
    ]
    slowest = query(db, duck.EXAMPLES["slowest"])
    assert slowest[0] == ["sql/flaky.test::flaky", "2.6"]
    assert query(db, duck.EXAMPLES["flaky"]) == [["sql/flaky.test::flaky", "1", "2"]]
    assert query(db, duck.EXAMPLES["changed"]) == []  # only one run so far


@needs_duckdb
def test_runs_append_and_a_run_loaded_twice_is_not_counted_twice(tmp_path):
    db = str(tmp_path / "results.duckdb")
    second = tmp_path / "second.jsonl"
    later = open(EVENTS).read().replace("amber-koala-01", "brass-otter-02").replace("T10:00:0", "T11:00:0")
    second.write_text(later.replace('"failed", ', '"passed", '))
    duck.load(db, [EVENTS], duckdb_bin=shell())
    duck.load(db, [EVENTS], duckdb_bin=shell())  # again: it replaces its own rows
    assert query(db, "SELECT count(*) FROM cases") == [["6"]]
    duck.load(db, [str(second)], duckdb_bin=shell())
    assert query(db, "SELECT count(*) FROM runs") == [["2"]]
    assert query(db, duck.EXAMPLES["changed"]) == [["sql/rows.test::rows", "failed", "passed"]]
