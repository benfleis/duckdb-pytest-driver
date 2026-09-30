"""An embedded PySpark session with Delta Lake, as an engine booted once per run. pyspark is imported at boot, not here."""

import contextlib
import logging
import os

from ..resources.client import SqlError
from ..resources.engine import BootedEngineDef

_log = logging.getLogger(__name__)


class SparkHandle:
    def __init__(self, session):
        self.session = session
        self._conf = None  # the session's configuration when this worker's clone opened

    def run(self, statements):
        result, rows = None, []
        for n, statement in enumerate(statements):
            try:
                result = self.session.sql(statement)
                rows = [tuple(_plain(v) for v in r) for r in result.collect()]
            except Exception as exc:  # py4j and pyspark raise unrelated types; all mean "the SQL failed"
                raise SqlError(_message(exc), statement=n) from None
        return (list(result.columns) if result is not None else []), rows

    def for_worker(self):
        """A session of this worker's own, for a run with several workers. A clone keeps the
        configuration the engine was given (a brand new session starts with none) and has its own
        temp views, so `begin_test` here cannot drop the views of a test running beside it. The JVM,
        the context and the cache stay the run's, and so does stopping them."""
        from pyspark.sql import SparkSession

        clone = SparkHandle(SparkSession(self.session.sparkContext, self.session._jsparkSession.cloneSession()))
        clone._conf = clone._current_conf()
        return clone

    def _current_conf(self):
        return {key: value for key, value in self.session.sql("SET").collect()}

    def begin_test(self):
        """One session serves every test of a worker, so drop what a test can leave behind in it:
        its temp views, the cache, and every configuration it set (`SET`), which goes back to what
        the clone had when it opened."""
        for view in self.session.catalog.listTables():
            if view.isTemporary:
                self.session.catalog.dropTempView(view.name)
        self.session.catalog.clearCache()
        if self._conf is None:
            return
        now = self._current_conf()
        changed = sorted(k for k in now.keys() | self._conf.keys() if now.get(k) != self._conf.get(k))
        if changed:
            _log.debug("spark conf restored by reset: %s", ", ".join(changed))
        for key in changed:
            if key in self._conf:
                self.session.conf.set(key, self._conf[key])
            else:
                self.session.conf.unset(key)

    def scope_jobs(self, test_id):
        """Scopes this session's jobs to `test_id`, so a test's timeout can cancel just its own work
        (`cancel_jobs`) without touching another test running on the same JVM."""
        self.session.sparkContext.setJobGroup(test_id, test_id)

    def cancel_jobs(self, test_id):
        self.session.sparkContext.cancelJobGroup(test_id)

    def set_conf(self, key, value):
        self.session.conf.set(key, value)

    def close(self):
        """A worker's clone shares the run's context; stopping it would stop the run's. The run
        stops that one."""

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
        # Its default is spark-warehouse/ in the working directory, which Spark makes at boot.
        .config("spark.sql.warehouse.dir", os.path.join(os.path.dirname(os.path.abspath(log_path)), "spark-warehouse"))
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


def delta_spark(id="spark", *, s3a=False, **fields):
    """Local Spark with the delta-spark that matches the installed pyspark. `s3a=True` puts the
    hadoop-aws that matches Spark's Hadoop on the classpath, so it can take an s3 storage as input.
    `fields` are those of every engine (`settings`, `lifetime`, ...)."""
    from importlib.metadata import PackageNotFoundError, version

    def v(dist):
        try:
            return ".".join(version(dist).split(".")[:2])
        except PackageNotFoundError:
            return "missing"

    packages = (f"org.apache.hadoop:hadoop-aws:{_hadoop_version()}",) if s3a else ()
    booted = f"spark:{v('pyspark')}:delta-{v('delta-spark')}:local" + (":s3a" if s3a else "")
    return BootedEngineDef(
        id, version=booted, boot=lambda log: _boot_delta(log, packages)[0], dialect="spark", **fields
    )
