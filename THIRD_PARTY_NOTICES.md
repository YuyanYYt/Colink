# Third-party software

CoLink source code and its original SVG assets are licensed under MIT. This
does not replace the licenses of dependencies or bundled runtime components.

The self-contained macOS build includes:

- CPython / python-build-standalone: Python Software Foundation license and
  applicable third-party notices, preserved inside `Contents/Resources/python`.
- Runtime Python dependencies locked by `uv.lock`: their `.dist-info` metadata
  and license directories are preserved inside `Contents/Resources/vendor`.
- Tree-sitter Python bindings 0.26.0 and tree-sitter-java 0.23.5: MIT, used only
  for static Java parsing. Their bundled metadata/license files are preserved
  with the other locked runtime dependencies. Python extraction uses the
  standard-library AST parser; no JDK, project compiler or language server runs.
- OpenAI `tunnel-client` 0.0.15: Apache-2.0 and its dependency notices. The
  official distribution's `LICENSE`, `NOTICE`, license inventory and SPDX
  manifest are preserved inside `Contents/Resources/tunnel-client`.
- The official tunnel distribution also contains its cloudflared components
  and corresponding license notices. These are not CoLink's own software.
- Anthropic Sandbox Runtime 0.0.78 (Apache-2.0), with the production dependency
  closure fixed in `src/code_context/resources/sandbox/package-lock.json`:
  socks5-server, commander, node-forge (BSD-3-Clause option), and zod. Original
  package licenses and the JVM proxy adapter are preserved in the application
  sandbox resources. CoLink uses the library and applies its own narrower native
  file, communication and loopback-port policies.
- OpenAI Codex at commit `d27764b82f7118f674371e6d6e76271d9d606edb`
  (Apache-2.0): the read-only descriptor fcntl protections in
  `codex-rs/sandboxing/src/seatbelt.rs` are adapted in CoLink's sandbox helper.
  Modifications by CoLink, 2026, narrow protected paths and registered endpoints.
  Original LICENSE and NOTICE are preserved in `resources/sandbox/licenses/codex`.
  The process/output coordinator is an independent Python implementation inspired
  by Codex's process groups, cancellation and bounded head/tail output mechanisms.

sharp is a development-only tool for rendering SVG icons. Node.js is also used
by the optional terminal sandbox helper; it is discovered from approved existing
installations. Node.js, Python project tools, JDK and database servers are not
bundled as additional development toolchains. Read-only code access does not
require Node.js.

The official tunnel source and releases are at
[openai/tunnel-client](https://github.com/openai/tunnel-client). Releases pin a
verified official client; maintainers must re-check its checksums and notices
when upgrading. CoLink is an independent project, not an OpenAI or Apple product.
