from ducktest import Project, default
from ducktest.engines.duckdb import DuckDBEngineDef
from ducktest.services.azure import AzureServiceDef

# The run uses an Azurite that answers at DUCKTEST_SERVICE_HOST:DUCKTEST_AZ_LOCAL_PORT, else starts one in docker.
AZ = AzureServiceDef.azurite()
BLOBS = AZ.storage("blobs", path="lake")

project = Project(
    engines=[DuckDBEngineDef("duck", requires=["azure"])],
    storages=[BLOBS],
    credentials=[AZ.credential("keys", scopes=[BLOBS])],
    rules=[default(storage=BLOBS, temp=BLOBS)],  # temp=: {TEMP_DIR} is on the server
)
