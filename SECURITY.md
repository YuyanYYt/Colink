# Security and privacy

The public download remains 0.5.0b2. Maintenance source 0.5.0b4 adds default-off,
project-scoped native development jobs. Actual source, native, webpage and installed
application evidence are recorded separately; no production security audit is claimed.
The legacy mirror protocol and existing databases retain their historical semantics.

Live reads verify original files without keeping a permanent whole-project mirror.
Task-scoped writes, moves and generated-result writeback go through WriteCoordinator,
with source hashes, path/identity checks and durable recovery records. Unknown recovery
objects and unresolved partial operations are preserved. Intentional code undo is not
exposed; use independently saved Git history. A task Diff is not a permanent backup or
a multi-file atomic filesystem transaction.

The webpage cannot register a source, enable local read/write/execute grants, expand
ports or network scope, or force recovery. Every tool uses an explicit project ID.
Local authorization and ChatGPT's action approval policy are separate. Original mirror
read tools remain read-only. Development status/output/list operations that renew a
service lease or acknowledge log cleanup are annotated as effects, not read-only.

## Native development boundary

- Only the currently validated Darwin 27 implementation is enabled. Other systems
  keep terminal execution unavailable. A private libproc ABI mismatch fails closed;
  there is no fallback to parent PID tracking or an unconfined command.
- Each job uses validated temporary input and a capacity-limited APFS disk. Original
  project writes are performed only through the existing write coordinator.
- Fixed SRT 0.0.78 generates Seatbelt profiles with narrower project policy. Controller
  directories, protected ancestors, inherited descriptors and system-fcntl 80/110 are
  guarded. Tool paths and precise dynamic-library/symlink identities are bound to plans.
  Controller credentials and user shell profiles are not inherited.
- launchd creates a dedicated kernel resource coalition for each job. Fork, exec,
  double-fork and setsid descendants remain supervised. Signals bind process birth
  identities through audit tokens. Cancellation is reported complete only after native
  exit/cleanup verification. Missing or changed recovery evidence blocks new execution.
- Project TCP binds are denied. The trusted controller binds explicit 127.0.0.1
  registered ports and forwards only to its exact Unix socket after checking the peer's
  kernel audit identity and coalition. Node has a service adapter; Python/JVM services
  need Unix socket support. Existing Tomcat/Spring TCP services are not directly supported.
- Package network access is limited to locally approved public registries through an
  authenticated proxy; other local ports and external endpoints are not opened wholesale.
- CPU, RSS and process-count controls are sampled supervision and stopping thresholds,
  not VM isolation or hard resource reservations. Short exited processes can be missed
  by CPU sampling. A successful exit and absent denial text do not prove containment.
- Pipe execution is noninteractive; stdin is closed except the private controller's
  bounded database capability transport. There is no project terminal input or PTY API.

Output, request metadata, helper input, temporary disks and dependency caches have
separate bounded budgets. Active or unverified work is not evicted to make room.
Completed logs are reclaimed after final acknowledgement or the unread retention limit;
failed summaries are separately bounded. Runtime-owned retired job resources may be
reclaimed by their exact recorded identities; evidence artifacts and user materials
are not general cache-cleanup targets. See docs/WEB_SESSION_DEVELOPMENT_PLAN.md.

## Ports, databases and Git

The port inventory reports listener/process ownership without returning executable
arguments. A manually started current-project service needs confirmation of the exact
process in the local app. PID birth, listener, executable and project identity are
rechecked before signalling; unknown/system/other-project processes cannot be released.
CoLink-owned current-project jobs use the job cancellation path.

Database profiles bind a project-specific endpoint, database and limited role.
Passwords are stored in the macOS Keychain and passed to jobs through bounded private
stdin, not command arguments, source files or ordinary metadata. Client presence, TCP
readiness, authenticated role checks, migrations, CRUD and vector operations remain
separate facts. PostgreSQL preflight checks inherited privileges and verifies that an
incorrect password is rejected. MySQL checks the server's actual `partial_revokes`
mode and parses database-level wildcard escapes before accepting project grants.
Global/cross-database privileges, grant options and inherited roles are refused.
Client connection parameters are bound to the profile and cannot be overridden or
abbreviated by project arguments. The final successful authentication is checked again
for privilege changes. First SHA2 authentication on an explicitly non-TLS local test
endpoint uses RSA public-key retrieval; this does not provide full-connection encryption.
Database server grants remain authoritative; these checks do not turn a SQL server into
a native sandbox. Qdrant execution is withheld until project-scoped authentication or a
dedicated instance can be enforced. Never configure production-wide administrative roles.

Local Git commits use a private index and only selected task files, with HEAD/index/
source conflict checks. User staging is preserved. Hooks, filters and external helpers
are disabled; push/reset/clean are not offered.

## Connection and distribution

Private tunnel traffic is initiated outbound. Do not expose local management endpoints
or disable firewall/security controls. Queried source and requested command output are
sent to the selected AI provider; a private connection does not mean data never leaves
the Mac. Mandatory credential/file/link filters are defense in depth, not a complete
secret scanner. Previously returned data cannot be revoked by changing ignore rules.

Tunnel runtime keys remain in private mode-600 files, separate from database Keychain
items. Do not distribute user .env.local, .code-context, private runtime settings,
connection manifests or selected source. Public packaging contains fixed runtime
resources and applicable third-party notices, with no maintainer connection data.
Checksums and ad-hoc signatures are integrity checks, not Apple notarization.

The GitHub distribution is independent of the ChatGPT public plugin directory.
Each user configures their own connection and local project permissions.

## Reporting a vulnerability

Do not disclose runtime keys, private source or exploit payloads in public
issues. Use GitHub's private vulnerability reporting when enabled. If that
option is unavailable, open a public issue saying only that you need a private
security contact; do not include the confidential details. Ordinary bugs can
use Issues with reproduction steps and sanitized errors.

No production security audit or universal account-availability guarantee is
claimed for this early release.
