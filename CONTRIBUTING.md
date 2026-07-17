# Contributing

Issues and focused pull requests are welcome.

## Before opening a change

1. Search existing issues.
2. Keep behavior changes small and explain the failure mode they address.
3. Add or update tests for recovery, replay, authorization, or audit behavior.
4. Do not commit runtime state, databases, audit keys, receipts, tokens, or private data.

## Local checks

```bash
python -m unittest discover -s tests -v
python -m build
```

The second command requires the optional `build` package: `python -m pip install build`.

## Design constraints

- Preserve stable idempotency keys across retries.
- Never automatically repeat a command with an uncertain outcome.
- Keep command execution on `shell=False`.
- Require explicit authorization for command and HTTP workflow steps.
- Keep the core runtime usable without third-party runtime dependencies.
