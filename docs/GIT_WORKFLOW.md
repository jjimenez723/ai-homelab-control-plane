# Git Workflow

## Default branch

`main` is the canonical integration branch and should remain deployable/reviewable.

## Change flow

1. Create a short-lived branch from current `main` using a descriptive prefix such as `feat/`, `fix/`, `docs/`, or `chore/`.
2. Keep one logical concern per branch whenever practical.
3. Open a pull request before merging substantive changes.
4. Require relevant automated checks to pass before merge.
5. Prefer squash merge for small feature branches so `main` keeps one coherent commit per reviewed change set.
6. Use a Conventional Commits-style squash title, for example `feat: add social-video learning pipeline` or `fix: correct queue platform handling`.
7. Delete stale merged branches.

## History rules

- Never force-push `main`.
- Never rewrite published `main` history.
- Do not commit generated media, model weights, secrets, credentials, browser cookies, or local caches.
- Avoid catch-all commits such as `updates` or `misc fixes`.
- Commit messages should state the intent of the change, not the tool that made it.
- Use signed commits when practical for human-authored release or security-sensitive changes; unsigned commits are not by themselves a blocker for this private planning repository.
- Tag meaningful stable milestones rather than every commit.

## Pull-request standard

A substantive PR should state:

- purpose and scope;
- security or data-loss implications;
- tests/validation performed;
- known limitations;
- follow-up work explicitly left out of scope.

## Branch protection target

As the repository becomes operational, configure `main` to require pull requests and the `Python tests` CI check. Do not enable rules that prevent emergency recovery until the workflow has been proven on this small repository.
