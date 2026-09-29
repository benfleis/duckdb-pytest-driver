import os

from ducktest5 import default, match
from ducktest5.resources import StorageDef
from ducktest5.services.http import HttpServerDef

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

HTTP = HttpServerDef("http", source=DATA)
# No machine has 192.0.2.1 (it is reserved for documentation), so this server never starts.
NOWHERE = HttpServerDef("nowhere", source=DATA, host="192.0.2.1")

STORAGES = [
    StorageDef("served", "{http.url}", inputs=[HTTP]),
    StorageDef("unserved", "{nowhere.url}", inputs=[NOWHERE]),
]

RULES = [
    default(bind={"storage": "served"}),
    match("blocked.test", bind={"storage": "unserved"}),
]
