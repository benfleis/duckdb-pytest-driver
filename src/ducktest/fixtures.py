"""Table fixtures: a fixture table is INSTANTIATED from its (fixture) DEFINITION and
optional SEED DATA, with DuckDB as the middleman.

THE VOCABULARY. When we *provision*, we gather/reference `Fixture` definitions and
*instantiate* them into `Table`s which may or may not contain seed data:

    provision ─▶ Fixture (ref)  ─load─▶ FixtureDef (schema + optional seed)
                                        │ instantiate (per backend)
                                        ▼
                                      Table (columns + optional seed_data)

A `FixtureDef` is a tiny SQL file that defines ONE table's schema + seed rows and
NOTHING about physical storage:

    -- fixture: simple_table
    -- keys: [id]
    CREATE TABLE simple_table (id INTEGER);
    INSERT INTO simple_table VALUES (1), (2), (3), (4), (5);

The logical/physical split is deliberate: the definition says *what the table is*; the
2x2 commit/storage properties (LOCATION, catalog-managed TBLPROPERTIES, ...) are the
per-backend INSTANTIATOR's job. So the SAME definition instantiates on pure-duckdb,
Databricks, Iceberg, ... — only the instantiator changes. One definition can be
instantiated into MANY tables (a table per parallel test, several data variants).

DUCKDB IS THE CONVERTER. We never hand-parse the SQL or hand-declare column types:
we run the definition body through the located duckdb CLI (the `<build>/duckdb` next to
the unittest binary — same one `--repl` uses; NO python-duckdb dependency) and read
back the resolved schema (`DESCRIBE`) + rows (`SELECT *`) as JSON. That canonical
`Table` is what non-duckdb instantiators translate.

LAZY BY CONSTRUCTION. A `Fixture(name)` is a pure value — it does NO I/O. Nothing is
read or instantiated until a test that *runs* asks for it (via the backend's
`resources` fixture at test-setup time). Collecting or skipping a test touches no
fixture files. Resolution/caching policy is the consumer's `resources` fixture's job,
not this module's — the loader stays pure so the consumer controls scope.

THREE KINDS of `source=` (driver/requires.py):
  * `Fixture("simple_table")` — this module: SQL definition + seed, instantiated via duckdb.
  * a generator (future)      — computed/large data (e.g. tpc*); duckdb -> parquet.
  * an FQN string / Clone     — CTAS from a pre-existing source (only earns its keep at
                                scale; the legacy Databricks path). Not self-contained.

INSTANTIATOR SEAM. The generic core (format, load, canonicalize) lives here; the
per-backend step is a registered `Instantiator`. `DuckDBInstantiator` is the built-in
default (instantiate into a duckdb db file). A backend registers its own from a
conftest, the same scoped seam as provision.py's `register_provisioner`.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass, field, replace


class FixtureError(RuntimeError):
    """A fixture failed to load, instantiate, or introspect."""


class _Unset:
    """Sentinel: a `Fixture`'s seed was not overridden — use the fixture's own seed."""

    def __repr__(self):
        return "UNSET"


_UNSET = _Unset()


def resolve_seed(seed, default_rows):
    """Effective rows to load for one instantiation.

    `_UNSET` -> the fixture's own seed (`default_rows`); `None` -> empty table (drop the
    coupled seed); a list of row tuples -> replace the seed with those rows.
    """
    if seed is _UNSET:
        return default_rows
    if seed is None:
        return []
    return list(seed)


# ---------------------------------------------------------------------------
# Fixture reference (a NAMED entry, not a path) + parsed definition
# ---------------------------------------------------------------------------


def _stem(path: str) -> str:
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    return base[:-4] if base.endswith(".sql") else base


@dataclass(frozen=True)
class Fixture:
    """A reference to a named table fixture — resolved by the framework, not a path.

    Write it in `@requires(source=Fixture("simple_table"), ...)`. The name is LOGICAL
    (extension-less by convention; a trailing `.sql` is tolerated and stripped). It is
    a pure value: constructing it does no I/O — resolution against the search path
    happens only when a running test instantiates it (see `load_fixture`).

    Seed override: a fixture's `.sql` carries a default seed; `.Seed(None)` yields an
    empty table (schema only), `.Seed(rows)` replaces the seed, and omitting `.Seed`
    (the `_UNSET` default) uses the fixture's own seed. This is the early, ref-site form
    of the def-vs-data split.

    Reserved for the next iteration (see docs/FIXTURES.md): a `domain` to select among
    multiple registered fixture roots (e.g. per-extension), explicit-kind constructors
    (`Fixture.parquet(...)`, `Fixture.gen("tpch", sf=1)`), and a fuller `.Table`/`.Data`
    split. Kept out of the constructor until designed.
    """

    name: str
    seed: object = _UNSET  # _UNSET=use the fixture's seed; None=empty; list=replace

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Fixture(name): name must be a non-empty string")
        if self.name.endswith(".sql"):
            object.__setattr__(self, "name", self.name[:-4])

    def Seed(self, rows):
        """Return a copy with the seed overridden: `None` => empty table, a list of row
        tuples => replace the fixture's coupled seed. (Omit to keep the fixture's own.)"""
        return replace(self, seed=rows)


@dataclass(frozen=True)
class FixtureDef:
    """A resolved+parsed fixture DEFINITION: the leading `-- key: value` header + body.

    `body` is the ENTIRE file text (header lines are valid SQL comments, so a duckdb
    instantiator runs it verbatim). `name` is the header `fixture:` or the file stem.
    Produced by `load_fixture`/`parse_fixture` — never constructed by test authors.
    """

    name: str
    header: dict
    body: str
    path: str = None

    def table(self) -> str:
        """The table this definition creates — header `table:`/`fixture:`, else None
        (introspection then discovers the single table the body creates)."""
        return self.header.get("table") or self.header.get("fixture")

    def keys(self) -> list:
        """Declared logical key columns (header `keys: [a, b]`), or []."""
        return _parse_list(self.header.get("keys", ""))


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # DuckDB logical type as DESCRIBE reports it (e.g. INTEGER, VARCHAR, DECIMAL(10,2)).
    nullable: bool = True


@dataclass(frozen=True)
class Table:
    """An instantiated table in canonical, backend-agnostic form: schema + seed data.

    This is the hand-off to the per-backend instantiator: a duckdb instantiator ignores
    it (it just ran the SQL); a Spark/Iceberg instantiator maps `columns` to its own DDL
    and emits `seed_data` (as VALUES, or via a parquet the same duckdb wrote). A table
    "may or may not contain seed data" — an empty definition yields `seed_data == []`.
    """

    name: str
    columns: list  # list[Column] — the definition
    seed_data: list  # list[tuple], column order matches `columns` (the optional data)
    keys: list = field(default_factory=list)
    fixture: FixtureDef = None

    def column_names(self) -> list:
        return [c.name for c in self.columns]


_HEADER_RE = re.compile(r"^--\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


def _parse_list(raw: str) -> list:
    """Parse `[a, b, c]` (or bare `a, b`) into a list of trimmed tokens; '' -> []."""
    raw = raw.strip()
    if not raw:
        return []
    raw = raw.strip("[]")
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def parse_fixture(text: str, path: str = None) -> FixtureDef:
    """Parse a definition's leading `-- key: value` header block; body = the whole text.

    The header is the run of leading comment/blank lines; parsing stops at the first
    SQL statement. Non-`key: value` comment lines in the block are ignored.
    """
    header = {}
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if not s.startswith("--"):
            break
        m = _HEADER_RE.match(s)
        if m:
            header[m.group(1)] = m.group(2).strip()
    stem = _stem(path) if path else "fixture"
    return FixtureDef(name=header.get("fixture") or stem, header=header, body=text, path=path)


def load_fixture(ref, search_paths) -> FixtureDef:
    """Resolve a `Fixture`/str ref against `search_paths` and parse it (reads the file).

    This is the ONLY file-reading entry point; call it at test runtime, not collection.
    `ref` may carry an explicit `.sql` or not. First match on the path wins.
    """
    rel = ref.name if isinstance(ref, Fixture) else str(ref)
    if not rel.endswith(".sql"):
        rel += ".sql"
    tried = []
    for base in search_paths:
        p = os.path.join(str(base), rel)
        tried.append(p)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return parse_fixture(f.read(), p)
    raise FileNotFoundError(f"fixture {rel!r} not found; looked in: {tried}")


# ---------------------------------------------------------------------------
# DuckDB middleman — instantiate + introspect via the located CLI
# ---------------------------------------------------------------------------


def duckdb_cli_for(unittest_binary: str) -> str:
    """Derive the duckdb CLI path from the unittest binary path.

    Mirrors plugin._launch_cli: the CLI is `<build>/duckdb`, two dirs up from
    `<build>/test/unittest`. Same resolution the `--repl` path uses.
    """
    build_dir = os.path.dirname(os.path.dirname(unittest_binary))
    return os.path.join(build_dir, "duckdb")


def _run_json(duckdb_bin: str, db_path: str, sql: str, *, readonly: bool = False) -> list:
    args = [duckdb_bin, "-json"]
    if readonly:
        args.append("-readonly")
    args += [db_path, "-c", sql]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FixtureError(f"duckdb failed ({' '.join(args[:-1])} ...):\n{proc.stderr.strip()}")
    out = proc.stdout.strip()
    return json.loads(out) if out else []


def _exec(duckdb_bin: str, db_path: str, sql: str, *, what: str) -> None:
    proc = subprocess.run([duckdb_bin, db_path, "-c", sql], capture_output=True, text=True)
    if proc.returncode != 0:
        raise FixtureError(f"{what} failed in duckdb:\n{proc.stderr.strip()}")


def instantiate_db(duckdb_bin: str, definition: FixtureDef, db_path: str) -> None:
    """Run the definition body into a duckdb database file (creates/appends to it)."""
    _exec(duckdb_bin, db_path, definition.body, what=f"fixture {definition.name!r} body")


def _sql_literal(v) -> str:
    """Render a Python value as a DuckDB SQL literal for a VALUES row."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def introspect(duckdb_bin: str, db_path: str, *, table: str = None, keys=None) -> Table:
    """Read back the resolved schema + seed data for `table` in an existing duckdb db.

    With `table=None`, discovers the single user table in schema `main` (errors if the
    body created zero or several — name it via the `table:` header then).
    """
    if table is None:
        found = _run_json(
            duckdb_bin,
            db_path,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name",
            readonly=True,
        )
        names = [r["table_name"] for r in found]
        if len(names) != 1:
            raise FixtureError(
                f"expected exactly one table, found {names or 'none'}; set a `-- table:` header to disambiguate"
            )
        table = names[0]
    described = _run_json(duckdb_bin, db_path, f'DESCRIBE "{table}"', readonly=True)
    columns = [Column(c["column_name"], c["column_type"], c["null"] == "YES") for c in described]
    data = _run_json(duckdb_bin, db_path, f'SELECT * FROM "{table}"', readonly=True)
    seed_data = [tuple(d[c.name] for c in columns) for d in data]
    return Table(name=table, columns=columns, seed_data=seed_data, keys=list(keys or []))


