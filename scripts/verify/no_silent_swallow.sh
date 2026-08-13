#!/bin/sh
# Anti-criterion: no silently swallowed exception in the import loop. Every
# caught exception must become a counted report entry. Expected: count == 0.
grep -rn -A1 "except.*:" src/popoto/transfer/import_.py | grep -c "pass$"
