import os

from ducktest import Project, default, match
from ducktest.resources import StorageDef
from ducktest.services.http import HttpServerDef

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

HTTP = HttpServerDef("http", source=DATA)
# No machine has 192.0.2.1 (it is reserved for documentation), so this server never starts.
NOWHERE = HttpServerDef("nowhere", source=DATA, host="192.0.2.1")

SERVED = StorageDef("served", "{http.url}", inputs=[HTTP])
UNSERVED = StorageDef("unserved", "{nowhere.url}", inputs=[NOWHERE])

project = Project(
    storages=[SERVED, UNSERVED],
    # `data=` makes the server's root the tests' {DATA_DIR}.
    rules=[
        default(storage=SERVED, data=SERVED),
        match("blocked.test", storage=UNSERVED, data=UNSERVED),
    ],
)
