# Contributing

Thank you for helping. This page says how a change gets from an idea to a merge. How we write code, tests, comments and commits is in [AGENTS.md](AGENTS.md); please read it first, because reviews follow it.

## 1. Open an issue first

Every change starts as an issue, even a small one. Use the issue form, and fill in every part:

- what happens, and how to make it happen;
- what you expected instead;
- why you think it happens;
- the fix you suggest, if you have one;
- what the fix would touch, in this project and beyond it.

Writing these down is how we make sure the problem is understood before anyone writes code. If you aren't sure about the cause or the fix yet, say so. That is fine, and the issue is the place to work it out together.

## 2. Make the change

1. Branch from `main`.
2. Make the change in small commits, each one passing the tests, in the form `type(scope): lowercase summary`. The commit body says why.
3. Add tests:
   - a new feature gets tests at its interface;
   - a bug fix gets a regression test, marked `# Regression:`;
   - keep tests orthogonal, and explain any overlap in a comment.
4. Run the checks:

   ```bash
   uv run pytest
   uvx ruff check
   uvx ruff format --check
   uv run pyright
   uvx marimo check --strict notebooks/explorer.py   # if you changed the notebook
   ```

## 3. Open a pull request

- Link the issue in the description with `Closes #123`. A check fails any pull request that doesn't link an open issue.
- Fill in the pull request template: what changed and why, what it touches, and which tests you ran and added.
- Write it in plain language, without em dashes. Someone who hasn't read the code should follow it.

## Setting up

You need [uv](https://docs.astral.sh/uv/). Then:

```bash
uv sync            # the package, with everything the tests use
uv run pytest      # every test
uvx marimo edit --sandbox notebooks/explorer.py   # the explorer
```
