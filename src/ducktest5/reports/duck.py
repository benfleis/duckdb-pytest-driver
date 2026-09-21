"""The events as DuckDB tables, so a history of runs is a query.

Loaded by the run's own `duckdb` shell over `read_ndjson_objects` -- no Python DuckDB package, and
the same binary the run used, so the file is always readable by the build at hand. Runs **append**:
every row carries `run_id`, and loading a run that is already in the file replaces that run's rows,
so re-reporting an old run never doubles it.

    runs      one row per run: when, the command, the run dir, the version, the counts, the exit code
    cases     one row per case: outcome, seconds, attempts, runner, batch, the variant values
    failures  one row per case that did not pass: location, statement, expected, actual, message
    clients   one row per (case, client): the seconds spent in that client
    services  one row per service or engine a run brought up, or failed to

Three questions this answers, which no single run can:

    -- the slowest cases of the last run
    SELECT case_id, seconds FROM cases
    WHERE run_id = (SELECT run_id FROM runs ORDER BY started DESC LIMIT 1)
    ORDER BY seconds DESC LIMIT 10

    -- cases whose outcome changed between the last two runs
    WITH recent AS (SELECT run_id, row_number() OVER (ORDER BY started DESC) AS n FROM runs),
         now AS (SELECT * FROM cases WHERE run_id = (SELECT run_id FROM recent WHERE n = 1)),
         before AS (SELECT * FROM cases WHERE run_id = (SELECT run_id FROM recent WHERE n = 2))
    SELECT coalesce(now.case_id, before.case_id) AS case_id,
           before.outcome AS was, now.outcome AS became
    FROM now FULL JOIN before ON now.case_id = before.case_id
    WHERE before.outcome IS DISTINCT FROM now.outcome AND (SELECT count(*) FROM recent) > 1
    ORDER BY case_id

    -- flaky cases: they passed, but not on the first attempt
    SELECT case_id, count(*) AS times, max(attempts) AS most_attempts
    FROM cases WHERE outcome = 'passed' AND attempts > 1
    GROUP BY case_id ORDER BY times DESC, case_id
"""

from __future__ import annotations

import os
import re
import subprocess

from ..binary import UsageError

# The same SQL as the docstring above; a self-test keeps the two equal.
EXAMPLES = {
    "slowest": """SELECT case_id, seconds FROM cases
    WHERE run_id = (SELECT run_id FROM runs ORDER BY started DESC LIMIT 1)
    ORDER BY seconds DESC LIMIT 10""",
    "changed": """WITH recent AS (SELECT run_id, row_number() OVER (ORDER BY started DESC) AS n FROM runs),
         now AS (SELECT * FROM cases WHERE run_id = (SELECT run_id FROM recent WHERE n = 1)),
         before AS (SELECT * FROM cases WHERE run_id = (SELECT run_id FROM recent WHERE n = 2))
    SELECT coalesce(now.case_id, before.case_id) AS case_id,
           before.outcome AS was, now.outcome AS became
    FROM now FULL JOIN before ON now.case_id = before.case_id
    WHERE before.outcome IS DISTINCT FROM now.outcome AND (SELECT count(*) FROM recent) > 1
    ORDER BY case_id""",
    "flaky": """SELECT case_id, count(*) AS times, max(attempts) AS most_attempts
    FROM cases WHERE outcome = 'passed' AND attempts > 1
    GROUP BY case_id ORDER BY times DESC, case_id""",
}

TABLES = {
    "runs": """run_id VARCHAR, started TIMESTAMP, ended TIMESTAMP, command VARCHAR, run_dir VARCHAR,
               project_root VARCHAR, duckdb_version VARCHAR, cases BIGINT, seconds DOUBLE, exit_code BIGINT,
               passed BIGINT, failed BIGINT, error BIGINT, blocked BIGINT, skipped BIGINT""",
    "cases": """run_id VARCHAR, case_id VARCHAR, test VARCHAR, file VARCHAR, type VARCHAR, runner VARCHAR,
                outcome VARCHAR, seconds DOUBLE, attempts BIGINT, note VARCHAR, batch VARCHAR,
                variants VARCHAR, kept VARCHAR[]""",
    "failures": """run_id VARCHAR, case_id VARCHAR, outcome VARCHAR, location VARCHAR, statement VARCHAR,
                   expected VARCHAR, actual VARCHAR, message VARCHAR""",
    "clients": "run_id VARCHAR, case_id VARCHAR, client VARCHAR, seconds DOUBLE",
    "services": """run_id VARCHAR, service_id VARCHAR, kind VARCHAR, state VARCHAR, mode VARCHAR,
                   seconds DOUBLE, error VARCHAR""",
}


