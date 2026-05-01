# Test fixtures

This directory holds two flavors of fixture data, kept strictly separate.

## Layout

```
tests/fixtures/
├── sdm/
│   ├── samples/          # pre-committed, fictional, hand-crafted (cam side)
│   └── captured/         # operator-captured from real hardware (gitignored)
└── foyer/
    ├── samples/          # pre-committed, fictional, hand-crafted (wifi side)
    └── captured/         # operator-captured from real hardware (gitignored)
```

## `samples/` — pre-committed fictional fixtures

Files in `samples/` ship with the repo and are used by unit tests in Phase 1+.
**They contain no real identifiers.** Every `enterprises/...` path, every
device id, every structure id, every room name, every MAC address, every
PSK in a `samples/` file is a literal placeholder of the form
`{{PROJECT_ID}}`, `{{DEVICE_ID}}`, etc., or an obviously-fabricated value
(e.g. `Front Porch Doorbell`, `aa:bb:cc:dd:ee:01`).

Adding a new sample fixture:

1. Hand-craft the JSON. Do not copy from `captured/`.
2. Validate it parses: `python -c "import json; json.load(open('<path>'))"`.
3. Reference the SRD section that justifies the trait set / shape.
4. Open a PR.

## `captured/` — real-hardware output (NEVER commit raw)

Files in `captured/` are produced by `scripts/smoke-cam.py` and
`scripts/smoke-wifi.py` against the operator's actual Google account /
mesh during the Phase 0 onboarding gate (SRD §16.0). The smoke scripts
redact PII before writing — but `captured/` is **also** gitignored at the
repo root (see `.gitignore`) as a defense-in-depth measure.

If a captured fixture needs to feed the test corpus, the workflow is:

1. Run the smoke script — it writes redacted output to `captured/`.
2. **Human review.** Open the file. Diff it against the corresponding
   `samples/` skeleton. Confirm no `enterprises/...`, no UUIDs, no
   structure ids, no MAC bytes leaked through.
3. If review passes, manually copy the file into `samples/` (and rename
   if helpful for clarity).
4. Open a PR. The PR reviewer is the second pair of eyes on the redaction.
5. The original file in `captured/` stays gitignored.

**Never `git add tests/fixtures/sdm/captured/` or
`tests/fixtures/foyer/captured/`.** The redactor in the smoke scripts is
defense-in-depth, not the only defense — a captured fixture leaking PII
into the public repo is the failure mode this layout exists to prevent.
