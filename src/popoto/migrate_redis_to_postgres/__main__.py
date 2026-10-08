"""``python -m popoto.migrate_redis_to_postgres``: the CLI entry (#756).

Deliberately thin. Under ``-m`` this file runs as ``__main__``, and the tool
itself is imported as the package ``popoto.migrate_redis_to_postgres`` --
the same module an operator's mapping file imports ``ModelMapping`` from. If
the logic lived here, ``-m`` would load it a second time under another name,
and every ``--mapping`` would fail its ``isinstance(ModelMapping)`` check.

It is also the target of the ``popoto-migrate-redis-to-postgres`` console
script (``[project.scripts]``): :func:`console_main` imports this module under
its package name, so the same rule holds there.
"""

import os
import sys
from typing import Optional, Sequence

from popoto.migrate_redis_to_postgres import main


def console_main(argv: Optional[Sequence[str]] = None) -> int:
    """The console script. ``python -m`` puts the working directory on
    ``sys.path``, which is how ``--mapping myapp.memory_migration:MAPPINGS``
    finds the operator's module; a console script starts with its own ``bin``
    directory there instead. Add the working directory, so both forms import
    the same modules."""
    cwd = os.getcwd()
    if cwd not in sys.path and "" not in sys.path:
        sys.path.insert(0, cwd)
    return main(argv)


if __name__ == "__main__":
    sys.exit(main())
