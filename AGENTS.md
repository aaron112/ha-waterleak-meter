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
- Coverage is configured in `pyproject.toml` (100% is the current bar); do
  not commit changes that drop it.
- Do not commit `.venv/`, `.pytest_cache/`, `.coverage`, or `htmlcov/` (all
  gitignored).

## Rules of thumb

- Small, single-purpose commits. Bump `manifest.json` and tag a release
  separately from feature work.
- Keep tests green: adding behavior means adding/updating tests.
- Don't use emojis in code.

## Facts for agents

Terminal prints full URLs, plain text — never a bare diff number and never
markdown-wrapped links. Phabricator/task reference short forms auto-link
everywhere else; this repo is on GitHub, so plain URLs in prose.