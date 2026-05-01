# Security policy

## Reporting a vulnerability

Report security issues **privately** to <!-- TODO: replace with security contact email before v0.1.0 release --> rather than opening a public GitHub issue.

This is especially important for any vulnerability touching:

- OAuth client credentials, refresh tokens, or access tokens
- The cam credentials file format (`~/.config/nest-cli/credentials-cam.json`, see SRD §FR-CRED-3)
- The wifi credentials file format (`~/.config/nest-cli/credentials-wifi.json`, see SRD §FR-CRED-8)
- The Foyer master-token bootstrap path (SRD §3.2.1, §5.2)

**Do NOT open public issues for OAuth-related vulnerabilities.** Public disclosure of a credential-handling bug could expose every operator running the affected version before a fix ships.

## Threat model

See [`docs/SRD-nest-cli.md`](SRD-nest-cli.md) §4.7 for the full threat model, including the asymmetric trust posture between the cam side (Google-blessed SDM API) and the wifi side (reverse-engineered Foyer service).

## Supported versions

Until v0.1.0 ships, only the latest tagged release receives security fixes. The support matrix will be expanded once the project reaches a stable version.
