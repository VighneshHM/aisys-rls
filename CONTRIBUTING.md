# Contributing to ShelfSense

Contributions are welcome: bug reports, ideas, documentation fixes and code. ShelfSense is source-available (see
[`LICENSE`](LICENSE)): you may fork it to prepare a contribution, and by opening a pull request you agree that your
contribution can be used as part of the project. Any other use of the code needs the author's permission first.

## Reporting a bug or asking a question

Open an issue with what you did, what you expected and what happened. Use only synthetic data: never paste real patron
names, card numbers or contact details.

## Making a change

1. Fork the repository and create a branch from `main` (`fix/...`, `feature/...` or `docs/...`).
2. Set up the project (README, *Quick start*, from source) and load the demo library with `shelfsense seed-demo`.
3. Make the change with tests. Tests carry `@pytest.mark.req("FR..")` with the requirement they prove; a new behaviour
   gets a new test, a bug fix gets a test that failed before the fix.
4. Run the same checks as CI and make sure all of them pass:

   ```bash
   python -m ruff format .
   python -m ruff check .
   python -m mypy
   python -m pytest -n auto
   ```

5. If you changed a dependency, the database or the API, regenerate the generated documents: `python scripts/sbom_lock.py`,
   `python scripts/gen_docs.py` and `python scripts/trace.py` (a test fails when one is stale).
6. Open a pull request that says what changed and why, and which requirement or issue it relates to.

## Style

- Code: ruff's formatter, lines up to 120 characters, type hints that mypy accepts.
- Messages shown to library staff say what happened and what to do next, in plain words.
- Every device or outside service goes behind an interface with a mock, never called directly.
- A dependency is added only through the hashed lock files, with its licence checked (`docs/third-party-licences.md`).
