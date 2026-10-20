from ducktest import Project, default
from ducktest.engines.duckdb import duckdb_engine
from ducktest.services.azlocal import az_local

# The run uses an Azurite that answers at DUCKTEST_SERVICE_HOST:DUCKTEST_AZ_LOCAL_PORT, else starts one in docker.
AZ = az_local()
BLOBS = AZ.storage("blobs", "lake")

project = Project(
    engines=[duckdb_engine("duck", requires=["azure"])],
    storages=[BLOBS],
    credentials=[AZ.credential("keys", scopes=[BLOBS])],
    rules=[default(storage=BLOBS, temp=BLOBS)],  # temp=: {TEMP_DIR} is on the server
)
