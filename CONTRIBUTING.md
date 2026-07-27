# Contributing to LaTeX Studio

Thanks for wanting to help! This guide covers how to get set up, what the
project expects from a change, and — most importantly — the **safety rules**
that every contribution has to respect.

By contributing you agree that your work is released under the
[MIT License](LICENSE), the same terms as the project.

---

## Getting set up

You need Windows (the launcher scripts are Windows-first) plus Git.

```bash
git clone https://github.com/AhmedOuf19/LatexOS.git
cd LatexOS
install.bat            # one-time: installs Python, TinyTeX, editor, deps into the folder
```

Then run the app and the tests:

```bash
"Launch LaTeX Studio.bat"                    # start the app
.venv\Scripts\python.exe -m pytest           # run the test suite
.venv\Scripts\python.exe run.py --check      # diagnose the install
```

The LaTeX-dependent tests skip automatically if no TeX distribution is present,
so `pytest` works even on a partial setup.

Everything installs *into the project folder* — nothing touches your system.
To start over, delete the generated folders (`.venv/`, `python/`, `tinytex/`,
`bin/`, `frontend/vendor/monaco/`) and re-run `install.bat`.

---

## 🔒 Security invariants — please read before changing the backend

This app compiles LaTeX, which is a **full programming language**, and it serves
files over HTTP. A few behaviours are deliberate and must not be weakened. Each
one is locked by a test; if your change makes one of these tests fail, the fix
is to change your code, not the test.

| Invariant | Where | Test that locks it |
|---|---|---|
| Shell-escape (`\write18`) is **off** unless explicitly opted in per compile | `backend/compiler.py` | `test_shell_escape_disabled_by_default`, `test_write18_blocked_by_default`, `test_shell_escape_per_compile_override` |
| LaTeX cannot read/write outside the project workspace (`openin_any`/`openout_any` = `p`) | `backend/compiler.py` (`_build_env`) | `test_build_env_confines_file_access` |
| Static and file routes are path-confined (no `..`, no `%2e%2e`, no `C:`/ADS) | `backend/main.py`, `backend/file_manager.py` | `test_static_route_traversal_blocked`, `test_file_api_encoded_traversal_blocked`, `test_ads_colon_rejected` |
| ZIPs cannot escape, bomb, or drop extensionless files (e.g. `.latexmkrc`) | `backend/file_manager.py` | `test_zip_slip_blocked`, `test_zip_bomb_capped`, `test_zip_extensionless_rejected` |
| Uploads and edits are size-capped | `backend/file_manager.py`, `backend/main.py` | `test_upload_size_cap`, `test_put_size_cap` |
| The API refuses cross-origin calls and checks the instance token | `backend/main.py` | `test_cross_origin_api_refused`, `test_wrong_token_refused` |
| The server binds `127.0.0.1` by default | `backend/config.py` | — (please keep it that way) |

**If you genuinely need to change one of these**, that's fine — but say so
explicitly in the pull request and explain why it is still safe. A silent
change to a security default will not be merged.

Also please **do not** change these without discussing it first:

- `scripts/*.ps1` — these download and execute installers; changes here are
  reviewed carefully (URL changes, checksum handling, pinned versions).
- Pinned versions in `scripts/install.ps1` and `requirements.txt`.

---

## Making a change

1. **Open an issue first** for anything non-trivial, so we can agree on the
   approach before you spend time on it. Small bug fixes can go straight to a PR.
2. Create a branch: `git checkout -b fix/short-description`.
3. Make your change, and **add or update a test** that would fail without it.
4. Run the full suite: `.venv\Scripts\python.exe -m pytest` — it must be green.
5. Update `CHANGELOG.md` under an *Unreleased* heading, and the `README.md` if
   you changed behaviour or configuration.
6. Open a pull request and fill in the template.

### Style

Match the surrounding code — that is the rule that overrides everything else.

- **Python:** standard library style, 4-space indent, type hints on new
  functions, and a docstring explaining *why* rather than restating the code.
- **JavaScript:** plain ES2020, no build step, no framework, no new runtime
  dependencies. The frontend must keep working offline.
- **Comments:** this project is read by beginners. Explain non-obvious
  decisions, especially anything security- or LaTeX-toolchain-related.
- Keep the committed repository small — never commit anything from the
  generated folders listed in `.gitignore`.

### Commits

Write a short imperative subject line (`Fix stale PDF reported as success`) and
use the body to explain the reasoning. Reference issues with `Fixes #12`.

---

## What makes a pull request easy to merge

- It does **one** thing.
- It has a test proving the change works.
- The full suite passes and CI is green.
- It explains *why* in the description, not just *what*.
- It does not add dependencies without a good reason.

## Reporting bugs

Use the issue templates. For a compile problem, please include the **log panel
output** and, if you can, a minimal `.tex` that reproduces it — that is usually
enough to find the cause immediately.

## Reporting a security problem

**Do not open a public issue.** Follow [SECURITY.md](SECURITY.md) and report it
privately.

---

## Questions

Open a [Discussion](https://github.com/AhmedOuf19/LatexOS/discussions) or an
issue. Beginner questions are welcome — if something in this guide was unclear,
that is a bug in the guide, and pointing it out is itself a useful contribution.
