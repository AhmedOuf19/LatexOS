# Security Policy

## Threat model — read this first

LaTeX Studio is a **single-user, local tool**. It has **no authentication** and
is meant to run on `127.0.0.1` (loopback) on your own machine.

- ✅ **Supported:** running it locally and compiling your own or trusted
  LaTeX projects.
- ❌ **Not supported / do not do this:** exposing it to a network or the
  internet (e.g. binding `--host 0.0.0.0`, or putting it behind a public
  reverse proxy). There is no multi-tenant isolation.

## Compiling untrusted documents

Compiling a `.tex` file runs a real LaTeX engine, which is a powerful
interpreter. Defaults are chosen to be safe:

- **Shell-escape is OFF by default.** `\write18{…}` cannot run OS commands.
  Enable it only for documents you trust, with `LATEX_ALLOW_SHELL_ESCAPE=1`.
- **File access is confined** to the project workspace (`openin_any=p`,
  `openout_any=p`), so a document cannot read your other files.
- Uploads are size-capped and type-whitelisted; ZIPs are checked for
  path-traversal, decompression bombs and dangerous names.

Even so, treat a `.tex` from a stranger with the same caution as any program.

## Reporting a vulnerability

If you find a security issue, please open a **private** report (GitHub Security
Advisory) or contact the maintainer directly rather than filing a public issue.
Include steps to reproduce and the impact. We aim to acknowledge within a few
days.
