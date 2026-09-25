# AGENTS.md

Guidance for AI agents working in this repository.

## Running the tests (required before every commit)

This integration is a Home Assistant custom component; the `homeassistant.*`
package is not installed here. The unit tests stub the HA surface the
integration imports (see `conftest.py`) and run in a local virtualenv.

Always create/use the project virtualenv and run the full suite:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest --cov=custom_components/water_leak_meter
```

- Do not skip the suite "because the change is trivial" — run it regardless.
- Fix any failure the change causes before committing.
- Before committing, always ensure test coverage stays at 100%: rerun the
  suite with `--cov` and confirm the report shows no missing statements or
  branches. Do not commit changes that drop coverage below 100%.
- When making changes, always update and/or add unit tests as appropriate so
  the integration's behavior stays covered; a change to production code
  without a matching test change is not ready to commit.
- Do not commit `.venv/`, `.pytest_cache/`, `.coverage`, or `htmlcov/` (all
  gitignored).

## Adversarial code review (required before every commit)

Before committing, a fresh agent (one that did not write the change) must
review the diff adversarially — looking for bugs, edge cases, misuse of the
stubbed HA surface, and untested behavior.

- Delegate the review to a reviewer subagent with the final diff in hand.
- If the review fails, fix the findings and run the review again.
- Loop until the review passes 100%, or every finding has a reasonable,
  evidence-backed rebuttal (with the `file:line` evidence for each).

## Rules of thumb

- Small, single-purpose commits. Bump `manifest.json` and tag a release
  separately from feature work.
- Keep tests green: adding behavior means adding/updating tests. Any change
  to production code ships with its tests in the same commit.
- Don't use emojis in code — except in user-facing notification text, which is
  emoji-prefixed by design (one per alert kind) so messages are scannable.

## Facts for agents

Terminal prints full URLs, plain text — never a bare diff number and never
markdown-wrapped links. Phabricator/task reference short forms auto-link
everywhere else; this repo is on GitHub, so plain URLs in prose.