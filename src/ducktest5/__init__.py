"""ducktest v0.5: scan -> plan -> provision -> exec -> cleanup, with no pytest underneath.

Imports nothing from `ducktest` (whose package init pulls in pytest); `ducktest` imports from here.
"""

from .declare import Engine, test  # noqa: F401
from .sqltext import L  # noqa: F401
