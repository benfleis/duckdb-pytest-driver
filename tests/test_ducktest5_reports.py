"""The renderers, against one synthetic run that holds every outcome.

`ducktest5_goldens/events.jsonl` is that run: a passed test, a failed one with rows, a skipped one
with its reason, an error in a Python body, a blocked test, and one that passed on its second
attempt. The expected text of each renderer is a file beside it, so the goldens read as a sample of
what a run looks like. Re-make them with `UPDATE_GOLDENS=1 pytest tests/test_ducktest5_reports.py`.
"""

import io
import json
import os
import xml.etree.ElementTree as ET

import pytest

from ducktest5 import events
from ducktest5.reports import Report, document, junit, render
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
    path.write_text(open(EVENTS).read() + '{"event": "test_end", "id": "cut off her')
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
        (
            "1. c.test:4\n"
            + "=" * 80
            + "\nQuery unexpectedly failed! (c.test:4)!\n"
            + "=" * 80
            + "\nSELECT * FROM t;\n"
            + "=" * 80
            + "\nCatalog Error: Table with name t does not exist!\n\nLINE 1: SELECT * FROM t\n\n"
            + "~" * 79
            + "\nunittest is a Catch v2.13.7 host application.",
            {
                "summary": "Query unexpectedly failed! (c.test:4)!",
                "sql": "SELECT * FROM t;",
                "error": "Catalog Error: Table with name t does not exist!\n\nLINE 1: SELECT * FROM t",
                "where": "c.test:4",
            },
        ),
        ("", {}),
    ],
)
def test_what_the_binary_printed_comes_apart_into_message_statement_and_results(text, parts):
    assert events.binary_failure(text) == parts


def test_a_place_outside_the_project_keeps_its_leading_slash():
    assert events.where_in("boom\n  at /opt/elsewhere/helper.py:3") == "/opt/elsewhere/helper.py:3"


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


def test_terse_has_no_banner_and_no_passed_test(report):
    produced = rendered(report, format="text", level="terse")
    golden("terse.txt", produced)
    assert "sql/read.test::read" not in produced  # the passed test
    assert produced.splitlines()[0].startswith("test: ")


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
            assert key == "test"
            blocks += 1
        seen.append(key)
    assert blocks == 4  # every test that did not pass


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
    assert [c["id"] for c in as_json["tests"]] == sorted(c["id"] for c in report.tests)


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


def _file_of(test_id):
    return "test/" + test_id.split("::")[0]


def test_a_test_that_passed_on_a_second_attempt_says_so_as_a_property(report):
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
# a stable order in the files, whatever order the tests ended
#


def _test_end(name, *, file="test/a.test"):
    return {
        "event": "test_end",
        "run": "r",
        "id": f"{file.removeprefix('test/')}::{name}",
        "test": f"{file}::{name}",
        "file": file,
        "type": "sqllogic",
        "params": None,
        "variants": {},
        "tags": [],
        "outcome": "passed",
        "runner": "unittest",
    }


def test_the_yaml_json_and_junit_files_do_not_depend_on_which_test_ended_first(tmp_path):
    names = ["z_test", "a_test", "m_test"]
    forward = Report.of([_test_end(n) for n in names])
    backward = Report.of([_test_end(n) for n in reversed(names)])

    for writer, ext in ((document.write_yaml, "yaml"), (document.write_json, "json")):
        a, b = str(tmp_path / f"forward.{ext}"), str(tmp_path / f"backward.{ext}")
        writer(forward, a)
        writer(backward, b)
        assert open(a).read() == open(b).read()

    assert _junit(forward) == _junit(backward)


# -----------------------------------------------------------------------------
# `ducktest5 report`: an old run, and several merged
#


LATER = "2026-09-21T11-00-00Z--brass-otter-02"


def second_run(tmp_path):
    """The same run again, an hour later, with the test that failed passing this time."""
    text = open(EVENTS).read().replace("2026-09-21T10-00-00Z--amber-koala-01", LATER)
    path = tmp_path / "later.jsonl"
    path.write_text(text.replace("T10:00:0", "T11:00:0").replace('"outcome": "failed"', '"outcome": "passed"'))
    return str(path)


def report_command(args, capsys):
    from ducktest5.cli import main

    code = main(["report"] + args)
    return code, capsys.readouterr().out


def test_report_renders_an_old_run_without_recomputing_anything(capsys):
    code, printed = report_command(["--run-dir", EVENTS, "--level", "terse"], capsys)
    assert code == 1
    assert printed == open(os.path.join(GOLDENS, "terse.txt")).read()


def test_a_missing_event_file_is_a_usage_error(tmp_path, capsys):
    assert report_command(["--run-dir", str(tmp_path)], capsys)[0] == 2


def test_several_run_dirs_merge_and_a_later_run_wins_a_shared_test(tmp_path, capsys):
    code, printed = report_command(["--run-dir", EVENTS, "--run-dir", second_run(tmp_path), "--level", "terse"], capsys)
    assert "test: sql/rows.test::rows" not in printed  # it passed in the later run
    assert code == 1  # the error and the blocked test are still there
    assert printed.count(f"run: {LATER}") == 3  # every test left says which run it came from
    assert printed.splitlines()[-1].startswith("summary: 3 passed, 0 failed, 1 error, 1 blocked, 1 skipped")


def test_a_merged_report_names_the_run_of_every_test(tmp_path, capsys):
    _, printed = report_command(["--run-dir", EVENTS, "--run-dir", second_run(tmp_path)], capsys)
    assert f"<{LATER}>" in printed and "<2026-09-21T10-00-00Z--amber-koala-01>" not in printed


def test_report_writes_the_files_it_is_asked_for(tmp_path, capsys):
    out_json, out_xml = str(tmp_path / "r.json"), str(tmp_path / "r.xml")
    report_command(["--run-dir", EVENTS, "--report-json", out_json, "--junit", out_xml, "--format", "junit"], capsys)
    assert json.loads(open(out_json).read())["counts"]["failed"] == 1
    assert ET.parse(out_xml).getroot().tag == "testsuites"
