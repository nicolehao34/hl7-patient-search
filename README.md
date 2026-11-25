# HL7 v2 Patient Search

A compact reference implementation that receives HL7 v2 messages over MLLP, acknowledges them,
normalizes medically useful fields into SQLite, and exposes patient/workflow search over HTTP.

This project answers the following exercise:

> Health systems continuously stream HL7 v2 data. Build a server a hospital can configure as an
> interface destination, then provide a tool that finds patients eligible for workflows—for example,
> patients recently admitted to a particular hospital. Explain how different message types affect
> search and demonstrate distinct result sets.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run hl7-search serve --mllp-host 127.0.0.1
```

The command binds MLLP to `127.0.0.1:2575` for local testing. In a second terminal, send ten synthetic
HL7 v2.5.1 messages over MLLP:

```bash
uv run python examples/seed_demo.py
```

The seed sends six ADT events, three ORU lab results, and one ORM order across two hospitals. It
prints an `MSA|AA` acknowledgment for each message. Dates are relative to the first seed run.
Stable control IDs make reruns safe against the same database. To regenerate dates, seed a fresh
database by starting the server with a new `--database` path.

Open the [search page](http://127.0.0.1:8000/) or try the endpoints in [Swagger UI](http://127.0.0.1:8000/docs).

- **Patients** starts with a seven-day admission filter and shows the most recent admissions first.
  With fresh seed data, set **Hospital** to `GENERAL` to see Avery, Blake, and Devon. Click **View
  patient** to see the encounters, observations, and orders behind a row.
- **HL7 messages** searches the stored feed. Choose `ORU` and set **Ordering provider ID** to
  `PROVIDER123` to see three lab messages. Click **View HL7** to inspect the stored payload.

Use `uv run hl7-search send examples/oru_r01_alice.hl7` to send an individual checked-in fixture.
`docker compose up --build` starts the same service with a persistent volume.

## Hospital/interface-engine configuration

Configure the sending interface (Epic Bridges, Cloverleaf, Rhapsody, Mirth, etc.) as follows:

| Setting | Value |
|---|---|
| Transport | TCP/IP MLLP (lower-layer protocol) |
| Host / port | receiver DNS name / `2575` |
| Framing | start `0x0B`; end `0x1C 0x0D` |
| Character set | UTF-8 (agree this in the site interface specification) |
| HL7 versions | 2.3–2.8 ER7; examples use 2.5.1 |
| ACK | synchronous application ACK; wait for `AA`, retry on timeout/connection loss |
| Recommended feed | ADT A01/A02/A03/A04/A05/A06/A07/A08, ORU R01, order messages |
| Identity | stable `MSH-10`; populated `MSH-3`, `MSH-4`, `PID-3` assigning authority |
| Connectivity | private network/VPN, IP allowlist, and TLS tunnel where required |

The receiver handles multiple frames per TCP connection. A message is committed in one database
transaction before `AA` is sent. The tuple `(MSH-3, MSH-4, MSH-10)` is unique, so a sender may safely
retry after an ambiguous disconnect; duplicate deliveries receive `AA` without duplicating clinical
data. Malformed messages receive `AE`; unrecoverable processing failures receive `AR`.

This is a reference implementation, not a drop-in production PHI system. A deployment should add
TLS/mTLS termination, authentication for the HTTP API, network isolation, encryption at rest, audit
logging, secrets management, retention/deletion policies, monitoring, backups, a dead-letter queue,
and a signed site-specific interface/conformance profile. Never put PHI in ordinary application logs.

## How messages become searchable

HL7 v2 is event-oriented, not a canonical patient database. The service preserves every raw message
and projects selected fields into queryable tables:

| Input | Meaning | Projection and search impact |
|---|---|---|
| `ADT^A01` / `A04` / `A05` | admit, register, pre-admit | Upserts `PID` demographics and opens/updates the `PV1-19` visit using class, location and admit time. |
| `ADT^A02`, `A06`, `A07` | transfer or class change | Updates the same facility/visit, so searches use the latest location/class. |
| `ADT^A03` | discharge | Marks the visit discharged and records `PV1-45`; it no longer matches `status=active`. |
| `ADT^A08` | demographic/visit update | Applies non-empty fields without erasing previously known values. |
| `ORU^R01` | unsolicited result | Stores individual `OBX` results and indexes `OBR-16` ordering providers. |
| `ORM^O01` | general order | Stores `OBR` order identifiers and test or procedure codes. Indexes `OBR-16` ordering providers. |
| Other types | site-specific or unsupported | Stores the message and header metadata. Projection depends on the recognized segments and message type. |

Patient identity is the pair `PID-3.1` + `PID-3.4` (ID and assigning authority), not name or date of
birth. Encounters are keyed by sending facility + `PV1-19`. Real health systems require cross-domain
identity resolution (MRN merges such as A40, enterprise identifiers, and site-specific CX selection);
that is deliberately identified as the next major feature rather than guessed here.

The parser honors the field and encoding delimiters declared by `MSH-1`/`MSH-2`, repeated patient
identifiers, common escape sequences, CR or LF segment endings, and decreasing-precision HL7
timestamps. It retains observation values as strings because `OBX-2` permits many data types; numeric
threshold search casts values only when explicitly requested.

## Search design

MLLP and HTTP ingestion share the parser and SQLite store. A single transaction saves the HL7
payload and its header, patient, encounter, clinical, and provider records. This keeps the source
message available for inspection and makes its indexed fields searchable as soon as the transaction
commits.

**Patient discovery** starts with patients and encounters. Admission filters use `PV1-44`, and the
results are ordered by admission time, newest first. A patient with multiple matching encounters can
appear more than once. Observation and order filters alone search the patient's history. Adding a
message filter requires the header, provider, and clinical criteria to match the same source
message. Encounter criteria still apply independently to the patient's visits. A message-scoped
search can also return patients without an encounter when no encounter filter is set.

**Message history** starts with messages and returns metadata in descending message ID order
(newest ingested first). It can include messages without a linked patient. Filter by the sender's
`MSH-7` timestamp, then use the internal message ID to retrieve the stored HL7 text when needed.
Indexing the headers and providers lets history searches run without reparsing patient payloads.

## API

| Endpoint | Behavior |
|---|---|
| `GET /` | Serves the browser search page. |
| `POST /messages` | Accepts raw HL7 text. Returns `202` after storage, `202` with `duplicate: true` on retry, or `422` for invalid HL7. |
| `GET /patients` | Searches patient and encounter records by `facility`, `admitted_since`, `admitted_before`, `status`, `patient_class`, `mrn`, `observation_code`, `observation_min`, `observation_max`, and `order_code`, plus the message filters below. Numeric bounds require an observation code. |
| `GET /patients/{id}` | Returns demographics, encounters, observations, and orders for an internal patient ID. Returns `404` if the record is missing. |
| `GET /messages` | Lists metadata. Filter by internal `patient_id` for patient history. Pass the last item's `id` as `before_id` to fetch the next page with the same filters. |
| `GET /messages/{id}` | Returns metadata, ordering providers, extracted `segments`, and the stored HL7 payload in `raw`. Returns `404` if the record is missing. |
| `GET /health` | Reports service status and SQLite row counts. |

Both search endpoints default to `limit=50`, with a maximum of 500. Admission and message time
windows use inclusive starts and exclusive ends. Admission bounds compare to the stored visit
time. Use an ISO 8601 date-time without an offset when filtering the demo's admission times.

Both search endpoints accept these filters:

| Query parameters | HL7 field and matching rule |
|---|---|
| `sending_application`, `sending_facility` | Exact match against the stored `MSH-3` and `MSH-4` fields. |
| `message_since`, `message_before` | Inclusive start and exclusive end on `MSH-7`. Use ISO 8601 query values. Offsets are converted to UTC and omitted offsets are treated as UTC for the demo. The response's `message_at` uses UTC with no suffix. |
| `ordering_provider_id`, `ordering_provider_name` | Search every `OBR-16` repetition, including on ORU results. Match `XCN.1` exactly or the extracted `Given Surname` using SQLite's ASCII case-insensitive comparison. When both filters are set, they must match the same provider repetition. |
| `message_type`, `trigger_event`, `control_id` | Exact match on `MSH-9.1`, `MSH-9.2`, and `MSH-10`. |
| `event_code`, `event_reason`, `event_recorded_since`, `event_recorded_before` | `EVN-1`, `EVN-4`, and a half-open window on `EVN-2`. |
| `order_control`, `order_status`, `placer_order_id`, `filler_order_id` | `ORC-1`, `ORC-5`, and the identifier components of `ORC-2/3`. |
| `specimen_id`, `specimen_type` | Either identifier in `SPM-2` and the primary type code in `SPM-4`. |
| `note_source`, `note_contains` | Exact match on `NTE-2` and a literal substring match on decoded `NTE-3`, case-insensitive for ASCII. |
| `diagnosis_code`, `diagnosis_system`, `diagnosis_type` | Primary code and coding system in `DG1-3`, and type in `DG1-6`. |

Filters on the same segment type must match one occurrence of that segment. Filters on different
segment types must match one source message. The message detail response lists each extracted
occurrence in source order, with its segment name, position, and fields. It also includes fields
without dedicated filters, such as event occurrence time, order transaction time, specimen
collection interval and receipt time, note type, and diagnosis time. The new timestamp fields
use the same UTC convention as `message_at`. Malformed optional timestamps remain in the HL7 text.

```bash
# Two lab messages from GENERAL in a freshly seeded database
curl 'http://127.0.0.1:8000/messages?message_type=ORU&sending_application=DEMO_LIS&sending_facility=GENERAL'

