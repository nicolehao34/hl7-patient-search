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
uv run hl7-search serve
```

The MLLP receiver listens on `0.0.0.0:2575`; the HTTP API and interactive docs are available at
`http://127.0.0.1:8000` and `http://127.0.0.1:8000/docs`. In another terminal, load the examples:

```bash
for message in examples/*.hl7; do uv run hl7-search send "$message"; done
```

Each send prints an HL7 ACK containing `MSA|AA`. Then try searches:

```bash
# Active inpatients admitted at GENERAL since midnight on Nov 25
curl 'http://127.0.0.1:8000/patients?facility=GENERAL&patient_class=I&status=active&admitted_since=2026-11-25T00:00:00'

# Patients whose LOINC 2160-0 (creatinine) is at least 1.5
curl 'http://127.0.0.1:8000/patients?observation_code=2160-0&observation_min=1.5'

# Patients with a CT chest order
curl 'http://127.0.0.1:8000/patients?order_code=IMG200'
```

These return Alice, Alice, and Carol respectively. Bob is excluded from the first query because he
was admitted at `COMMUNITY`; Carol is excluded because `PV1-2` marks her outpatient (`O`).

Alternatively, `docker compose up --build` starts the same service with a persistent volume.

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
| `ORU^R01` | unsolicited result | Stores each `OBX` code, value, units, status and observation time; enables code and numeric-threshold cohorts. |
| `ORM^O01` | general order | Stores `ORC`/`OBR` order identifiers and universal service code; enables ordered-procedure cohorts. |
| Other types | site-specific or unsupported | The raw message and envelope remain durable; recognized `PID`, `PV1`, `OBR`, and `OBX` segments are still projected conservatively. |

Patient identity is the pair `PID-3.1` + `PID-3.4` (ID and assigning authority), not name or date of
birth. Encounters are keyed by sending facility + `PV1-19`. Real health systems require cross-domain
identity resolution (MRN merges such as A40, enterprise identifiers, and site-specific CX selection);
that is deliberately identified as the next major feature rather than guessed here.

The parser honors the field and encoding delimiters declared by `MSH-1`/`MSH-2`, repeated patient
identifiers, common escape sequences, CR or LF segment endings, and decreasing-precision HL7
timestamps. It retains observation values as strings because `OBX-2` permits many data types; numeric
threshold search casts values only when explicitly requested.

## API

- `POST /messages` accepts raw HL7 as an HTTP convenience path (MLLP remains the hospital feed).
- `GET /patients` filters by `facility`, `admitted_since`, `admitted_before`, `status`,
  `patient_class`, `mrn`, `observation_code`, `observation_min`, `observation_max`, and `order_code`.
- `GET /patients/{id}` returns the patient with encounters, observations, and orders.
- `GET /health` reports readiness and row counts.

Search uses normalized SQL fields so filter criteria map directly to patient, encounter, observation,
and order data.

## Next steps for high-volume ingestion

### Workload and retention

The five demo fixtures are 221–347 bytes each. They contain just enough fields for the walkthrough.
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
lag, query latency, write contention, and database and WAL growth. Include a lookup by patient and
date once historical-message access is available. Use the results to determine whether indexing,
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
