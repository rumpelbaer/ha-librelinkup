# Home Assistant LibreLinkUp

Brings the glucose readings that are shared with a LibreLinkUp account into Home
Assistant.

> **Not a medical device.** This integration is for information and automation
> only. Never use its entities, or a lamp they control, for treatment, dosing or
> any other medical decision. Always check the value and the sensor itself in the
> official LibreLinkUp app.

> **Not affiliated with Abbott.** This project is not built, supported or endorsed
> by Abbott, and it uses an API that Abbott does not document for third parties.
> That API can change or stop working at any time, and using it may conflict with
> the terms of service of your LibreLinkUp account. Whether to use it is your
> decision.

## Features

- One config entry per shared person; several people on one account are
  supported and stay strictly separate.
- One login and one request per account and interval, no matter how many people
  are configured.
- Glucose value, trend, low and high flags, plus diagnostic entities for the
  reading's timestamp, its age and whether it has gone stale.
- Keeps the last reading through short outages and reports the account as
  unavailable when it really is.
- Re-authentication flow when the LibreLinkUp password changes.
- Repair issues when a share is removed, when the account cannot log in, and for
  entries that predate the person selection.
- Diagnostics that describe the problem without identifying anyone.
- Optional blueprint that colours an RGB light by glucose range.

## Requirements

- Home Assistant **2026.2** or newer (developed and tested against 2026.2.3).
- A LibreLinkUp account that at least one FreeStyle Libre user shares their data
  with. The integration only reads what is already shared with that account; it
  never touches a sensor directly.

### Use a dedicated follower account

