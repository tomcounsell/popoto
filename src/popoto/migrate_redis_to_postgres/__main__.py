"""``python -m popoto.migrate_redis_to_postgres``: the CLI entry (#756).

Deliberately thin. Under ``-m`` this file runs as ``__main__``, and the tool
itself is imported as the package ``popoto.migrate_redis_to_postgres`` --
the same module an operator's mapping file imports ``ModelMapping`` from. If
the logic lived here, ``-m`` would load it a second time under another name,
and every ``--mapping`` would fail its ``isinstance(ModelMapping)`` check.
"""

import sys

from popoto.migrate_redis_to_postgres import main

if __name__ == "__main__":
    sys.exit(main())
