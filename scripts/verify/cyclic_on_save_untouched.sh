#!/bin/sh
# Anti-criterion (#556): CyclicDecayField.on_save body untouched. Hunk CONTENT,
# not --stat, which prints only a filename. Expected: match count == 0.
git diff main -U0 -- src/popoto/fields/cyclic_decay_field.py | grep -c '^[+-].*def on_save'
