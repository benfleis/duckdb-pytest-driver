"""ducktest v0.5: scan -> plan -> provision -> exec -> cleanup, with no pytest underneath.

Imports nothing from `ducktest` (whose package init pulls in pytest); `ducktest` imports from here.
"""

from .conf import default, match  # noqa: F401
from .declare import EngineDef, Variant, test  # noqa: F401
from .resources import ServiceDef  # noqa: F401
from .sqltext import L  # noqa: F401
