# Security and privacy

Colink is a single-user, read-only code mirror and MCP server. It is not a
system sandbox, a complete secret scanner or a shared multi-tenant service.

- Only start sharing after selecting a specific project and confirming access.
- MCP tools cannot execute commands or write to source directories.
- Private tunnel traffic is initiated outbound. Do not expose local management
  endpoints or disable firewall/security controls to complete installation.
- Query responses containing source code are sent to the selected OpenAI
  product. Read-only does not mean private data never leaves the Mac.
- Mandatory filters exclude known credentials/files, binaries and symlinks;
  this is defense in depth, not a guarantee against every embedded secret.
- Each project retains current and immediately previous code states. Changing
  ignore rules does not instantly remove the previous state or revoke data
  already returned to a client.
- Runtime keys live in user-owned mode-600 files, not in source, Git, application
  bundles, command-line arguments or diagnostic output. Keychain is not yet
  implemented. Do not grant this preview production-wide credentials.
- Official tunnel client versions and application archives are checksum-checked.
  Checksums and ad-hoc signatures are integrity checks, not Apple notarization.

The GitHub distribution is independent of the ChatGPT public plugin directory.
Every user must configure their own tunnel and workspace permissions. Do not
copy or distribute an existing user's `.env.local`, `.code-context`, app runtime
settings, personal connection manifest or code mirror.

## Reporting a vulnerability

Do not disclose runtime keys, private source or exploit payloads in public
issues. Use GitHub's private vulnerability reporting when enabled. If that
option is unavailable, open a public issue saying only that you need a private
security contact; do not include the confidential details. Ordinary bugs can
use Issues with reproduction steps and sanitized errors.

No production security audit or universal account-availability guarantee is
claimed for this early release.