# Avery and Casey have creatinine of at least 1.5 mg/dL in the seeded feed
curl 'http://127.0.0.1:8000/patients?ordering_provider_id=PROVIDER123&observation_code=2160-0&observation_min=1.5'

# The new CT order in the seeded feed
curl 'http://127.0.0.1:8000/messages?order_control=NW&placer_order_id=DEMO-CT-1'
```

To restrict message history to an interval, add `message_since=2026-11-25T00:00:00Z` and
`message_before=2026-11-26T00:00:00Z` (or use dates from your seed run). These filter the sender's
message time. Use `admitted_since` and `admitted_before` for a patient's admission time.

On first startup with an older SQLite database, the service backfills the new segment records
from retained HL7 in a write transaction. Databases from before the provider-search update also
get ordering providers and normalized message times. Allow extra startup time for large databases.
Legacy records that fail the current parser retain their existing metadata.

## The ten seeded HL7 messages

<details>
<summary>Show the ten payloads from the November 25, 2026 demo run</summary>

These synthetic messages are shown in ingestion order. Line breaks represent HL7 carriage-return
segment terminators. Blank lines separate messages for readability. The MLLP sender frames each
message individually.

```text
MSH|^~\&|DEMO_EHR|GENERAL|||20261125194618+0000||ADT^A01|UI-DEMO-ADT-A01-01|P|2.5.1
EVN|A01|20261125194618+0000
PID|1||UI-DEMO-01^^^GENERAL^MR||Demo^Avery||19800101
PV1|1|I|WARD^101^A||||||||||||||||UI-DEMO-V01|||||||||||||||||||||||||20261125194618+0000