def canonicalize(duckdb_bin: str, definition: FixtureDef, *, workdir: str = None) -> Table:
    """Instantiate a definition into a throwaway duckdb db and read back its canonical form.

    The one call a non-duckdb instantiator needs: SQL definition in, `Table` out.
    """
    import tempfile

    tmp = tempfile.mkdtemp(prefix="fixture.", dir=workdir)
    db = os.path.join(tmp, f"{definition.name}.duckdb")
    instantiate_db(duckdb_bin, definition, db)
    t = introspect(duckdb_bin, db, table=definition.table(), keys=definition.keys())
    return Table(t.name, t.columns, t.seed_data, t.keys, definition)


# ---------------------------------------------------------------------------
# Instantiator protocol + registry (per backend) — mirrors provision.py's seam
# ---------------------------------------------------------------------------


class DuckDBInstantiator:
    """Built-in default instantiator: the target IS duckdb, so just run the definition SQL.

    `instantiate(definition, db_path, duckdb_bin=...)` runs the body into `db_path` (the
    file is the isolation boundary — one db per test) and returns the `Table`. No type
    mapping, no seed translation: pure-duckdb is the trivial backend.
    """

    provider = "duckdb"

    def instantiate(self, definition: FixtureDef, target: str, *, duckdb_bin: str, seed=_UNSET) -> Table:
        if seed is _UNSET:
            # Default: run the body verbatim (its own CREATE + seed INSERTs).
            instantiate_db(duckdb_bin, definition, target)
        else:
            # Seed overridden: rebuild from the canonical schema + the effective rows,
            # so `.Seed(None)` yields an empty table and `.Seed(rows)` replaces the seed.
            base = canonicalize(duckdb_bin, definition, workdir=os.path.dirname(target) or None)
            rows = resolve_seed(seed, base.seed_data)
            cols = ", ".join(f'"{c.name}" {c.type}' for c in base.columns)
            stmts = [f'CREATE TABLE "{base.name}" ({cols});']
            if rows:
                vals = ", ".join("(" + ", ".join(_sql_literal(x) for x in r) + ")" for r in rows)
                stmts.append(f'INSERT INTO "{base.name}" VALUES {vals};')
            _exec(duckdb_bin, target, "\n".join(stmts), what=f"fixture {base.name!r} (seed override)")
        t = introspect(duckdb_bin, target, table=definition.table(), keys=definition.keys())
        return Table(t.name, t.columns, t.seed_data, t.keys, definition)


