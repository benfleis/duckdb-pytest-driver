"""ducktest v0.5: scan -> plan -> provision -> exec -> cleanup, with no pytest underneath.

Imports nothing from `ducktest` (whose package init pulls in pytest); `ducktest` imports from here.
"""

from .conf import default, match  # noqa: F401
from .declare import test  # noqa: F401
from .outcomes import Skip, skip  # noqa: F401
from .resources import EngineDef, ServiceDef  # noqa: F401
from .sqltext import L  # noqa: F401
