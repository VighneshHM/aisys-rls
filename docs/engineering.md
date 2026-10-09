# Engineering controls (D4, SOP section 10)

How the work is controlled, so a reviewer can see the practice and not only the code.

## Version control and branch strategy

* **`main` is always releasable.** Nothing is committed to it directly; every change arrives through a pull request that must pass
  the pipeline below.
* **One feature branch per acceptance scenario or requirement**, named for the work: `feature/ac06-gate`, `feature/ac10-restore`,
  `feature/ac09-offline-lifecycle`, `feature/audit-admin-pages`, `feature/patron-reminders`, `overall-fixes`, `docs/final-walkthrough`.
  A branch starts from the current `main`, so each reviewed merge is small enough to read.
* **Meaningful commits**: short imperative messages that say what the change adds or fixes ("addition of backup and restore",
  "fix cryptography vulnerabilities and raise CI timeout"). A merge commit records the pull request number.
* **Change control**: when behaviour changes, the requirement (`docs/requirements.csv`), design document, code, tests and
  `docs/traceability.md` change in the same pull request. Tests carry `@pytest.mark.req("FR..", "AC..")` so the matrix is generated, not
  remembered; `tests/test_docs.py` fails if a generated document is stale or a requirement marked done has no test.
* **Release tag.** The evaluation release is tagged on `main` once it is final, with the package version (the version in
  `pyproject.toml` and `src/aisys/__init__.py`, which the update tools report): `git tag -a v0.1.0 -m "Evaluation release"`, then
  `git push origin v0.1.0`. The tag and the package version must agree, so a later release bumps both together.

## Build pipeline (GitHub Actions, on every pull request and push to `main`)

| Gate | Tool | Fails the build when |
|---|---|---|
| Formatting | `ruff format --check` | any file is not formatted |
| Type checking | `mypy` (configured in `pyproject.toml`, covers `src/`) | any type error |
| Lint and static analysis | `ruff check` with `E F I B UP S` (the `S` rules are the Bandit security checks) | any finding (tests may use `assert`) |
| Automated tests | `pytest -q -n auto` (parallel workers; each test copies a database that was migrated once, which cut a 25-30 minute CI run) on Windows (`windows-latest` with Python 3.12 and 3.13, and `windows-2022` with 3.12, the Server 2022 image) | any test fails |
| Real offline lifecycle | `scripts/lifecycle_demo.py` on `windows-latest`: a real install from signed package, refusals, signed update, rollback with the data proven, with every connection off the machine refused and logged on the target-machine steps | any step fails, or any step tried to reach the network |
| Dependency scan | `pip-audit` on `requirements.lock` and `requirements-dev.lock` | any known vulnerability in a pinned dependency (it caught and forced the upgrade of `cryptography`) |
| Dependency updates | Dependabot, weekly, for pip and for the actions | opens pull requests; they go through the same gates |

Every job installs from `requirements.lock` and `requirements-dev.lock` with `--require-hashes`, and builds the project with
the pinned `setuptools` (`--no-build-isolation`), so CI runs the exact versions that were audited.

Concurrency is set so a newer push cancels the older run of the same branch; the test job has a 30-minute limit.

## Dependencies and licences

* Two lock files, both with sha256 hashes. `requirements.lock` holds the runtime packages (the direct ones are also pinned
  exactly in `pyproject.toml`), every transitive one included; these are the wheels shipped to a site.
  `requirements-dev.lock` holds the development tools (`pytest`, `pytest-xdist`, `httpx`, `ruff`, `mypy`, `cryptography`
  and their dependencies) and the build backend `setuptools`; they are never shipped. `scripts/sbom_lock.py --check` fails
  if a package is in the wrong file.
* `docs/third-party-licences.md` is the licence inventory of all 43 pinned packages with their scope (runtime, development,
  build), and `docs/sbom.cdx.json` the same as a CycloneDX SBOM with licences and hashes; both are generated and checked
  by `tests/test_docs.py`. No runtime package is copyleft; two development tools are MPL-2.0 and are not shipped.
  Redistribution and offline-use obligations: `docs/deployment.md`. Every update package also carries a CycloneDX
  `SBOM.json` and `LICENSES.md` generated from the wheels it actually ships, with copyleft or unknown licences flagged.
* Offline installation needs nothing from the internet: the package contains every wheel (`docs/deployment.md`).

## Configuration and secrets

* Environment-specific values live in a TOML file outside the code (`config/config.toml`, git-ignored) and in environment
  variables; `config/config.sample.toml` is the documented sample with no secrets. Settings are validated at start (bad device
  or driver names, a bad port, half a TLS pair, a short production key all stop the start with a plain message).
* Nothing secret is committed: no passwords, tokens, certificates or private keys. The only credentials in the repository are the
  **synthetic demo** card numbers and the demo administrator password, documented as such in `README.md` and `src/aisys/cli.py`.
  Signing keys for update packages are generated outside the repository (`release-keys/` is git-ignored).
* `AISYS_SECRET_KEY` (at least 32 characters) is required when `environment = production`; the installer generates one.

## Data protection in practice

* All data is synthetic (the sample spreadsheets are generated by `python -m aisys.devtools.sample_data`).
* Logs are structured JSON with masking of passwords, tokens and long digit runs; the audit log stores card numbers masked and
  never stores patron contact details.
* Least privilege: four roles, each endpoint names the permission it needs, and the matrix is generated from the code
  (`docs/data-dictionary.md`).
* Every administrative and circulation action is recorded in the append-only audit log or circulation history.

## Defects

Defects are recorded in `docs/defect-log.md` with severity, how to reproduce, owner, status, the branch or version that fixed
them, and the test or check that retested them.

## Documentation map

`docs/index.md` lists every document against the SOP deliverable (D1 to D9) it satisfies.
