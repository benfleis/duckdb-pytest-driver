from ducktest5 import default
from ducktest5.engines.duckdb import duckdb_engine
from ducktest5.services.s3local import s3_local

# The run uses a server that answers at DUCKTEST_SERVICE_HOST:DUCKTEST_S3_PORT, else starts one in docker.
S3 = s3_local()

ENGINES = [duckdb_engine("duck", requires=["httpfs"])]
STORAGES = [S3.storage("lake", "lake")]
CREDENTIALS = [S3.credential("keys", scopes=["lake"])]
RULES = [default(bind={"storage": "lake"})]
