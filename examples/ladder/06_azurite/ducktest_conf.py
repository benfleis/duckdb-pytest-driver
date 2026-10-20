from ducktest import AzureServiceDef, DuckDBEngineDef, Project, default

# The run uses an Azurite that answers at DUCKTEST_SERVICE_HOST:DUCKTEST_AZ_LOCAL_PORT, else starts one in docker.
LOCAL_AZ = AzureServiceDef.azurite("local_az")
LOCAL_BLOBS = LOCAL_AZ.storage("local_blobs", path="lake")
LOCAL_KEYS = LOCAL_AZ.credential("local_keys", scopes=[LOCAL_BLOBS])

project = Project(
    engines=[DuckDBEngineDef("duck", requires=["azure"])],
    rules=[default(storage=LOCAL_BLOBS, temp=LOCAL_BLOBS)],  # temp=: {TEMP_DIR} is on the server
)