def _quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def script(event_files):
    """The whole load, as one SQL script: make the tables, read the lines, drop the runs that are
    being loaded again, insert."""
    files = "[" + ", ".join(_quote(os.path.abspath(p)) for p in event_files) + "]"
    parts = [f"CREATE TABLE IF NOT EXISTS {name} ({columns});" for name, columns in TABLES.items()]
    parts.append(f"CREATE OR REPLACE TEMP TABLE ducktest_events AS SELECT json AS j FROM read_ndjson_objects({files});")
    parts.append(
        "CREATE OR REPLACE TEMP TABLE ducktest_loaded AS "
        "SELECT DISTINCT j->>'run' AS run_id FROM ducktest_events WHERE j->>'run' IS NOT NULL;"
    )
    for name in TABLES:
        parts.append(f"DELETE FROM {name} WHERE run_id IN (SELECT run_id FROM ducktest_loaded);")
    parts += [_runs(), _cases(), _failures(), _clients(), _services()]
    return "\n".join(parts) + "\n"


def _runs():
    return """
INSERT INTO runs
WITH s AS (SELECT j FROM ducktest_events WHERE j->>'event' = 'run_start'),
     e AS (SELECT j FROM ducktest_events WHERE j->>'event' = 'run_end')
SELECT s.j->>'run',
       try_cast(s.j->>'at' AS TIMESTAMP),
       try_cast(e.j->>'at' AS TIMESTAMP),
       array_to_string(coalesce(CAST(s.j->'command' AS VARCHAR[]), []), ' '),
       s.j->>'run_dir',
       s.j->>'project_root',
       s.j->>'$.versions.duckdb',
       try_cast(s.j->>'cases' AS BIGINT),
       try_cast(e.j->>'seconds' AS DOUBLE),
       try_cast(e.j->>'exit_code' AS BIGINT),
       try_cast(e.j->>'$.counts.passed' AS BIGINT),
       try_cast(e.j->>'$.counts.failed' AS BIGINT),
       try_cast(e.j->>'$.counts.error' AS BIGINT),
       try_cast(e.j->>'$.counts.blocked' AS BIGINT),
       try_cast(e.j->>'$.counts.skipped' AS BIGINT)
FROM s LEFT JOIN e ON s.j->>'run' = e.j->>'run';"""


def _cases():
    return """
INSERT INTO cases
SELECT j->>'run', j->>'id', j->>'test', j->>'file', j->>'type', j->>'runner', j->>'outcome',
       try_cast(j->>'seconds' AS DOUBLE), coalesce(try_cast(j->>'attempts' AS BIGINT), 1),
       j->>'note', j->>'batch', j->>'variants',
       coalesce(CAST(j->'kept' AS VARCHAR[]), [])
FROM ducktest_events WHERE j->>'event' = 'case_end';"""


def _failures():
    return """
INSERT INTO failures
SELECT j->>'run', j->>'id', j->>'outcome',
       j->>'$.failure.where', j->>'$.failure.sql', j->>'$.failure.expected', j->>'$.failure.actual',
       coalesce(j->>'$.failure.summary', array_to_string(coalesce(CAST(j->'problems' AS VARCHAR[]), []), chr(10)))
FROM ducktest_events
WHERE j->>'event' = 'case_end' AND j->>'outcome' <> 'passed';"""


def _clients():
    return """
INSERT INTO clients
SELECT j->>'run', j->>'id', k, try_cast(json_extract_string(j->'seconds_by_client', '$."' || k || '"') AS DOUBLE)
FROM ducktest_events, UNNEST(json_keys(j, '$.seconds_by_client')) AS t(k)
WHERE j->>'event' = 'case_end' AND json_type(j->'seconds_by_client') = 'OBJECT';"""


def _services():
    return """
INSERT INTO services
SELECT j->>'run', j->>'id', j->>'kind', j->>'state', j->>'mode',
       try_cast(j->>'seconds' AS DOUBLE), j->>'error'
FROM ducktest_events WHERE j->>'event' = 'service';"""


def load(db_path, event_files, *, duckdb_bin=None):
    """Put the events of `event_files` into `db_path`. Returns the path."""
    if not duckdb_bin:
        raise UsageError(
            "--duckdb needs the duckdb shell of the run. The run dir names it; for an old run made "
            "before that was recorded, pass --duckdb-bin PATH."
        )
    done = subprocess.run(
        [duckdb_bin, "-batch", "-noheader", db_path],
        input=script(event_files),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if done.returncode != 0:
        raise UsageError(f"the events could not be loaded into {db_path}:\n{_trim(done.stderr or done.stdout)}")
    return db_path


def _trim(text, limit=1200):
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    return text[:limit]
