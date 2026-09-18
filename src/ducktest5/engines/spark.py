"""An embedded PySpark session with Delta Lake, as an `Engine`. pyspark is imported at boot, not here."""

import contextlib
import os

from ..client import SqlError
from ..declare import Engine


class SparkHandle:
    def __init__(self, session):
        self.session = session

    def run(self, statements):
        result = None
        for n, statement in enumerate(statements):
            try:
                result = self.session.sql(statement)
                rows = [tuple(_plain(v) for v in r) for r in result.collect()]
            except Exception as exc:  # py4j and pyspark raise unrelated types; all mean "the SQL failed"
                raise SqlError(_message(exc), statement=n) from None
        return list(result.columns), rows

    def stop(self):
        self.session.stop()


def _plain(value):
    """A struct arrives as a Row, which is a tuple that has lost its field names in any other form."""
    if hasattr(value, "asDict"):
        return {k: _plain(v) for k, v in value.asDict().items()}
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return value.hex().upper()
    return value


def _message(exc):
    """The JVM exception's own message lines, without the py4j preamble and the Java stack."""
    lines = [l for l in str(exc).splitlines() if l.strip() and not l.startswith(("\tat ", "An error occurred while calling"))]
    return "\n".join(lines[:6]).lstrip(": ")


@contextlib.contextmanager
def _stdio_to(log_path):
    """The JVM launcher and ivy write to the inherited fds, so redirect those, not sys.stdout."""
    with open(log_path, "ab") as log:
        saved = os.dup(1), os.dup(2)
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        try:
            yield
        finally:
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])


def _boot_delta(log_path):
    import pyspark
    from delta import configure_spark_with_delta_pip
    from pyspark.sql import SparkSession

    builder = (
        SparkSession.builder.master("local[2]")
        .appName("ducktest")
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")
        .config("spark.ui.enabled", "false")
        .config("spark.ui.showConsoleProgress", "false")
        .config("spark.sql.shuffle.partitions", "2")
    )
    with _stdio_to(log_path):
        session = configure_spark_with_delta_pip(builder).getOrCreate()
        session.sparkContext.setLogLevel("ERROR")
    return SparkHandle(session), pyspark.__version__


def delta_spark(id="spark"):
    """Local Spark with the delta-spark that matches the installed pyspark."""
    from importlib.metadata import PackageNotFoundError, version

    def v(dist):
        try:
            return ".".join(version(dist).split(".")[:2])
        except PackageNotFoundError:
            return "missing"

    return Engine(id, f"spark:{v('pyspark')}:delta-{v('delta-spark')}:local", lambda log: _boot_delta(log)[0])
