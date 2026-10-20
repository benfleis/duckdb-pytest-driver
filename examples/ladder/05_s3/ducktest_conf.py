from ducktest import DuckEngineDef, Project, S3ServiceDef, default

# The run uses a server that answers at DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT, else starts one in docker.
LOCAL_S3 = S3ServiceDef.rustfs("local_s3")
LOCAL_LAKE = LOCAL_S3.storage("local_lake", path="lake")
LOCAL_KEYS = LOCAL_S3.credential("local_keys", scopes=[LOCAL_LAKE])

project = Project(
    engines=[DuckEngineDef("duck", requires=["httpfs"])],
    rules=[default(storage=LOCAL_LAKE, temp=LOCAL_LAKE)],  # temp=: {TEMP_DIR} is on the server
)
