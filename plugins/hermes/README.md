# popoto-memory: Hermes wiring

```bash
pip install 'popoto[mcp]'
popoto-memory doctor
mkdir -p ~/.hermes/hooks/popoto-memory
cp plugins/hermes/HOOK.yaml plugins/hermes/handler.py ~/.hermes/hooks/popoto-memory/
hermes mcp add popoto-memory -- popoto-memory mcp
```

Two files, no config file to edit. Hermes hooks are Python, so `handler.py`
runs in the gateway process: the read path costs one Redis round trip with
no interpreter startup at all, which is the fastest of the four harnesses.

Injected context lands in the user message, never the system prompt. That is
Hermes's own documented behavior and the reason prompt caching survives
per-turn injection.

**Verification status:** the hook contract here comes from the Hermes
documentation, not from a live run. Hermes is not installed on any machine
this repo is developed on, so the payload fixtures under
`tests/fixtures/harness_payloads/hermes_*.json` are docs-derived and the
round-trip tests prove our reading of the docs rather than the harness. If
the field names have moved, `popoto-memory doctor` will show no `last
assemble` timestamp; the adapter change is one function in
`popoto/integrations/hooks.py`.
