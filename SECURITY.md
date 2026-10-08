# Security and privacy

The public prerelease is 0.5.0b5 build 22. It adds compact modes, direct host terminals,
fixed read-only database commands, and configurable local storage.
Host terminals require the default-off project development grant. They are NOT sandboxed. Actual source, native, webpage and installed
application evidence are recorded separately; no production security audit is claimed.
The legacy mirror protocol and existing databases retain their historical semantics.

Live reads verify original files without keeping a permanent whole-project mirror.
Dedicated source-tool writes, moves and isolated generated-result writeback use WriteCoordinator,
with source hashes, path/identity checks and durable recovery records. Unknown recovery
objects and unresolved partial operations are preserved. Intentional code undo is not
exposed; use independently saved Git history. A task Diff is not a permanent backup or
a multi-file atomic filesystem transaction.

The webpage cannot enable local read/write/execute grants or force recovery through
management tools. Every tool uses an explicit project ID. Once development is enabled,
the host terminal has the current OS user filesystem/network permissions; project routing
and process supervision are not a security boundary against arbitrary host code.
Local authorization and ChatGPT's action approval policy are separate. Original mirror
read tools remain read-only. Development status/output/list operations that renew a
service lease or acknowledge log cleanup are annotated as effects, not read-only.

## Native host terminal (introduced in build 17)

- terminal_start/input require the current exact project development grant. Revoke and
  local-control disconnect stop owned sessions; every queued launch revalidates its grant.
- Initial cwd is a real directory within the registered project. Commands can subsequently
  access any path/service available to the OS user. No Seatbelt, exact-database proxy,
  per-port grant, input export or WriteCoordinator writeback applies to terminal_*.
- Database permissions are the database account's permissions, including DDL/CRUD/admin
  commands. No separate CoLink connection page, bootstrap approval or database grant exists.
- PTY or pipes support continuing input. PTY echo starts disabled; programs may still print
  secrets or enable echo. Avoid printing credentials. CoLink control/tunnel environment
  secrets are not automatically inherited. This is not secret isolation from host code.
- Process ownership supervision, bounded output, idle leases and sampled resource limits
  remain. Stop requests do not undo file/database/network side effects. Unknown restart
  outcomes are not rerun. Only opaque start receipts are persisted; command/stdin are not.
- Connection profiles, credentials and old provisioning recovery records are preserved
  but are not read or modified by this terminal runtime.
- Build 22 passed a full local regression and installed-app process supervision probe.
  These do not establish production security or validate every user's database and web flow.

## Optional isolated execution_plan runner (unchanged scope)

- Only the currently validated Darwin 27 implementation is enabled. Other systems
  keep this isolated execution path unavailable. A private libproc ABI mismatch fails closed;
  there is no fallback to parent PID tracking. Host terminal execution is a separate explicit path.
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
- This optional isolated runner remains noninteractive. Use terminal_* for explicit host PTY/input.

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

Read-only mode exposes terminal_read_targets and terminal_read without HTML resources.
Targets resolve from current bounded project configuration; credentials never appear in
these results. MySQL/PostgreSQL use fixed metadata/base-table queries in read-only
transactions, Redis uses a fixed read-command allowlist, and SQLite opens mode=ro with
query_only and an authorizer denying write/attach/function commands. No arbitrary SQL,
shell, scripts or client flags enter this path. Responses/time/row counts are bounded.
These controls restrict commands issued by CoLink; they do not authenticate a database
server's implementation or guarantee that server-side functions have no external effects.

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
