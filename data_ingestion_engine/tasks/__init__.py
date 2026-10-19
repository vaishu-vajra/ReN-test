# Ensure submodules are imported so @shared_task registers them.
from .downloads import *       # noqa: F401,F403
from .page_numbers import *    # noqa: F401,F403
from .extract_company import *  # noqa: F401,F403
from .housekeeping import *    # noqa: F401,F403
