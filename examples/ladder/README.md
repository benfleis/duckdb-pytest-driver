# The ladder

Each rung is the test directory of a repo that uses ducktest5, and each adds one idea to the one
before. Run these from inside a rung, e.g. `cd examples/ladder/02_rules`:

    ducktest5 scan --env                what each test is given, and where each part came from
    ducktest5 select --explain          what a bare run takes, which rule gave each test what, and why not
    ducktest5 run --no-binary-check     run it

`run` needs a DuckDB build: set `BUILD_DIR` to the directory that holds `duckdb` and `test/unittest`.
`--no-binary-check` is there because the build does not list these `.test` files.

## 01_solo

One Python body, and no `ducktest_conf.py` in this directory or above it. The body names nothing, so
it gets the default DuckDB Session, and it runs one query.
