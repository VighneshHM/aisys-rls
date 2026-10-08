# Remaining production work (NFR09)

The ten acceptance scenarios (AC01 to AC10) and deliverables D0 to D10 are complete and tested. These items are **deliberately
outside the exercise** (SOP section 3 and assumptions A-02, A-11) or need a production environment. Nothing here blocks a scenario.

| Area | Requirement | State | What is missing |
|---|---|---|---|
| Core ILMS | FR01 | Scoped out | Acquisition, serials, OPAC, labels. The product is an RFID add-on that talks to an ILMS through the connector, not an ILMS replacement (A-02). |
| Search | FR02 | Basic search done | Catalogue search is SQL `LIKE`. SQLite FTS5 full-text indexing, real-time indexing, net cataloguing and a virtual bookshelf view are future work. |
| Notifications | FR09 | Queue and mock adapters done | Patron notices (overdue, holds) are not generated. Delivery is a command, button or API call; scheduling it is documented in `docs/deployment.md`, not built as a daemon. |
| Real integrations | FR03 | Interfaces and mocks | A real SIP2 or NCIP wire adapter and a real reader, gate, handheld, camera, e-mail and SMS driver, each a class behind an existing interface. No certification is claimed. |
| Security | NFR02 | Partial | TLS termination, service wrappers and scheduled jobs are documented examples (`docs/deployment.md`), not tested here. No password reset or custom roles. |
| Compatibility | NFR03 | Partial | Developed on Windows 11; CI on Windows (Python 3.12 and 3.13). Linux and a real Windows Server 2022 deployment are unverified. |
| Testing | NFR05 | Partial | Functional, integration, migration, security and stress tests exist; formal UAT scripts and a multi-user soak test do not. |
| Data | all | SQLite only | A PostgreSQL option and its backup hooks. |
| Operations | NFR07 | Done | Automatic backup retention and an off-machine backup copy are not built. |

The same list with suggested next steps is in `docs/walkthrough.md`, section 12.