Create a separate LibreLinkUp account for Home Assistant and have the data shared
with that one. Its password is stored in Home Assistant in plain text (see
[Security](#security)), so it should not be the account used on a phone, and it
should not be an account that can change anything.

## Installation

### HACS (custom repository)

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/rumpelbaer/ha-librelinkup` with category
   **Integration**.
3. Install **LibreLinkUp** and restart Home Assistant.

This integration is not in the HACS default list, so it has to be added as a
custom repository.

### Manual

1. Copy `custom_components/librelinkup` into the `custom_components` directory of
   your Home Assistant configuration.
2. Restart Home Assistant.

## Setup

1. **Settings → Devices & services → Add integration → LibreLinkUp**.
2. Enter the e-mail address and password of the LibreLinkUp account.
3. If the account has access to more than one person, pick the person this entry
   should follow.

### Several people

Run the setup once per person. Each run creates its own config entry, its own
device and its own set of entities, and all entries of one account share a single
login and a single request per interval. A reading is only ever matched by the
person's own identifier, never by its position in the API response, so entities
cannot swap readings.

## Entities

Each person gets one device with these entities:

| Entity | Type | Notes |
| --- | --- | --- |
| Glucose | sensor | mmol/L, `blood_glucose_concentration`. Home Assistant can display it in mg/dL per entity. Carries the raw `glucose_mg_dl` attribute. |
| Trend | sensor | Enum: `not_determined`, `falling_rapidly`, `falling`, `stable`, `rising`, `rising_rapidly`. |
| Low / High | binary sensor | The flags LibreLinkUp reports with the reading. |
| Data Stale | binary sensor | On once the reading is older than 5 minutes. |
| Last Reading | sensor | Timestamp of the reading. Diagnostic. |
| Reading Age | sensor | Age in minutes. Diagnostic, **disabled by default** because it changes every minute and would otherwise fill the recorder. |

### When entities go unavailable

A person's entities become unavailable when **their own reading** is older than
15 minutes, not when the last request failed. A sensor that stopped delivering,
or a share that was removed, therefore stops the value instead of freezing it:
LibreLinkUp keeps answering with the last known reading in both cases.

Any automation that acts on the glucose value should check **Data Stale** first.

This also means the clock of the Home Assistant host matters: LibreLinkUp
timestamps are UTC, and a host clock that is off by more than 15 minutes makes
every reading look expired.

## Blueprint

`blueprints/automation/rumpelbaer/glucose_light.yaml` colours an RGB light by
glucose range, with configurable thresholds, colours, brightness, a time window
and presence conditions.

Thresholds are entered in mmol/L. The automation reads the unit of the glucose
entity and converts a mg/dL reading before comparing, so switching the display
unit does not change its behaviour. If the unit is something else entirely, it
shows the stale colour rather than guessing a range.

A glucose light tells everyone in the room how the person is doing, guests
included. Pick the room accordingly.

## Re-authentication

When LibreLinkUp rejects the stored password, Home Assistant asks for the new one
**once per account**, not once per person, and the confirmed password is written
to every entry of that account so a restart cannot fall back to the old one.

A login that fails for another reason — for example because the account has to
confirm something in the LibreLinkUp app — does **not** ask for the password. It
raises a repair issue instead, because a new password would not fix it.

## Privacy

- **Only configured people.** A LibreLinkUp account often shares more than one
  person. Readings of people without a config entry are discarded on arrival:
  they are not stored, not logged, not exposed as attributes and not part of
  diagnostics.
- **What leaves your network.** Only requests to Abbott's LibreLinkUp API:
  `POST /llu/auth/login` and `GET /llu/connections` against
  `api.libreview.io` or the regional host your account is redirected to
  (`api-eu`, `api-eu2`, `api-de`, `api-fr`, `api-jp`, `api-ap`, `api-au`,
  `api-ae`, `api-ca`). Nothing else, ever.
- **No telemetry.** No analytics, no crash reporting, no third-party service.
- **Minimal fetching.** `/llu/connections` returns the current reading of each
  shared person. The integration does not call the graph endpoint, which would
  additionally return about twelve hours of history it has no use for.
- **Logs.** Log messages never contain the e-mail address, the password, the
  token, a patient identifier or a glucose value, and errors are logged by kind
  ("HTTP 429"), not by message, because the API's own error objects carry the
  request URL and the bearer token.
- **Diagnostics.** The diagnostics download is built from an allowlist: region,
  API host, poll interval, whether the last poll succeeded and how many seconds
  ago, the kind of any ongoing failure, and how many entries and people are
  configured. No e-mail, password, token, account ID, patient ID, name, glucose
  value or measurement timestamp.
- **Recorder.** Home Assistant stores every state change of every entity,
  including long-term statistics of the glucose sensor. That is a complete
  glucose history in your Home Assistant database. To keep less of it, exclude
  the entities in your `recorder:` configuration, for example:

  ```yaml
  recorder:
    exclude:
      entities:
        - sensor.person_glucose
        - sensor.person_trend
  ```

- **Backups.** A Home Assistant backup contains both the stored credentials and
  the recorder database, so a backup of your instance contains the LibreLinkUp
  password and the glucose history. Treat backups accordingly.

## Security

- The LibreLinkUp password is stored in
  `.storage/core.config_entries`, unencrypted. Home Assistant does not offer a
  secret store for config entries, so any Home Assistant administrator and any
  backup can read it. This is why a dedicated follower account is recommended.
- The session token lives in memory only and is never written to disk or a log.
- All requests go over HTTPS through Home Assistant's own HTTP client, with
  certificate validation enabled.
- The region a login is redirected to is only accepted from a fixed list of
  Abbott hosts, so a manipulated response cannot redirect requests elsewhere.

## Troubleshooting

**Entities are unavailable.** Check the **Data Stale** and **Last Reading**
entities: if the reading is older than 15 minutes, the sensor or the phone
running LibreLinkUp is probably not delivering. Also check that the host clock is
correct.

**A repair issue says the share was removed.** The person no longer shares their
data with this account. Share it again in the LibreLinkUp app, or remove the
config entry.

**A repair issue says the login was refused.** The account exists and the request
arrived, but LibreLinkUp refused to log in for a reason a password cannot fix.
Open the LibreLinkUp app with that account and look for something to confirm, for
example new terms of use.

**Setup says the region is not supported.** The account is hosted in a region
this integration does not know yet. Open an issue with the region code from the
log.

**Setup says the data could not be read.** The API answered with something
unexpected, which usually means Abbott changed it. Open an issue.

**Everything stopped working at once.** The integration identifies itself as a
specific LibreLinkUp app version. Abbott has rejected outdated clients before; if
logins start failing for everyone, that value (in `const.py`) may need raising.

## Development

```bash
python -m pip install -r requirements_test.txt
python -m pytest tests/ -v --cov=custom_components/librelinkup --cov-branch
```

The suite never talks to the real API. Behaviour that can only be confirmed
against Abbott's servers — the response format, that `/llu/connections` carries
the same current reading as the graph endpoint, the timestamp format — was
verified manually with local scripts that are deliberately not part of this
repository, so that nothing in it asks a user to type their credentials into
something other than Home Assistant.

## License

No license has been chosen yet. Until one is added, the usual copyright default
applies and this code is not licensed for reuse.
