# Documentation index

Every document, grouped by area. Start with `walkthrough.md` for the whole system in
one read, or `demo-script.md` for a guided tour.

| Area | Documents |
|---|---|
| **Planning** | `assumptions.md` (assumptions and clarification log), `risks.md` (risks, dependencies, excluded work), `project-management.md` (plan and management record), `decisions.md` (decision log), `requirements.csv` (the first traceability matrix) |
| **Requirements** | `assumptions.md`, `use-cases.md`, `data-dictionary.md` (generated), `requirements.csv`, `traceability.md` (generated; every requirement against its tests) |
| **Architecture and design** | `architecture.md` (context, layers, components, deployment, database, interfaces, events, security, decisions); feature designs: `rfid.md`, `circulation.md`, `sip2.md` (the SIP2 connector, simulator and limits), `inventory.md`, `gate.md`, `reports.md`, `migration.md`, `reminders.md`, `backup-restore.md`, `administration.md` |
| **Running it** | the application itself; run it with `seed-demo` and `run` (`README.md`); what the demo library holds is `demo-data.md`; the hands-on tour is `feature-tour.md`, the guided demo `demo-script.md`; `Dockerfile` runs it in one command |
| **Repository and engineering** | the repository; `engineering.md` (branch strategy, pipeline, controls), `project-management.md` (pull requests, stage timeline D0 to D10), `third-party-licences.md` (licence inventory of every pinned package, generated), `sbom.cdx.json` (CycloneDX SBOM, generated), `requirements.lock` and `requirements-dev.lock` (hashed pins), `config/config.sample.toml` |
| **Database and migration** | `data-dictionary.md`, `src/shelfsense/migrations/` (versioned migrations 0001 to 0012), `samples/` (synthetic datasets with manifests), `docs/samples/` (committed example validation and reconciliation reports, made by `scripts/sample_reports.py`), `migration.md` (import utility, validation and reconciliation reports, rollback) |
| **Testing** | `test-plan.md`, `uat.md` (manual scenarios and results, run by the development team), `uat-signoff.md` (blank site acceptance sign-off form for library staff), `uat-signoff-development.md` (the development team's completed copy of that form, not site acceptance), `performance-results.md`, `security-checks.md`, `defect-log.md`, `tests/` (automated tests) |
| **Deployment** | `deployment.md` (offline installer, environment checklist, configuration guide, install, update and rollback), `backup-restore.md`, the signed package built by `scripts/release.py` |
| **Operations** | `administration.md` (the single administrator's guide: routine, staff, audit, Settings page, backups, updates, health), `site-addressing.md` (example site addressing plan), `user-guide.md`, `api-reference.md` (generated), `troubleshooting.md` (runbook), `training-plan.md` |
| **Demo and roadmap** | `demo-script.md` (a guided live demo), `remaining-work.md` (known limitations and the production-completion backlog) |

Generated documents (`data-dictionary.md`, `api-reference.md`, `traceability.md`, `third-party-licences.md`, `sbom.cdx.json`,
`samples/`) are rewritten by `python scripts/gen_docs.py`, `python scripts/trace.py`, `python scripts/sbom_lock.py` and
`python scripts/sample_reports.py`; a test fails if any is stale.

Other: `walkthrough.md` (the final technical walkthrough), `requirements.csv` (the requirement list and status).
