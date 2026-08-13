#!/bin/sh
# Anti-criterion: the transfer driver must contain no isinstance check against
# a concrete field or mixin type. Expected: exit code 1 (no matches).
grep -rn "isinstance" src/popoto/transfer/ | grep -E "Field|Mixin"
