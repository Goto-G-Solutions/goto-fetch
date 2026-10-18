# Contributing

Thank you for helping. This page says how a change gets from an idea to a merge. How we write code, tests, comments and commits is in [AGENTS.md](AGENTS.md); please read it first, because reviews follow it.

## 1. Open an issue first

Every change starts as an issue, even a small one. Use the issue form, and fill in every part:

- what happens, and how to make it happen;
- what you expected instead;
- why you think it happens;
- the fix you suggest, if you have one;
- what the fix would touch, in this project and beyond it, guides included.

Writing these down is how we make sure the problem is understood before anyone writes code. If you aren't sure about the cause or the fix yet, say so. That is fine, and the issue is the place to work it out together.

## 2. Make the change

1. Branch from `main`.
2. Make the change in small commits, each one passing the tests, in the form `type(scope): lowercase summary`. The commit body says why.
3. Add tests:
   - a new feature gets tests at its interface;
   - a bug fix gets a regression test, marked `# Regression:`, that fails without the fix;
   - a defence learned from another library's bug is marked `# Guard (source):`;
   - keep tests orthogonal, and explain any overlap in a comment;
   - a rule that should hold for any input belongs in `tests/test_properties.py`, as a property test.
4. Update the guides. The guides in `src/goto_fetch/docs/` ship with the package, and people and coding agents rely on them, so a valid change keeps them true:
   - a change to what a piece does, or to its public interface, updates that piece's guide, and `build-your-own.md` if it changes an interface;
   - a new piece or guide is added to the index (`src/goto_fetch/docs/README.md`), to `llms.txt`, and to `SKILL.md` if an agent needs to know of it;
   - every example in a guide runs as part of `uv run pytest` (`tests/test_docs.py`), so a stale example fails the tests;
   - update `README.md` and `CHANGELOG.md` the same way, under "Unreleased".
5. Run the checks:

   ```bash
   uv run pytest
   uvx ruff check
   uvx ruff format --check
   uv run pyright
   uvx marimo check --strict notebooks/explorer.py   # if you changed the notebook
   uvx zizmor .github/workflows                      # if you changed a workflow
   ```

## 3. Open a pull request

- Link the issue in the description with `Closes #123`. A check fails any pull request that doesn't link an open issue.
- Fill in the pull request template: what changed and why, what it touches, which tests you ran and added, and which guides you updated.
- Write it in plain language, without em dashes. Someone who hasn't read the code should follow it.

## Releasing

Releases go to PyPI from GitHub Actions (`.github/workflows/release.yml`), with trusted publishing, so no PyPI token is stored anywhere.

**Each release:**

1. Set the new version: `uv version --bump minor` (or `patch`, or `major`).
2. Add the release to `CHANGELOG.md`: what changed for people using the package.
3. Commit both as `chore(release): 0.2.0`, through a pull request like any other change.
4. Tag the merged commit and push the tag: `git tag v0.2.0 && git push origin v0.2.0`.

The workflow checks the tag matches the version, runs the tests, builds, and publishes.

**Once, before the first release:**

1. On PyPI, under your account's Publishing settings, add a pending trusted publisher: project `goto-fetch`, owner `Goto-G-Solutions`, repository `goto-fetch`, workflow `release.yml`, environment `pypi`.
2. On GitHub, in the repository's Settings, create an environment named `pypi`. Adding yourself as a required reviewer there makes every release wait for your approval.

## Setting up

You need [uv](https://docs.astral.sh/uv/). Then:

```bash
uv sync            # the package, with everything the tests use
uv run pytest      # every test
uvx marimo edit --sandbox notebooks/explorer.py   # the explorer
```