MSH|^~\&|DEMO_EHR|GENERAL|||20261124194618+0000||ADT^A01|UI-DEMO-ADT-A01-02|P|2.5.1
EVN|A01|20261124194618+0000
PID|1||UI-DEMO-02^^^GENERAL^MR||Demo^Blake||19800101
PV1|1|I|WARD^101^A||||||||||||||||UI-DEMO-V02|||||||||||||||||||||||||20261124194618+0000

MSH|^~\&|DEMO_EHR|COMMUNITY|||20261123194618+0000||ADT^A01|UI-DEMO-ADT-A01-03|P|2.5.1
EVN|A01|20261123194618+0000
PID|1||UI-DEMO-03^^^COMMUNITY^MR||Demo^Casey||19800101
PV1|1|I|WARD^101^A||||||||||||||||UI-DEMO-V03|||||||||||||||||||||||||20261123194618+0000

MSH|^~\&|DEMO_EHR|GENERAL|||20261122194618+0000||ADT^A04|UI-DEMO-ADT-A04-04|P|2.5.1
EVN|A04|20261122194618+0000
PID|1||UI-DEMO-04^^^GENERAL^MR||Demo^Devon||19800101
PV1|1|O|WARD^101^A||||||||||||||||UI-DEMO-V04|||||||||||||||||||||||||20261122194618+0000

