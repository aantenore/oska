# Security policy

## Reporting a vulnerability

Report it privately through GitHub: open the repository's **Security** tab and
choose **Report a vulnerability**
(<https://github.com/aantenore/oska/security/advisories/new>). Do not open a
public issue for it.

Include the version, what you did, what you expected and what happened. If you
can, include a minimal pack or request that reproduces it. The report is read
by the maintainer, who replies in the advisory.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

The project is at an early stage. Only the latest release receives fixes.

## Security model

The full account is in [docs/governance.md](docs/governance.md). In short:

- **A write is approved by its exact payload.** The payload is hashed in its
  canonical form, the person approves that payload with its digest, and the
  write runs only if the payload still hashes the same
  ([Approval](docs/governance.md#approval)).
- **A write is read back.** A write is reported as done only after the system
  of record has been read ([Verification](docs/governance.md#verification)).
- **Identity is read in one place.** `identity.mode` is `proxy` (an
  authenticated proxy in front of the HTTP application sets the principal) or
  `token` (a signed session token checked before any claim is read). Proxy mode
  is unsafe on a port that anything but the proxy can reach
  ([Identity](docs/governance.md#identity)).
- **Secrets come from the environment only.** Keys, tokens and provider
  credentials are read from environment variables that configuration names;
  configuration files and catalogs hold the variable's name, never its value.
- **The serving process cannot change its catalog.** Publication composes a
  change request for a reviewer; putting it into a catalog is a person's act.

Reports about any of these properties not holding are in scope.