def map_columns(table: Table, type_map: dict, *, on_missing: str = "error") -> list:
    """Translate a table's DuckDB column types to a provider's DDL types.

    The generic helper a backend instantiator uses; the MAP itself ships with the
    provider (e.g. {"INTEGER": "INT", "VARCHAR": "STRING", ...} for Spark/Databricks).
    Matches on the base type name (`DECIMAL(10,2)` -> `DECIMAL`). `on_missing`: "error"
    (fail loud — the safe default) or "passthrough" (keep the duckdb spelling).
    """
    out = []
    for col in table.columns:
        base = col.type.split("(", 1)[0].strip().upper()
        target = type_map.get(base)
        if target is None:
            if on_missing == "error":
                raise FixtureError(
                    f"no type mapping for DuckDB type {col.type!r} (column {col.name!r}); "
                    "the provider's instantiator must extend its type map"
                )
            target = col.type
        # carry parameterization (e.g. DECIMAL(10,2)) through when the target keeps it
        params = col.type[len(base) :] if col.type[len(base) :].startswith("(") else ""
        out.append((col.name, target + (params if "(" not in target else "")))
    return out


_INSTANTIATOR_ATTR = "_driver_instantiators"  # list[(scope_dir|None, instantiator)]


def register_instantiator(config, instantiator, scope=None):
    """Register a per-backend instantiator, scoped to a dir (call from a conftest).

    Same scoping semantics as provision.register_provisioner: resolution is by TEST
    LOCATION so a mixed selection routes each subtree to its own instantiator.
    `scope=None` registers a global fallback. `DuckDBInstantiator` is the natural default.
    """
    regs = getattr(config, _INSTANTIATOR_ATTR, None)
    if regs is None:
        regs = []
        setattr(config, _INSTANTIATOR_ATTR, regs)
    regs.append((os.path.abspath(str(scope)) if scope is not None else None, instantiator))


def get_instantiator(config, path=None):
    """Return the instantiator for `path` (nearest-ancestor scope), else the global one,
    else a `DuckDBInstantiator()`. See provision.get_provisioner for the scoping rules."""
    regs = getattr(config, _INSTANTIATOR_ATTR, None)
    if not regs:
        return DuckDBInstantiator()
    if path is None:
        return regs[-1][1]
    p = os.path.abspath(str(path))
    best, best_len, fallback = None, -1, None
    for scope, inst in regs:
        if scope is None:
            fallback = inst
        elif (p == scope or p.startswith(scope + os.sep)) and len(scope) > best_len:
            best, best_len = inst, len(scope)
    if best is not None:
        return best
    return fallback if fallback is not None else DuckDBInstantiator()
