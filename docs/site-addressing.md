# Example site addressing plan (NFR08)

> **This is an EXAMPLE, not a design for any real site.** Every address, VLAN number, port and rule below is invented to show the
> shape of the plan. The addresses come from the **10.x private range** (RFC 1918): the example uses `10.99.0.0/16`, split into
> one `/24` per VLAN. A site must replace all of them with its own values (checklist in section 4). Where a single documentation
> address is needed in a command or a file, use `192.0.2.x` (192.0.2.0/24, reserved for documentation by RFC 5737); it never
> routes anywhere.

AISYS runs with every device and external service simulated. No real reader, gate, camera, printer, ILMS or mail relay was
connected during development, so the ports and protocols in the table are the **usual ones for that kind of equipment**, to be
confirmed against the actual product's manual. They are not measured facts about any supplier's device.

## 1. Example plan

| Role | Example address | Port | Protocol | VLAN suggestion | Firewall rule (example) |
|---|---|---|---|---|---|
| AISYS server | 10.99.10.10 | 443 (TLS proxy in front) or 8000 (application on loopback) | HTTPS | 10 Servers | Allow staff desks and handhelds to 10.99.10.10:443; deny everything else inbound; no outbound internet needed |
| Staff desks (browsers and staff card readers) | 10.99.20.0/24 (DHCP) | client side | HTTPS to the server | 20 Staff | Allow to server :443 only |
| Handhelds (inventory) | 10.99.30.0/24 | client side | HTTPS to the server over Wi-Fi | 30 Handheld Wi-Fi | Allow to server :443 only; isolate from other VLANs |
| RFID readers (desk, self-service) | 10.99.40.11 to 10.99.40.19 (fixed) | vendor-specific | vendor protocol | 40 Devices | Server may open connections to the readers' port; readers may not initiate anything |
| Gate controller | 10.99.40.21 | vendor-specific | vendor protocol | 40 Devices | Server to gate on the vendor port; gate to server only if it posts events |
| Cameras | 10.99.40.31 to 10.99.40.39 | 554 for RTSP; 443 or 80 for a snapshot URL | RTSP or HTTP(S) | 40 Devices (or a separate CCTV VLAN) | Server to camera for snapshots only; no camera to internet |
| Card printer | 10.99.40.41 | 9100 (raw printing) or the vendor's port | raw TCP or vendor | 40 Devices | Server to printer on that port only |
| ILMS server (the existing library system) | 10.99.50.10 | 6001 in the sample file (SIP2 or NCIP port of that system) | SIP2 over TCP (NCIP is not implemented) | 50 ILMS | Server to ILMS on that port only; SIP2 has no encryption, so use a VPN, stunnel or TLS-terminating proxy (`docs/sip2.md`) |
| Mail relay (e-mail notifications) | 10.99.50.25 | 587 (submission) or 25 | SMTP | 50 Services | Server to relay on that port only; the relay decides what leaves the site |

Notes on the example:

- One `/24` per VLAN leaves room for growth; a small library could use one VLAN, but keep devices apart from staff PCs where it
  is cheap to do so.
- Give fixed addresses (or DHCP reservations) to the server, readers, gate, cameras, printer, ILMS and relay. Staff desks and
  handhelds can use DHCP.
- The application listens on `127.0.0.1:8000` by default and expects TLS in front (`deployment.md`, "TLS and the reverse proxy").
  Never expose the devices or the application port to the internet.
- No internet access is needed at all after installation. Updates arrive on media as signed packages.
- SMS and print notification channels would need their own gateways; none is included in the example.

## 2. Where each address is set

| What | Setting in `config/config.toml` | In the sample file | Notes |
|---|---|---|---|
| The application's own listening address | `[app] host`, `[app] port` (also `AISYS_HOST`, `AISYS_PORT`) | `127.0.0.1`, `8000` | Keep on loopback behind a proxy; use `0.0.0.0` or the server's address only if staff browsers connect straight to it |
| Serving HTTPS directly | `[server] tls_certfile`, `[server] tls_keyfile` (also `AISYS_TLS_CERTFILE`, `AISYS_TLS_KEYFILE`) | both empty | Both or neither; PEM files kept outside the repository |
| ILMS host and port | `[ilms] endpoint` (host name or IP), `[ilms] port`, `[ilms] protocol` (`sip2` or `ncip`), `[ilms] driver` | commented out (`#endpoint`, `#port = 6001`), driver `mock` | Used only by a real connector; the mock ignores the address. The Settings page shows the value read-only |
| ILMS retries | `[ilms] retries`, `[ilms] backoff_s` | `2`, `0.1` | How often a failed request is retried |
| Staff reader, handheld, gate, camera, card printer | one `[[devices]]` block each: `id`, `kind` (`staff_reader`, `handheld`, `gate`, `camera`, `card_printer`), `driver` | five blocks, all `driver = "mock"` | **Today a `[[devices]]` block carries only these three keys. It has no address field.** The address of a real device belongs to the driver written for it (code registered in `src/aisys/rfid/registry.py`), which is where its host and port would be read. Record the device's address in section 1 and in the site's own notes until that driver exists |
| E-mail relay | `[notifications] email` (driver name) | `"mock"` | Likewise: the setting that names a real relay's host and port belongs to the real e-mail driver, which does not exist yet |
| Backup folder | `[backup] dir` | `data/backups` | A local path; copy it off the server |

What the administrator can change in the browser (Settings page) never includes an address; addresses, devices and drivers stay in
the file and need a restart.

## 3. Reading the plan back

| Question | Answer |
|---|---|
| Which machines talk to the server? | Staff desks and handhelds, over HTTPS to the server's port 443 |
| Which does the server call out to? | Readers, gate, cameras and card printer (VLAN 40), the ILMS, the mail relay |
| What must be open to the internet? | Nothing |
| What do I check first when a device is silent? | Its address in section 1, then the `[[devices]]` block, then the **Audit log** and the structured log for the failure |

## 4. Checklist: replace the example values on site

- [ ] Choose the site's own address range and VLAN numbers; replace every `10.99.x.y` and every VLAN number in section 1.
- [ ] Record the real address of the server, each reader, the gate controller, each camera, the card printer, the ILMS server and the mail relay.
- [ ] Confirm each device's real port and protocol from its manual; replace the "usual" ports in section 1.
- [ ] Give the fixed devices static addresses or DHCP reservations; write them into the network documentation.
- [ ] Set `[app] host` and `port`, and the TLS proxy or `[server] tls_certfile` and `tls_keyfile`, for the real server.
- [ ] Set `[ilms] driver = "sip2"`, `endpoint`, `port`, `login`, `institution` and `location`; set `AISYS_ILMS_PASSWORD` in the service's environment; agree the SIP2 account with the ILMS owner and run `aisys doctor` (the SIP2 client has only been tested against a simulator).
- [ ] For each real device, register its driver and set `driver = "<name>"` in its `[[devices]]` block (see `rfid.md`).
- [ ] Agree the mail relay's address, port and sender with the site's IT; set up the real e-mail driver.
- [ ] Write the firewall rules from section 1 into the real firewall; test that a staff PC reaches only the server's HTTPS port.
- [ ] Check that no device and no application port is reachable from outside the site.
- [ ] Run `python -m aisys.cli doctor` on the server and fix every FAIL (`deployment.md`).
- [ ] Keep a copy of the final plan with the backups, and put the date and the person who approved it on it.
- [ ] Delete this example's values, or mark the file as replaced, so nobody mistakes them for the real plan.
