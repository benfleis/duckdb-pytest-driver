from ducktest import Project, default
from ducktest.engines.duckdb import DuckDBEngineDef
from ducktest.services.s3 import S3ServiceDef

# The run uses a server that answers at DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT, else starts one in docker.
S3 = S3ServiceDef.rustfs()
LAKE = S3.storage("lake", path="lake")

project = Project(
    engines=[DuckDBEngineDef("duck", requires=["httpfs"])],
    storages=[LAKE],
    credentials=[S3.credential("keys", scopes=[LAKE])],
    rules=[default(storage=LAKE, temp=LAKE)],  # temp=: {TEMP_DIR} is on the server
)
