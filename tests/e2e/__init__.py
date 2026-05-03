"""End-to-end CliRunner tests for ``nest-cli`` (wifi + auth verbs).

These tests invoke the actual Click commands through ``CliRunner.invoke``
and assert against:

1. Exit code (0 / 2 / 3 / 4 / 5 / 6 / 64) per SRD §11.1.
2. JSONL envelope shape per FR-9a / SRD §11.3 (``{family, error?, ...}``).
3. The mocked ``FoyerClient._rest`` seam — confirms the verb hit the right
   ``(method, path, json, params)`` tuple without touching network.
4. Click argument parsing — positional args, ``--flag`` options, ``GOOGLE_*``
   env vars, missing-required-arg → exit 64.
5. Error envelopes carrying ``family="wifi"`` for the SRD-aligned wifi side.

The per-verb tests under ``tests/wifi/`` and ``tests/auth/`` cover individual
client/credential code paths at a finer grain. This e2e tier is the operator-
shaped surface — the same arg list an operator types into a shell, with the
same exit code shell scripts will pattern-match.
"""