MSH|^~\&|DEMO_EHR|COMMUNITY|||20261111194618+0000||ADT^A01|UI-DEMO-ADT-A01-05|P|2.5.1
EVN|A01|20261111194618+0000
PID|1||UI-DEMO-05^^^COMMUNITY^MR||Demo^Ellis||19800101
PV1|1|I|WARD^101^A||||||||||||||||UI-DEMO-V05|||||||||||||||||||||||||20261111194618+0000

MSH|^~\&|DEMO_LIS|GENERAL|||20261125204618+0000||ORU^R01|UI-DEMO-ORU-R01-01|P|2.5.1
PID|1||UI-DEMO-01^^^GENERAL^MR||Demo^Avery||19800101
PV1|1|I|WARD^101^A||||||||||||||||UI-DEMO-V01|||||||||||||||||||||||||20261125194618+0000
OBR|1||DEMO-LAB-1|2160-0^Creatinine^LN|||20261125204618+0000|||||||||PROVIDER123^Chen^Maya
OBX|1|NM|2160-0^Creatinine^LN||1.8|mg/dL|||||F|||20261125204618+0000

MSH|^~\&|DEMO_LIS|GENERAL|||20261124204618+0000||ORU^R01|UI-DEMO-ORU-R01-02|P|2.5.1
PID|1||UI-DEMO-02^^^GENERAL^MR||Demo^Blake||19800101
PV1|1|I|WARD^101^A||||||||||||||||UI-DEMO-V02|||||||||||||||||||||||||20261124194618+0000
OBR|1||DEMO-LAB-2|2160-0^Creatinine^LN|||20261124204618+0000|||||||||PROVIDER123^Chen^Maya
OBX|1|NM|2160-0^Creatinine^LN||0.9|mg/dL|||||F|||20261124204618+0000

MSH|^~\&|DEMO_LIS|COMMUNITY|||20261123204618+0000||ORU^R01|UI-DEMO-ORU-R01-03|P|2.5.1
PID|1||UI-DEMO-03^^^COMMUNITY^MR||Demo^Casey||19800101
PV1|1|I|WARD^101^A||||||||||||||||UI-DEMO-V03|||||||||||||||||||||||||20261123194618+0000
OBR|1||DEMO-LAB-3|2160-0^Creatinine^LN|||20261123204618+0000|||||||||PROVIDER123^Chen^Maya
OBX|1|NM|2160-0^Creatinine^LN||2.1|mg/dL|||||F|||20261123204618+0000

MSH|^~\&|DEMO_RIS|GENERAL|||20261123214618+0000||ORM^O01|UI-DEMO-ORM-O01-04|P|2.5.1
PID|1||UI-DEMO-04^^^GENERAL^MR||Demo^Devon||19800101
PV1|1|O|WARD^101^A||||||||||||||||UI-DEMO-V04|||||||||||||||||||||||||20261122194618+0000
ORC|NW|DEMO-CT-1
OBR|1|DEMO-CT-1||IMG200^CT chest without contrast||||||||||||PROVIDER456^Patel^Anika

