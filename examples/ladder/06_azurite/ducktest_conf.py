from ducktest5 import default
from ducktest5.engines.duckdb import duckdb_engine
from ducktest5.services.azlocal import az_local

# The run uses an Azurite that answers at DUCKTEST_SERVICE_HOST:DUCKTEST_AZ_LOCAL_PORT, else starts one in docker.
AZ = az_local()

ENGINES = [duckdb_engine("duck", requires=["azure"])]
STORAGES = [AZ.storage("blobs", "lake")]
CREDENTIALS = [AZ.credential("keys", scopes=["blobs"])]
RULES = [default(bind={"storage": "blobs"})]
