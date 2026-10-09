# Remaining production work and known limitations (NFR09, D9)

The ten acceptance scenarios (AC01 to AC10) and every deliverable (D1 to D9) are complete and tested. This is what remains before
a live site, and what the prototype deliberately does not do. Nothing here blocks a scenario.

## Not built, by decision

| Area | Requirement | What is missing |
|---|---|---|
| NCIP 2.0 | FR03 | An NCIP 2.0 client. Only SIP2 is built (`docs/sip2.md`); an NCIP adapter would be another class registered under a new driver name |
| Real devices and services | FR03, FR07, FR09 | Drivers for a real reader, handheld, gate, camera, card printer, e-mail, SMS and print service, and the outside catalogue. Each is one class behind an existing interface plus a config line |
| Full ILMS depth | FR01 | Budgets, invoices and claim letters in acquisitions; serial binding and prediction patterns; holds and reservations; an inter-library loan. The add-on is not an ILMS replacement (A-02) |
| Patron notices | FR09 | Overdue repeat notices and hold-ready notices (the day-before and due-day reminders are built); a combined digest; patron language and quiet hours |
| Search extras | FR02 | Synonyms, spelling correction, faceted filters. A real network cataloguing protocol (Z39.50 or SRU) |

## Needs a production environment

| Area | Requirement | What is missing |
|---|---|---|
| A real ILMS over SIP2 | FR03 | The SIP2 client was tested only against the simulator written from the specification. It needs a run against the site's real ILMS (account, institution id, location code, checksum setting), small keyword-mapping changes if its wording differs, and an encrypted path to it (VPN, stunnel or TLS proxy) because SIP2 has no encryption. Only a subset of SIP2 is implemented (`docs/sip2.md`) |
| Acceptance by real users | NFR05 | A site acceptance test with library staff and real devices (the UAT in `docs/uat.md` was run by the development team; the blank sign-off form for library staff is `docs/uat-signoff.md`) |
| Load and endurance | NFR05 | A soak test of hours or days on a server-class machine; the measured figures are one process on one machine (`docs/performance-results.md`) |
| TLS in front | NFR02 | The application's own HTTPS is tested; the proxy and service-wrapper examples in `docs/deployment.md` are documented, not run |
| Platforms | NFR03 | A real Windows Server 2022 installation (CI runs on the Server 2022 image); Linux unverified |
| Hardening | NFR02 | A content-security-policy (needs the inline scripts removed), a penetration test, multi-factor sign-in, encryption of the data file at rest |
| Real credentials | AC07 | A smart-card challenge-response driver instead of the mock (the interface already carries the `proof` field) |
| Database | all | A PostgreSQL option and its backup hooks for sites that outgrow one writer |
| Operations | NFR07 | Automatic backup retention, an off-machine backup copy, monitoring and alerting on the health endpoints, a service wrapper installed and tested |

## Limits worth knowing

* A restore replaces everything, including the audit log up to the backup's moment (the restore itself is logged afterwards).
* An update needs the application stopped; the install folder on Windows must be short (about 150 characters).
* A first install trusts the key it is given; there is no certificate authority or key revocation; the licence has no hardware binding.
* Packages contain wheels for the platform and Python they were built on.
* Reminders are created when the loop runs: if the application is down for a whole day, that day's reminder is not sent later.
* The mock devices and providers prove the interfaces, not any vendor's behaviour.

The same list with suggested next steps is in `docs/walkthrough.md`, section 12.
