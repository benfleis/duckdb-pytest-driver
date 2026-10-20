from ducktest import Project, default
from ducktest.engines.duckdb import duckdb_engine
from ducktest.services.s3local import s3_local

# The run uses a server that answers at DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT, else starts one in docker.
S3 = s3_local(flavor="rustfs")
LAKE = S3.storage("lake", "lake")

project = Project(
    engines=[duckdb_engine("duck", requires=["httpfs"])],
    storages=[LAKE],
    credentials=[S3.credential("keys", scopes=[LAKE])],
    rules=[default(storage=LAKE, temp=LAKE)],  # temp=: {TEMP_DIR} is on the server
)
