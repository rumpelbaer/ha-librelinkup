# Home Assistant LibreLinkUp

Brings the glucose readings that are shared with a LibreLinkUp account into Home
Assistant, as one device per person, with entities for the value, the trend and
how fresh the reading is.

> **Not a medical device, and not affiliated with Abbott.** See
> [Disclaimer](#disclaimer) before using this.

Current release: **0.1.0**. Install it as a HACS custom repository or by hand.

## Features

- One config entry per shared person; several people on one account are
  supported and stay strictly separate.
- One login and one request per account and interval, no matter how many people
  are configured.
- Glucose value, trend, low and high flags, plus diagnostic entities for the
  reading's timestamp, its age and whether it has gone stale.
- Keeps the last reading through short outages, backs off when the API asks for
  it, and reports a person as unavailable when their reading really is too old.
- Re-authentication flow when the LibreLinkUp password changes.
- Repair issues when a share is removed, when the account cannot log in, and for
  entries that predate the person selection.
- Diagnostics built from an allowlist, with no credentials, identifiers or
  glucose values in them.
- Optional blueprint that colours a light by glucose range.

## Requirements

- Minimum Home Assistant version: **2026.2.0**. Live-tested with Home Assistant
  **2026.9.0**.
- A **LibreLinkUp follower account** that at least one FreeStyle Libre user
  shares their data with.

The integration only reads what is already shared with the follower account. It
never talks to a sensor, and it does not need — or accept — the credentials of
the FreeStyle Libre / LibreLink account that owns the sensor.

### Use a dedicated follower account

Create a separate LibreLinkUp account for Home Assistant and have the data shared
with that one. Its password is stored in Home Assistant unencrypted (see
[Privacy and security](#privacy-and-security)), so it should not be the account
used on a phone, and it should not be an account that can change anything.

## Installation

### HACS (custom repository)

This integration is **not** in the HACS default store, so it has to be added as a
custom repository.

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/rumpelbaer/ha-librelinkup` with category
   **Integration**.
3. Find **LibreLinkUp** in HACS and install it.
4. Restart Home Assistant.
5. Set it up under **Settings → Devices & services**.

### Manual

1. Copy the `custom_components/librelinkup/` directory of this repository to
   `/config/custom_components/librelinkup/` on your Home Assistant instance.
2. Restart Home Assistant.
3. Set it up under **Settings → Devices & services**.

### The blueprint

The blueprint is not part of the integration and is not installed by HACS. See
[Glucose light blueprint](#glucose-light-blueprint) for how to add it.

## Setup

1. **Settings → Devices & services → Add integration → LibreLinkUp**.
2. Enter the e-mail address and password of the LibreLinkUp follower account.
3. If the account has access to more than one person, pick the person this entry
   should follow. With a single shared person this step is skipped and the entry
   is created straight away.

Each person is its own config entry, with its own device and its own entities.
To add a second person from the same account, run the setup again and pick that
person — see [Several people on one account](#several-people-on-one-account).

## Entities

Each person gets one device with these entities:

| Entity | Type | State | Notes |
| --- | --- | --- | --- |
| Glucose | sensor | number, mmol/L | Device class `blood_glucose_concentration`. Carries the reading as received in mg/dL in the `glucose_mg_dl` attribute. |
| Trend | sensor | enum | `not_determined`, `falling_rapidly`, `falling`, `stable`, `rising`, `rising_rapidly`. Attributes `trend_code` (the raw code) and `trend_arrow` (`↓↓`, `↓`, `→`, `↑`, `↑↑`, or none for `not_determined`). |
| Low | binary sensor | `on` / `off` | The low flag LibreLinkUp reports with the reading. |
| High | binary sensor | `on` / `off` | The high flag LibreLinkUp reports with the reading. |
| Data Stale | binary sensor | `on` / `off` | Device class `problem`, so `on` shows as *Problem*. Diagnostic. `on` once the reading is older than 5 minutes — and unavailable itself once it passes 15, see below. |
| Last Reading | sensor | timestamp | When the reading was taken. Diagnostic. |
| Reading Age | sensor | number, minutes | Age of the reading. Diagnostic, **disabled by default**, and deliberately without a state class: it changes every minute and long-term statistics of it carry nothing worth keeping. Enable it per entity if you want it. |

### Units

LibreLinkUp delivers the reading in mg/dL. The integration converts it and
publishes **mmol/L as the native unit**; Home Assistant can display it in mg/dL
per entity (**entity settings → Unit of measurement**), which changes the
display only — the native unit stays mmol/L, and the unconverted mg/dL value is
always on the `glucose_mg_dl` attribute.

## Freshness and availability

A person's entities become unavailable when **their own reading** is older than
15 minutes, not when the last request failed. A sensor that stopped delivering,
or a share that was removed, therefore stops the value instead of freezing it:
LibreLinkUp keeps answering with the last known reading in both cases.

The full sequence for one person, measured from the timestamp of their reading:

| Age of the reading | Glucose and the other entities | Data Stale |
| --- | --- | --- |
| up to 5 minutes | the current value | `off` |
| more than 5, less than 15 minutes | still the last value | `on` |
| 15 minutes or more | `unavailable` | `unavailable` |

**Checking Data Stale alone is not enough.** Past 15 minutes every entity of
that person goes unavailable, Data Stale included — so it is not `on` at that
point, it is `unavailable`, and a condition that only asks whether Data Stale is
`on` will read the oldest data as if it were fine. An automation that acts on
the glucose value has to require that value to be a usable number as well, for
example:

```yaml
condition:
  - condition: template
    value_template: >-
      {{ not is_state('binary_sensor.person_data_stale', 'on')
         and states('sensor.person_glucose') | float(-1) >= 0 }}
```

The blueprint below already does both: it falls back to the stale colour when
Data Stale is `on` *or* when the glucose state does not parse as a number, which
covers `unavailable` and `unknown` alike.

Because the age is measured against the Home Assistant clock and LibreLinkUp
timestamps are UTC, the host clock matters: a host clock running fast makes every
reading look expired. A reading dated more than two minutes ahead of the host
clock is refused rather than trusted, so a badly wrong clock in the other
direction cannot keep an old reading "fresh" forever.

### Rate limits and outages

When a request fails, the last known reading keeps being served for up to 15
minutes while the integration retries. After that the update is reported as
failed, and each person's entities go unavailable on their own reading's age as
described above.

- **HTTP 429 (rate limited).** Polling slows down for as long as the API's
  `Retry-After` header asks for, within sane bounds; without that header a
  default delay is used. Because the account shares one coordinator, one rate
  limit slows the whole account down once, not once per person.
- **HTTP 5xx.** Polling backs off progressively and returns to the normal
  interval on the first successful request.
- **Malformed responses and network errors.** Retried at the normal interval; no
  backoff, since the server is not the thing that needs relief.

## Several people on one account

Run the setup once per person. Each run creates its own config entry, its own
device and its own set of entities.

Behind that, the entries of one LibreLinkUp account share a single API client and
a single coordinator, so the account logs in once and makes **one request per
interval no matter how many people are configured** — instead of one per person.
The data stays separated: a reading is only ever matched to the person's own
identifier, never to its position in the API response, and people the account
shares but Home Assistant has no entry for are discarded on arrival.

Polling runs every **60 seconds**, against the `/llu/connections` endpoint that
carries the current reading of every shared person.

## Re-authentication

When LibreLinkUp rejects the stored password, Home Assistant shows a
re-authentication prompt (and a matching repair notice) asking for the current
password. Enter it there.

This happens **once per account**, not once per person: the confirmed password is
written to every entry of that account and all of them are reloaded, so a
restart cannot fall back to the old one.

A login that fails for another reason — for example because the account has to
confirm something in the LibreLinkUp app — does **not** ask for the password. It
raises a repair issue instead, because a new password would not fix it.

## Glucose light blueprint

`blueprints/automation/rumpelbaer/glucose_light.yaml` colours a light by glucose
range, with configurable thresholds, colours, brightness, an optional time
window and optional presence conditions.

### Installing it

In **Settings → Automations & scenes → Blueprints → Import blueprint**, paste:

```
https://github.com/rumpelbaer/ha-librelinkup/blob/main/blueprints/automation/rumpelbaer/glucose_light.yaml
```

Or copy the file to `/config/blueprints/automation/rumpelbaer/glucose_light.yaml`
and reload automations.

### Inputs

Three inputs are required: the **glucose sensor**, the **Data Stale** binary
sensor *of the same person*, and the **target light**. Everything else has a
default.

### Ranges

Thresholds are entered in mmol/L. The defaults, and how a reading is classified:

| Range | Condition | Default thresholds |
| --- | --- | --- |
| Very low | below the very low threshold | below 3.0 |
| Low | below the low threshold | 3.0 up to but not including 3.9 |
| Normal | up to and including the high threshold | 3.9 through 10.0 |
| High | up to and including the very high threshold | above 10.0 through 13.9 |
| Very high | above the very high threshold | above 13.9 |
| Stale | Data Stale is `on`, or the glucose state is not a usable number | — |

The stale range also covers `unavailable` and `unknown`, and a glucose entity
reporting an unexpected unit: the light then shows the stale colour rather than
guessing a glucose range.

### Default palette and brightness

| Range | Default colour (RGB) | Default brightness |
| --- | --- | --- |
| Very low | `[205, 75, 65]` | 75 % |
| Low | `[235, 165, 45]` | 70 % |
| Normal | `[255, 180, 110]` | 60 % |
| High | `[120, 140, 210]` | 70 % |
| Very high | `[95, 70, 160]` | 75 % |
| Stale | `[115, 110, 120]` | 45 % |

Every colour and every brightness is a separate input, so the palette is a
starting point, not a fixed scheme.

### Normal range as warm white

`normal_color_mode` decides how the **normal** range is shown:

- `rgb` (default) — uses the *Normal — RGB color* input.
- `color_temp` — uses `normal_color_temp_kelvin` instead, default **2700 K**,
  adjustable from **2200 K to 4000 K**. Suits lights with a dedicated white
  channel, which render warm white more naturally than an RGB mix.

Only the normal range follows this setting. The warning ranges and the stale
colour are always sent as RGB, so the light has to be RGB-capable either way.

There is no capability detection and no fallback: if you choose `color_temp` for
a light that does not support colour temperature, Home Assistant will return a
service error for those calls.

### Display units

The blueprint works whether the glucose entity is displayed in mmol/L or in
mg/dL. It reads the entity's unit and converts an mg/dL state back to mmol/L
before comparing, using the **flat factor of 18.0** — the same factor Home
Assistant used to produce that mg/dL display value, so the conversion is exactly
undone and a reading sitting on a threshold lands in the same range in either
unit. This is deliberately not the 18.0182 used in diabetes care; 18.0182 would
shift threshold cases by about 0.1 % into the more alarming band.

### When the light is active

`activation_mode` selects when the light should react: `always` (default),
`time`, `presence`, `time_and_presence` or `time_or_presence`. The time window
supports overnight ranges such as 20:00 to 07:00, and setting both times to the
same value means all day. Presence counts an entity as present when its state is
`home` or `on`, so person entities, input booleans and binary sensors all work.

While the conditions are not met, `inactive_behavior` either turns the light off
(default) or leaves it as it is.

### Where to put it

A glucose light tells everyone in the room how the person is doing, guests
included. Pick the room accordingly.

## Privacy and security

### What the integration itself handles

- **Only configured people.** A LibreLinkUp account often shares more than one
  person. Readings of people without a config entry are discarded on arrival:
  not stored, not logged, not exposed as attributes, not in diagnostics.
- **What leaves your network.** Only requests to Abbott's LibreLinkUp API:
  `POST /llu/auth/login` and `GET /llu/connections` against `api.libreview.io`
  or the regional host your account is redirected to (`api-eu`, `api-eu2`,
  `api-de`, `api-fr`, `api-jp`, `api-ap`, `api-au`, `api-ae`, `api-ca`). Nothing
  else.
- **No telemetry.** No analytics, no crash reporting, no third-party service.
- **Minimal fetching.** `/llu/connections` already carries the current reading of
  each shared person. The graph endpoint, which would additionally return about
  twelve hours of history, is not called.
- **Logs.** Log messages never contain the e-mail address, the password, the
  token, a patient identifier or a glucose value, and errors are logged by kind
  ("HTTP 429"), not by message, because the API's own error objects carry the
  request URL and the bearer token.
- **Credential storage.** The password is stored in `.storage/core.config_entries`
  unencrypted — Home Assistant has no secret store for config entries, so any
  Home Assistant administrator and any backup can read it. This is why a
  dedicated follower account is recommended. The session token lives in memory
  only and is never written to disk or a log.
- **Transport.** All requests go over HTTPS through Home Assistant's own HTTP
  client with certificate validation enabled, and a login may only be redirected
  to a host from the fixed list of Abbott regions above.

### Diagnostics

The diagnostics download is built from an allowlist. The part this integration
contributes contains only:

| Field | Meaning |
| --- | --- |
| `integration_version` | Version from the manifest |
| `account_loaded` | Whether the account's runtime exists |
| `region`, `api_host` | Which regional API the account talks to |
| `token_present` | Whether a session token exists — never the token |
| `polling_enabled`, `update_interval_seconds` | Current polling state |
| `last_update_success`, `seconds_since_last_success` | Whether the last poll worked, and how long ago (relative, never a point in time) |
| `failure_label` | The *kind* of an ongoing failure, e.g. `HTTP 429` |
| `configured_entries`, `configured_patients`, `patients_with_data` | Counts only |

It contains **no** e-mail address, password, token, account ID, patient ID,
person name, glucose value or measurement timestamp — not even hashed.

Two things around it are Home Assistant's, not the integration's, and are worth
knowing before attaching a diagnostics file to a public issue:

- Home Assistant wraps the payload in its own envelope: system information about
  your instance, the list of installed custom components, this integration's
  manifest, and setup timings. The download's filename contains the config entry
  ID.
- Repair issues are persisted in `.storage` and are titled with the config entry
  title, which contains the person's name. No patient ID is ever persisted there.

### Recorder and backups

Home Assistant stores every state change of every entity, including long-term
statistics of the glucose sensor. That is a complete glucose history in your
Home Assistant database. To keep less of it, exclude the entities in your
`recorder:` configuration:

```yaml
recorder:
  exclude:
    entities:
      - sensor.person_glucose
      - sensor.person_trend
```

A Home Assistant backup contains both the stored credentials and the recorder
database, so a backup of your instance contains the LibreLinkUp password and the
glucose history. Treat backups accordingly.

## Troubleshooting

**Setup says the e-mail address or password is invalid.** The credentials were
rejected by LibreLinkUp. Check them in the LibreLinkUp app. Note that the
follower account is the one to use, not the account that owns the sensor.

**Home Assistant asks to re-authenticate.** The stored password is no longer
accepted. Enter the current one in the prompt; it is applied to every entry of
that account at once. If you have several people configured, you only get one
prompt — answering it brings all of them back.

**A repair issue says the login was refused.** The account exists and the request
arrived, but LibreLinkUp refused to log in for a reason a password cannot fix.
Open the LibreLinkUp app with that account and look for something to confirm, for
example new terms of use.

**A repair issue says the entry has no person selected.** The entry predates the
person selection, so it cannot be loaded without risking data from the wrong
person. Remove it and add it again.

**A repair issue says the share was removed.** The person no longer shares their
data with this account. Share it again in the LibreLinkUp app, or remove the
config entry.

**Data Stale is on.** No reading newer than 5 minutes has arrived. Usually the
phone running LibreLinkUp is out of range of the sensor, offline, or not running
the app in the background. Values older than 5 minutes are still shown until they
pass 15 minutes.

**Entities are unavailable after about 15 minutes.** The person's last reading
has expired. Check **Last Reading** on that device: if it really is that old, the
sensor or the phone is not delivering. If it looks current, check that the Home
Assistant host clock is correct — the age is measured against it.

**Setup says the region is not supported.** The account is hosted in a region
this integration does not know yet. Open an issue with the region code from the
log.

**Setup says the data could not be read.** The API answered with something
unexpected, which usually means Abbott changed it. Open an issue.

**HACS does not show the integration.** It is not in the HACS default store. Add
this repository under **Custom repositories** with category **Integration**
first, and restart Home Assistant after installing.

**Everything stopped working at once.** The integration identifies itself as a
specific LibreLinkUp app version. Abbott has rejected outdated clients before; if
logins start failing for everyone, that value may need raising — open an issue.

## Limitations

- Uses an undocumented LibreLinkUp API. It can change or stop working at any
  time; see [Disclaimer](#disclaimer).
- Read-only and poll-based: one request per account per minute. There is no push,
  and readings therefore arrive with up to a minute of delay on top of whatever
  delay LibreLinkUp itself has.
- Only the current reading. No history is fetched or backfilled, so entities
  start empty and build history from the moment they are set up.
- Only regions on the fixed host list are supported.
- The password is stored unencrypted in Home Assistant's config entry storage.
- The blueprint is not installed by HACS and has to be imported separately.

## Disclaimer

**Not a medical device.** This integration is for information and automation
only. Do not use its entities, or a lamp they control, as the basis for
treatment, dosing or any other medical decision. Check the value and the sensor
itself in the official LibreLinkUp app.

**Not affiliated with Abbott.** This project is not built, supported or endorsed
by Abbott, and it uses an API that Abbott does not document for third parties.
That API can change or stop working at any time, and using it may conflict with
the terms of service of your LibreLinkUp account. Whether to use it is your
decision. There is no guarantee that this integration keeps working.

**Glucose data is health data.** A glucose light makes it visible to everyone in
the room, and the recorder keeps a full history of it. See
[Privacy and security](#privacy-and-security).

## Development

```bash
python -m pip install -r requirements_test.txt
python -m pytest tests/ -v --cov=custom_components/librelinkup --cov-branch
```

The suite never talks to the real API.

## License

MIT. See [LICENSE](LICENSE).
