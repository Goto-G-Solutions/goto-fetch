Closes #

## What changed, and why

<!-- In plain language: what this does, and the reason. Someone who hasn't read the code should follow it. -->

## How

<!-- The approach, and anything a reviewer should look at closely. -->

## What it touches

<!-- In this project: the modules and behaviour it changes. Beyond it: anything that depends on that behaviour. Say "nothing beyond the issue" if that's so. -->

## Tests

<!-- Which checks you ran, and what you added. -->

- [ ] `uv run pytest` passes.
- [ ] `uvx ruff check` and `uvx ruff format --check` pass.
- [ ] `uv run pyright` passes.
- [ ] `uvx marimo check --strict notebooks/explorer.py` passes, if I changed the notebook.
- [ ] New behaviour has tests at its interface.
- [ ] A bug fix has a regression test, marked `# Regression:`.
- [ ] The tests are orthogonal, and any overlap is explained in a comment.

## Guides

<!-- Which guides in src/goto_fetch/docs/ this changes, or "none: nothing a user sees changed". -->

- [ ] Every guide this change makes untrue is updated, with examples that run (`tests/test_docs.py`).
- [ ] A new piece or guide is in the index, `llms.txt` and, if agents need it, `SKILL.md`.
- [ ] `README.md` and `CHANGELOG.md` ("Unreleased") say what changed for users.

## Before you send it

- [ ] The issue above was opened first, and this pull request does what it describes.
- [ ] Commits read `type(scope): lowercase summary`, with the reason in the body.
- [ ] Plain language throughout, and no em dashes.
