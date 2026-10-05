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

Node.js and sharp are development-only tools for rendering SVG icons. They are
not required on end-user Macs and are not bundled in the application.

The official tunnel source and releases are at
[openai/tunnel-client](https://github.com/openai/tunnel-client). Releases pin a
verified official client; maintainers must re-check its checksums and notices
when upgrading. CoLink is an independent project, not an OpenAI or Apple product.
