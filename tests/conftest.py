# Enable pytest's `pytester` fixture so the collector self-tests can spin up an isolated
# pytest run (in a subprocess) against a stub unittest binary — no real duckdb build needed.
pytest_plugins = ["pytester"]
