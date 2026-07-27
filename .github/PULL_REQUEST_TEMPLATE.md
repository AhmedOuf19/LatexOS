<!--
  Thanks for contributing! Filling this in makes review much faster.
  See CONTRIBUTING.md for the setup and the security invariants.
-->

## What does this change?

<!-- One or two sentences. What problem does it solve? -->

Fixes #

## Why this approach?

<!-- Briefly: why this way rather than an alternative. -->

## How was it tested?

<!-- Which test did you add or update? How did you verify it by hand? -->

- [ ] I added or updated a test that fails without this change
- [ ] `pytest` passes locally
- [ ] I ran the app and checked the change works in the browser

## Security checklist

<!-- See the "Security invariants" table in CONTRIBUTING.md. -->

- [ ] This change does **not** enable shell-escape (`\write18`) by default
- [ ] This change does **not** widen file access outside the project workspace
- [ ] This change does **not** relax a path-traversal, ZIP or upload-size guard
- [ ] This change does **not** loosen the origin/token check or the loopback bind
- [ ] I did not modify `scripts/`, pinned versions, or CI without saying so below

<!--
  If you DID change any of the above on purpose, that's allowed - just explain
  here why it is still safe. A silent change to a security default won't merge.
-->

## Anything else reviewers should know?
