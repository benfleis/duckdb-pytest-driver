"""An embedded PySpark session with Delta Lake, as an `Engine`. pyspark is imported at boot, not here."""

import contextlib
import os

from ..client import SqlError
from ..declare import Engine


class SparkHandle:
    def __init__(self, session):
        self.session = session

    def run(self, statements):
        result, rows = None, []
        for n, statement in enumerate(statements):
            try:
                result = self.session.sql(statement)
                rows = [tuple(_plain(v) for v in r) for r in result.collect()]
            except Exception as exc:  # py4j and pyspark raise unrelated types; all mean "the SQL failed"
                raise SqlError(_message(exc), statement=n) from None
        return (list(result.columns) if result is not None else []), rows

    def begin_test(self):
        """One session serves every test, so drop what a test can leave behind in it."""
        for view in self.session.catalog.listTables():
            if view.isTemporary:
                self.session.catalog.dropTempView(view.name)
        self.session.catalog.clearCache()

    def use_service(self, service_id, block):
        """Point s3a at an S3-compatible service. Spark copies SQL confs into the Hadoop conf of each
        query, so this works after boot; the jar must already be on the classpath (`s3a=True`)."""
        for key, value in {
            "fs.s3a.endpoint": block["endpoint"],
            "fs.s3a.endpoint.region": block["region"],
            "fs.s3a.path.style.access": str(block["url_style"] == "path").lower(),
            "fs.s3a.connection.ssl.enabled": str(block["use_ssl"]).lower(),
            "fs.s3a.access.key": block["access_key"],
            "fs.s3a.secret.key": block["secret_key"],
            "fs.s3a.aws.credentials.provider": "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider",
            # so that a body can give Spark the same `s3://` URI it gives DuckDB
            "fs.s3.impl": "org.apache.hadoop.fs.s3a.S3AFileSystem",
        }.items():
            self.session.conf.set(key, value)
            self.session.sparkContext._jsc.hadoopConfiguration().set(key, value)

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
    lines = [
        line
        for line in str(exc).splitlines()
        if line.strip() and not line.startswith(("\tat ", "An error occurred while calling"))
    ]
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


def _boot_delta(log_path, packages=()):
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
        # Delta rebuilds a table's state as a 50-partition job by default: 50 tasks for a log of a
        # few commits, on every statement. Test tables are tiny.
        .config("spark.databricks.delta.snapshotPartitions", "1")
        .config("spark.default.parallelism", "2")
    )
    with _stdio_to(log_path):
        session = configure_spark_with_delta_pip(builder, extra_packages=list(packages)).getOrCreate()
        session.sparkContext.setLogLevel("ERROR")
    return SparkHandle(session), pyspark.__version__


def _hadoop_version():
    import glob

    import pyspark

    jars = glob.glob(os.path.join(os.path.dirname(pyspark.__file__), "jars", "hadoop-client-api-*.jar"))
    return os.path.basename(jars[0])[len("hadoop-client-api-") : -len(".jar")] if jars else None


def delta_spark(id="spark", *, s3a=False, uses=()):
    """Local Spark with the delta-spark that matches the installed pyspark. `s3a=True` puts the
    hadoop-aws that matches Spark's Hadoop on the classpath, so the engine can use an S3 service."""
    from importlib.metadata import PackageNotFoundError, version

    def v(dist):
        try:
            return ".".join(version(dist).split(".")[:2])
        except PackageNotFoundError:
            return "missing"

    packages = (f"org.apache.hadoop:hadoop-aws:{_hadoop_version()}",) if s3a else ()
    variant = f"spark:{v('pyspark')}:delta-{v('delta-spark')}:local" + (":s3a" if s3a else "")
    return Engine(id, variant, lambda log: _boot_delta(log, packages)[0], uses=tuple(uses))
