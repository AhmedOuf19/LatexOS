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
  Some packages (notably `minted`) need it: tick the **Shell-escape** box next to
  the Compile button to enable it for that compile only, or set
  `LATEX_ALLOW_SHELL_ESCAPE=1` to enable it for every compile. Only do this for
  documents you trust — it lets the document run programs on your computer.
- **File access is confined** to the project workspace (`openin_any=p`,
  `openout_any=p`), so a document cannot read your other files.
- Uploads are size-capped and type-whitelisted; ZIPs are checked for
  path-traversal, decompression bombs and dangerous names.

Even so, treat a `.tex` from a stranger with the same caution as any program.

## How paths are kept inside their directory

Every route that takes a name from the request is confined in two independent
ways, and each is covered by a test in `tests/test_security.py`:

1. **Look up, don't build.** A session directory is found by matching the id
   against the real entries of `uploads/`, so the only paths the code can return
   are directories that already exist there. The request string never becomes
   part of a path expression.
2. **Reject, don't normalise.** Static asset components must match a strict
   allow-list (`[A-Za-z0-9._-]`, with at least one non-dot character), so `..`,
   absolute paths, `C:` drive prefixes and NTFS stream names are refused before
   any path is constructed — not cleaned up afterwards. The resolved result is
   then still required to sit inside the expected root.

### A note on static-analysis warnings

Code scanning may report `py/path-injection` ("Uncontrolled data used in path
expression") on these functions. Those reports are false positives: the analyser
does not model `Path.relative_to()` in a `try/except`, nor a `fullmatch()` UUID
check, as sanitisers — it flags the sanitiser lines themselves and even
read-only `.exists()` calls. Containment is asserted directly by
`tests/test_security.py::TestPathTraversal` on every CI run.

## Reporting a vulnerability

If you find a security issue, please open a **private** report (GitHub Security
Advisory) or contact the maintainer directly rather than filing a public issue.
Include steps to reproduce and the impact. We aim to acknowledge within a few
days.
