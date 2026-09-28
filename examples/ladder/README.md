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

## 02_rules

`ducktest_conf.py` declares two local storages, and `default()` binds the role `storage` to
`scratch_a` for every test. Each `match()` rule names one file. `own_inputs.py` and
`paired_own_inputs.py` bind `storage` to `scratch_b` on `@test(...)`, and that wins over the rule;
`select --explain` shows both bindings. `default.test` is named by no `match()` and runs with what
`default()` gives. The rule of `excluded.test` declares it out of the default suite, so a bare run
leaves it out and `select --explain` names that rule; `--suite all` runs it.

Tests with the same bindings share a Session. The two `.test` files run in one `unittest` process,
although `rule_matched.test` has an inline `require json`: that is part of its body. A binding on
`@test` gives another Session. The batches in `02-plan.yaml`, in the run directory, show both.

## 03_credentials

The storage is now an S3 bucket that nothing reads. A credential scoped to it takes a fake key from
`LADDER_FAKE_KEY_ID` and `LADDER_FAKE_SECRET`; set both before a run. The axis `auth` makes two tests
of each body: `key` binds the credential, and `none` unsets the names it delivers, which hides it.
Each body asks DuckDB for its secrets: one, scoped to the bucket, under `auth=key`, and none under
`auth=none`. A Python body's Session creates the secret with the values. For a `.test` body the
init file holds only a template, `KEY_ID '{AWS_ACCESS_KEY_ID}'`, and the binary fills it from the
variables passed through to it; `scan --env` shows it as the `secret:` line. The init file and the
run's own records hold the names of the secrets, never their values.