MSH|^~\&|DEMO_EHR|COMMUNITY|||20261124214618+0000||ADT^A03|UI-DEMO-ADT-A03-03|P|2.5.1
EVN|A03|20261124214618+0000
PID|1||UI-DEMO-03^^^COMMUNITY^MR||Demo^Casey||19800101
PV1|1|I|WARD^101^A||||||||||||||||UI-DEMO-V03|||||||||||||||||||||||||20261123194618+0000|20261124214618+0000
```

</details>

## Next steps for high-volume ingestion

### Workload and retention

The five demo fixtures are 221–377 bytes each. They contain just enough fields for the walkthrough.
An InterSystems benchmark reported an average of 1.2 KB for its ADT and ORU workload. A clinical data
warehouse study reported 411 MB across 343,091 HL7 v2 messages, also about 1.2 KB per message.
These are examples from specific feeds. Measure payload bytes by sender and message type and track
p50, p95, p99, and max, especially for ORU messages with multiple observations or embedded content.
[InterSystems HL7 benchmark](https://community.intersystems.com/post/intersystems-iris-health-2020-1-hl7-benchmark)
· [Majeed and Röhrig clinical data warehouse study](https://www.ukgm.de/ugm_2/deu/ugi_ana/PDF/Majeed_Roehrig_HL7_I2B2_Cell.pdf)

For this project, assume **1 million messages per day**, **120 messages per second in bursts**,
**2 KB per message on average** for planning, and **30 days of online raw-message retention**.
That is about 12 messages per second on average, 30 million retained messages, and **60 GB of raw
payloads** (decimal units). At a 5 KB average, the same window would hold 150 GB of raw payloads.
Measure SQLite table and index growth separately and budget disk space for the WAL and backups.
Retention is a design assumption. The current implementation keeps every accepted message.
Implement expiry with a clear policy for linked result and order records and the retry deduplication
window before enabling deletion.

### Real-time search on SQLite

Use **p95 under 1 second from receipt of a complete MLLP frame to visibility in a fresh search**
as the initial target at the assumed workload. Today the normalized rows and raw message commit in
the same transaction, so qualifying data can appear in a new search as soon as that commit succeeds.
SQLite WAL mode lets readers run during a write, with one writer at a time. [SQLite WAL documentation](https://www.sqlite.org/wal.html)

Preload a representative retained dataset, then drive the average rate and bursts through MLLP
while running admission and observation searches. Record p95 and p99 ACK latency, receipt-to-search
lag, query latency, write contention, and database and WAL growth. Include a historical-message
lookup by patient and date. Use the results to determine whether indexing,
shorter write transactions, or checkpoint tuning can keep SQLite within the target. The workload
and latency figures above are planning targets that still need a load test.

### Separate intake and indexing with a durable queue

If sustained write contention or backlog causes missed freshness targets after tuning, put a
durable queue between the MLLP receiver and the workers that build the search tables:

```text
MLLP receiver → durable queue → projection workers → search database → patient API
                         └───→ archive consumer → retained raw messages
```

1. Publish the raw payload and sender metadata with the existing deduplication key
   `(MSH-3, MSH-4, MSH-10)`. Keep the current `AA` contract by returning it only after application
   processing succeeds. If the sender agrees to enhanced acknowledgment mode, return an accept ACK
   after durable enqueue and send the application acknowledgment according to the agreed profile.
   [HL7 v2 acknowledgment rules](https://hl7.eu/HL7v2x/v282/std282/ch02.html)
2. Make projection and archive consumers idempotent because queue delivery may repeat. Commit each
   consumer's writes before marking the message processed. Preserve visit-level ordering where
   needed and handle late clinical events explicitly. Retry transient failures and route persistent
   failures to a dead-letter queue for investigation and replay.
3. Track the oldest queued message and receipt-to-search lag against the same freshness target.
   Keep a single projection writer while SQLite meets the measured workload. A queue can buffer
   bursts, but sustained throughput is still limited by the writer. Move the projection to
   PostgreSQL if that write limit becomes the bottleneck. Retain the raw archive independently so
   historical lookup and projection replay can use the same source messages.

## Correctness and tests

```bash
uv run pytest
uv run ruff check .
```

The suite covers delimiter parsing, ADT fields, ORU observations, ORM orders, deduplication,
different cohort filters, and a real TCP MLLP/ACK round trip.

## HL7 references

- [HL7 v2.9 Control: construction, delimiters, acknowledgements](https://hl7.eu/HL7v2x/v29/std29/ch02.html)
- [HL7 v2.5 ADT trigger-event index](https://www.hl7.eu/HL7v2x/v25/hl7v25msgtypADT.htm)
- [HL7 v2.4 orders: ORM O01](https://hl7.eu/HL7v2x/v24/std24/ch04.htm)
- [HL7 v2.5.1 specification contents: ORU R01 and OBX](https://hl7.eu/HL7v2x/v251/std251/hl7.html)

The standard requires CR (`0x0D`) segment terminators; LF in checked-in fixtures is accepted only for
developer ergonomics. The ACK echoes the incoming control ID in `MSA-2` while creating a new response
control ID, consistent with HL7 control-chapter guidance.
