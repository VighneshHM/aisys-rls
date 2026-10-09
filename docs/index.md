# Documentation index

Every document, against the SOP deliverable it satisfies (SOP section 7). Start with `walkthrough.md` for the whole system in
one read, or `demo-script.md` for a guided tour.

| Deliverable | Documents |
|---|---|
| **D0 Initiation** | `assumptions.md` (assumptions and clarification log), `risks.md` (risks, dependencies, excluded work), `project-management.md` (plan and management record), `decisions.md` (decision log), `requirements.csv` (the first traceability matrix) |
| **D1 Requirements package** | `assumptions.md`, `use-cases.md`, `data-dictionary.md` (generated), `requirements.csv`, `traceability.md` (generated; every requirement against its tests) |
| **D2 Architecture package** | `architecture.md` (context, layers, components, deployment, database, interfaces, events, security, decisions); feature designs: `rfid.md`, `circulation.md`, `sip2.md` (the SIP2 connector, simulator and limits), `inventory.md`, `gate.md`, `reports.md`, `migration.md`, `reminders.md`, `backup-restore.md`, `administration.md` |
| **D3 Working prototype** | the application itself; run it with `seed-demo` and `run` (`README.md`); the guided tour is `demo-script.md` |
| **D4 Source repository** | the repository; `engineering.md` (branch strategy, pipeline, controls), `third-party-licences.md` (licence inventory), `requirements.lock`, `config/config.sample.toml` |
| **D5 Database and migration** | `data-dictionary.md`, `src/aisys/migrations/` (versioned migrations 0001 to 0011), `samples/` (synthetic datasets with manifests), `docs/samples/` (committed example validation and reconciliation reports, made by `scripts/sample_reports.py`), `migration.md` (import utility, validation and reconciliation reports, rollback) |
| **D6 Test package** | `test-plan.md`, `uat.md` (manual scenarios and results, run by the development team), `uat-signoff.md` (blank site acceptance sign-off form for library staff), `performance-results.md`, `security-checks.md`, `defect-log.md`, `tests/` (automated tests) |
| **D7 Deployment package** | `deployment.md` (offline installer, environment checklist, configuration guide, install, update and rollback), `backup-restore.md`, the signed package built by `scripts/release.py` |
| **D8 Operational documentation** | `administration.md` (the single administrator's guide: routine, staff, audit, Settings page, backups, updates, health), `site-addressing.md` (example site addressing plan), `user-guide.md`, `api-reference.md` (generated), `troubleshooting.md` (runbook), `training-plan.md` |
| **D9 Demonstration** | `demo-script.md` (live demonstration and presentation outline), `presentation.html` (the 12-slide deck, open in a browser), `remaining-work.md` (known limitations and the production-completion backlog) |

Generated documents (`data-dictionary.md`, `api-reference.md`, `traceability.md`) are rewritten by `python scripts/gen_docs.py`
and `python scripts/trace.py`; a test fails if they are stale.

Other: `walkthrough.md` (the final technical walkthrough), `requirements.csv` (the requirement list and status).
