# Security

## Reporting a problem

If you find a security problem in goto-fetch, please report it privately, so it can be fixed before it is public: open the repository's **Security** tab and choose **Report a vulnerability**, which reaches the maintainers only. Please don't open a public issue for it.

Say what you found, how to make it happen, and what you think the impact is. You will get a reply within a week, and a fix or a plan within 30 days of confirming it.

## What counts

Anything that lets data or a credential go where it shouldn't: a secret reaching a log, an error message or another host; a run that reports success while losing or skipping records; SQL or path handling that a record's content can abuse; a check that can be made to pass when it should fail. A report that turns out not to be a security problem is still welcome as an issue.

## Which versions get fixes

The latest release on PyPI. A fix ships as a patch release, with the problem described in the changelog once the release is out.

## How releases are protected

Releases are built and published by GitHub Actions from a `v*` tag, through PyPI's trusted publishing, so no PyPI token exists anywhere; each upload carries a PEP 740 attestation, which lets you verify on PyPI that a wheel was built from this repository's workflow. Actions are pinned to commit SHAs, `main` and the release tags are protected, and Dependabot keeps the dependencies current.
