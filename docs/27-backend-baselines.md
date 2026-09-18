# 27 — Backend Baselines (Firebase / GCP / non-Firebase)

Workstream F of the Angel V1 completion pass. This is a sourced evaluation of
the application-backend building blocks available to the software factory
(`docs/06-software-factory.md`), so that a spec written against
`templates/backend-profiles/` picks tools for reasons that are current as of
**2026-09-17/18**, not folk memory of 2022-era Firebase. Every quantitative
claim below is sourced; where sources disagree or a page is SEO-derivative of
Google's own docs, that is noted rather than silently resolved.

This document is the tracked canonical copy. `knowledge/` is gitignored
(`.gitignore`); an Angel export under `/Volumes/Angel/50-research/knowledge/backend-baselines/`
(written by `scripts/export-angel-knowledge`, deliverable 5) is a **copy**,
never the source of truth.

## How to read this document

For each service: what it is good for, limits/quotas/pricing model as of
2026, offline/realtime characteristics, security-rules model, multi-region/
consistency, vendor lock-in, emulator/test story, and the failure modes
agents most often hit. The **one backend does not fit every app** thesis is
made concrete in `templates/backend-profiles/`: four different application
classes land on visibly different service selections.

---

## 1. Firebase Authentication

**Good for:** email/password, phone, and federated (Google/Apple/etc.)
sign-in with minimal backend code; anonymous auth for try-before-signup
flows; custom claims for coarse authorization checked inside Security Rules.

**Limits/pricing (2026):** Firebase Authentication itself has no per-MAU
charge for standard providers; SMS-based phone auth is billed per verification
and is the line item that actually shows up on an invoice at scale. Multi-factor
auth and enterprise SSO (SAML/OIDC) require the **Identity Platform** upgrade
path, which is priced per monthly active user above a free allotment
[Firebase Pricing](https://firebase.google.com/pricing).

**Offline/realtime:** the SDK caches the signed-in user and ID token locally
and refreshes silently; there is no "offline sign-in as a new user" — first
auth requires connectivity.

**Security-rules model:** `request.auth.uid` / `request.auth.token.<claim>`
is the primitive every Firestore/RTDB/Storage rule keys off. Custom claims are
set server-side (Admin SDK / a Cloud Function) and only take effect on the
next token refresh — a common source of "I set the claim and the rule still
denies" bug reports.

**Multi-region/consistency:** global service; not a regional resource.

**Vendor lock-in:** moderate. UID format and the concept of custom claims are
Firebase-specific; most identity data (email, provider links) is exportable
via the Admin SDK `listUsers`/bulk-import format, but MFA enrollment and some
provider-linkage state does not migrate cleanly to another IdP.

**Emulator/test story:** the Auth Emulator (part of Local Emulator Suite,
below) reproduces sign-in, custom claims, and token verification locally with
no network calls, and composes automatically with the Firestore/RTDB/Functions
emulators so rules that read `request.auth` can be tested end-to-end
[Connect your app to the Authentication Emulator](https://firebase.google.com/docs/emulator-suite/connect_auth).

**Failure modes agents hit most:**
- Firebase Authentication (the branded product) is **not** on Google Cloud's
  HIPAA-covered-products list even though the underlying identity substrate
  overlaps with Identity Platform, which *is* covered — see §12. Agents
  building a health app default to `firebase-auth` and only discover the BAA
  gap during a compliance review.
- Trusting a stale ID token client-side after a custom-claim change instead
  of forcing `getIdToken(true)`.
- Treating anonymous-auth UIDs as stable identity for analytics without
  planning the anonymous→permanent account linking flow.

---

## 2. Cloud Firestore

**Good for:** the default structured-data store for new Firebase apps —
document/collection model, strong per-document consistency, native mobile
offline persistence, and Security Rules that can express per-document
ownership.

**Limits/pricing (2026):** Blaze (pay-as-you-go) free quota is roughly 1 GiB
stored, 50K reads/day, 20K writes/day, 20K deletes/day, 10 GiB/month egress.
Above that: **regional** locations bill around $0.03/100K reads (the
commonly cited $0.06/100K figure above is the *multi-region* US rate — see
below), $0.09–0.18/100K writes, $0.01–0.02/100K deletes, ~$0.18/GiB-month
stored; a **multi-region** location (e.g. `nam5`) runs roughly 50%+ higher on
reads than a single region for the extra availability
[Google Firebase Pricing Explained (2026)](https://blog.back4app.com/firebase-pricing/),
[Firestore pricing — Google Cloud](https://cloud.google.com/firestore/pricing).
**Pricing pages and third-party summaries disagree on exact figures across
2025/2026 revisions — verify against `cloud.google.com/firestore/pricing` at
implementation time, not this document.**

**Offline/realtime:** first-class. Mobile/web SDKs keep a local persistent
cache, queue writes while offline, and replay on reconnect with last-write-
wins per document (not per-field merge) unless the app structures documents
to avoid conflicting concurrent writes. `onSnapshot`/`addSnapshotListener`
gives realtime updates with millisecond-scale latency when online.

**Security-rules model:** declarative rules language evaluated per request,
keyed on `request.auth`, `resource.data`, and `request.resource.data`; rules
cannot fan out to check *other* documents cheaply (each `get()`/`exists()`
call inside a rule is a billed read and adds latency), so deep
authorization graphs push agents toward denormalizing ownership fields onto
every document instead of chasing joins in rules.

**Multi-region/consistency:** both regional and multi-region locations give
strong consistency for reads (always the latest committed write); multi-
region raises the SLA from four nines to 99.999% at a cost premium
[Cloud Firestore locations](https://firebase.google.com/docs/firestore/locations).
Location is chosen once at project creation and cannot be changed in place.

**Vendor lock-in:** high for the query engine (no arbitrary joins, no
`OR` across fields without composite indexes or client-side merge,
composite-index management is Firestore-specific), low for the underlying
data (documents export to JSON via `gcloud firestore export` /
`google-cloud-firestore` client libraries).

**Emulator/test story:** first-tier emulator with an import/export dataset
format, a Rules unit-testing library (`@firebase/rules-unit-testing`), and a
local Emulator UI for inspecting data during test runs.

**Failure modes agents hit most:**
- Designing a relational schema and fighting Firestore's lack of joins/
  transactions-across-collections instead of denormalizing.
- Unbounded collection-group queries that blow past the free read quota in
  development because listeners re-fetch on every hot-reload.
- Composite-index requirements only surface at first query time in
  production (the emulator does not always require the same indexes),
  producing a runtime `FAILED_PRECONDITION` an agent didn't provision for.
- Picking multi-region by default "for safety" without pricing the read/
  write premium against the app's actual availability requirement.

---

## 3. Realtime Database (RTDB)

**Good for:** the original Firebase database — a single large JSON tree with
very low-latency presence/typing-indicator-style realtime sync. Still the
better fit than Firestore for extremely high-frequency, low-value ephemeral
state (cursor positions, presence, live counters) because of lower per-op
overhead and native `onDisconnect()` semantics.

**Limits/pricing (2026):** free tier ~1 GB stored / 10 GB downloaded per
month; Blaze bills roughly $5/GB-month stored and $1/GB downloaded
[Google Firebase Pricing Explained (2026)](https://blog.back4app.com/firebase-pricing/).
No per-operation billing the way Firestore has per-read/write pricing — cost
scales with data size and bandwidth, which makes RTDB cheaper than Firestore
for chatty small-payload realtime traffic and more expensive for large
documents.

**Offline/realtime:** the original offline-first realtime sync engine;
`onDisconnect()` lets the server clean up presence state when a client drops
without an explicit close — the standard building block for "who's online"
and multiplayer presence.

**Security-rules model:** an older, path-based rules language (`.read`/
`.write`/`.validate` per path) that is less expressive than Firestore Rules
and cannot do collection-group-style rules; still supports `auth` variables.

**Multi-region/consistency:** single-region per database instance (you can
create multiple RTDB instances in different regions for sharding, but there
is no built-in multi-region replication the way Firestore has). Strong
consistency within one instance.

**Vendor lock-in:** high. The single-JSON-tree data model and path-based
rules do not map cleanly to any non-Firebase product; migrating means a data
model rewrite, not just a data export.

**Emulator/test story:** covered by the Local Emulator Suite alongside
Firestore/Auth/Functions.

**Failure modes agents hit most:**
- Defaulting to Firestore for everything and missing that RTDB is the
  better/cheaper choice for presence and high-frequency ephemeral multiplayer
  state (see `templates/backend-profiles/multiplayer-game.yaml`).
- Deeply nested JSON trees that make security rules and partial reads
  expensive — RTDB rewards flat, denormalized structures even more than
  Firestore does.
- **RTDB is not on the HIPAA covered-products list** (§12) — do not put PHI
  in it.

---

## 4. Cloud Functions (2nd gen)

**Good for:** event-driven backend logic (Firestore/RTDB/Auth/Storage
triggers, HTTPS callables, Pub/Sub subscribers, scheduled jobs) without
managing a server. 2nd gen functions run on Cloud Run under the hood, so they
get Cloud Run's concurrency (multiple requests per instance), longer request
timeouts, and larger instance sizes than 1st gen ever had.

**Limits/pricing (2026):** $0.40 per million invocations, ~$0.0000025/GB-sec
memory, ~$0.000024/vCPU-sec compute, plus egress ~$0.12/GB beyond a free
tier [Google Firebase Pricing Explained (2026)](https://blog.back4app.com/firebase-pricing/).
Because 2nd gen is Cloud Run underneath, its actual quota/limit documentation
(max instances, concurrency, timeout up to 60 min for background functions
vs. shorter for HTTPS) lives on the Cloud Run pricing/quotas pages — verify
current values there before sizing a design.

**Offline/realtime:** not applicable — Functions are server-side and always
require connectivity to invoke; they are how an offline-first client's queued
writes get server-side side effects (e.g., sending a push notification) once
sync completes.

**Security-rules model:** N/A directly, but Functions are frequently the
escape hatch for authorization logic too complex for Firestore/RTDB Rules —
an HTTPS callable that re-validates ownership server-side before a
privileged write.

**Multi-region/consistency:** deployed per-region; cold-start and
multi-region failover are the operator's responsibility (no automatic
multi-region Functions).

**Vendor lock-in:** moderate — 2nd gen functions are close enough to plain
Cloud Run + a thin trigger adapter that a determined team can port the
business logic to Cloud Run directly with the Functions Framework, but the
Firebase-specific trigger bindings (`onDocumentWritten`, `onCall`, etc.) are
Firebase-only glue code.

**Emulator/test story:** the Functions Emulator runs real Node/Python
function code locally and can trigger against the local Firestore/RTDB/Auth
emulators, giving a full-stack local dev loop.

**Failure modes agents hit most:**
- Writing a Firestore-triggered function that itself writes back to the
  triggering document/collection without a guard, causing an infinite
  trigger loop that is invisible until the bill arrives.
- Not accounting for cold starts in min-instance-0 configurations for
  latency-sensitive HTTPS callables (mobile app launch paths).
- Conflating 1st-gen and 2nd-gen quota/behavior — most current docs and
  blog posts describe 2nd gen, but older Stack Overflow answers and cached
  agent knowledge describe 1st gen limits (9-minute max timeout, no
  concurrency) that no longer apply.

---

## 5. Cloud Run

**Good for:** any stateless containerized backend that needs more control
than Cloud Functions — custom runtimes, long-lived WebSocket/gRPC
connections (with session affinity), authoritative game-server logic, or a
non-Firebase backend (e.g., a self-hosted Postgres API) that still wants
serverless scale-to-zero economics.

**Limits/pricing (2026):** pay-per-use CPU/memory/request billing with
scale-to-zero; Cloud Run **jobs** support run-to-completion batch work;
Cloud Run now also hosts GPU-backed services for inference workloads. Verify
current vCPU-second/GiB-second rates at `cloud.google.com/run/pricing` — they
move more often than Firestore's.

**Offline/realtime:** N/A (server-side); supports WebSockets and HTTP/2 with
configurable session affinity, which is what makes it viable for real-time
game-server or chat backends when Firestore/RTDB realtime isn't authoritative
enough (see `multiplayer-game.yaml`).

**Security-rules model:** none built in — auth/authorization is whatever the
container implements (commonly: verify a Firebase ID token or App Check
token on each request using the Admin SDK).

**Multi-region/consistency:** deploy per region; multi-region requires a
load balancer in front of multiple regional Cloud Run services — not
automatic.

**Vendor lock-in:** low. Cloud Run runs an arbitrary container; the same
image is portable to any container platform (Kubernetes, another cloud's
container service) with minimal changes, which is why it's the piece of this
stack most agents reach for when they want an explicit low-lock-in escape
hatch from Firebase-specific products.

**Emulator/test story:** no dedicated "emulator" — local dev is `docker run`
or `cloud-run-proxy`/`gcloud run deploy --local` style workflows; the
container's own test suite is the actual test story.

**Failure modes agents hit most:**
- Assuming Cloud Run sessions are sticky/stateful by default — they are
  stateless and can be recycled between requests unless session affinity is
  explicitly configured, which breaks naive in-memory game-server state.
- Underestimating cold-start latency for a min-instances=0 service used as a
  synchronous API in a mobile app's critical path.

---

## 6. Cloud Storage for Firebase

**Good for:** user-uploaded media (photos, video, PDFs), backed by Google
Cloud Storage buckets with a Firebase-flavored client SDK (resumable
uploads, download URLs, and Security Rules keyed on `request.auth`).

**Limits/pricing (2026):** free tier ~5 GB stored, then ~$0.026/GB stored,
~$0.12/GB downloaded, small per-operation charges for uploads/downloads
[Google Firebase Pricing Explained (2026)](https://blog.back4app.com/firebase-pricing/).
Storage-class tiering (Standard/Nearline/Coldline/Archive) and lifecycle
rules are inherited from underlying GCS and can meaningfully cut cost for
write-once/rarely-read media (video archives, backups).

**Offline/realtime:** resumable uploads survive connectivity drops; there is
no offline *read* cache built into the SDK the way Firestore has — an app
that needs offline media access must cache files itself (e.g., to local
disk) after first download.

**Security-rules model:** Storage Security Rules, syntactically similar to
Firestore Rules but evaluated per-object-path, commonly gated on
`request.auth.uid` matching a path segment (`/users/{uid}/...`).

**Multi-region/consistency:** inherits GCS bucket location choices
(region/dual-region/multi-region); strongly consistent for object reads
after write.

**Vendor lock-in:** low for the bytes (it's a GCS bucket; portable via
`gsutil`/any S3-compatible tooling with adapters), moderate for the
Rules/SDK integration layer.

**Emulator/test story:** covered by the Local Emulator Suite.

**Failure modes agents hit most:**
- Public-read buckets left open because a rule was written permissively
  during prototyping and never tightened before shipping.
- No App Check enforcement on Storage, so a leaked API key lets a scripted
  client run up storage/egress costs.

---

## 7. App Check

**Good for:** attesting that requests to Firebase backends (Firestore, RTDB,
Functions, Storage, Data Connect/SQL Connect) come from your genuine app
binary, not a scraped API key replayed from a script — mitigates abuse/
billing-drain attacks more than it does authentication.

**Limits/pricing (2026):** App Check itself is free; its default
attestation provider on web is reCAPTCHA Enterprise, which is no-cost for
the first 10,000 assessments/month and billed per assessment above that
[Get started using App Check with reCAPTCHA Enterprise](https://firebase.google.com/docs/app-check/web/recaptcha-enterprise-provider).
Mobile platforms use device-attestation providers (Play Integrity on
Android, App Attest/DeviceCheck on iOS) that don't carry the reCAPTCHA
per-assessment charge. Token TTL is configurable and directly controls
assessment frequency (default refresh roughly twice per hour).

**Offline/realtime:** App Check tokens are cached client-side between
refreshes so a briefly offline client isn't immediately locked out.

**Security-rules model:** rules can require `request.auth != null &&
firebase.app_check_available` style checks in newer rule syntax, or
enforcement can be turned on per-product (Firestore/Storage/RTDB/Functions/
Data Connect each have their own App Check enforcement toggle) so it fails
closed once enabled.

**Vendor lock-in:** moderate — the concept (attestation) is portable, but
the provider wiring is Firebase/Play/Apple specific.

**Emulator/test story:** App Check has a debug-token mode specifically for
local development and CI so emulator-suite tests and debug builds aren't
blocked by attestation.

**Failure modes agents hit most:**
- Enabling enforcement in production without first shipping a client build
  that actually sends App Check tokens — instantly breaks all traffic.
- Leaving App Check in "unenforced/monitor" mode indefinitely because
  enabling enforcement feels risky, so it never actually stops abuse.

---

## 8. Firebase Cloud Messaging (FCM)

**Good for:** push notifications to iOS/Android/web clients — the standard
mechanism for "streak reminder", "your shift starts in 30 minutes",
"someone challenged you" style nudges across the four profiles in this
workstream.

**Limits/pricing (2026):** FCM sending is free; there is no meaningful
message-volume quota for normal app usage. **The legacy HTTP/XMPP FCM APIs
were fully discontinued June 2024** — any code sample or agent-generated
snippet using the legacy server-key HTTP API is dead on arrival in 2026 and
must use the **HTTP v1 API** with OAuth2/service-account credentials instead
[FCM Deprecation guide](https://customer.io/learn/mobile-marketing/fcm-deprecation).

**Offline/realtime:** messages queue at Google's infrastructure and deliver
when the device reconnects; delivery is best-effort, not guaranteed/ordered.

**Security-rules model:** N/A — server-side send requires a service-account
credential (Admin SDK) or an authenticated HTTP v1 call; client-side receipt
requires a registration token tied to app install + (optionally) a user.

**Multi-region/consistency:** global delivery infrastructure; not a
regional resource.

**Vendor lock-in:** low-moderate — APNs/FCM token registration is inherent
to iOS/Android push regardless of provider; swapping FCM for another push
aggregator (OneSignal, etc.) mainly means re-registering tokens and moving
send-side code.

**Emulator/test story:** no true local emulator; the Local Emulator Suite
does not emulate FCM delivery. Testing means a real device/simulator plus a
non-production Firebase project, or mocking the Admin SDK send call.

**Failure modes agents hit most:**
- Generating legacy-API send code from stale training data (see above) —
  this is the single most common "worked in the tutorial, dead in
  production" FCM failure in 2026.
- Not handling token rotation/invalidation, so a meaningful fraction of
  sends silently fail against stale tokens over time.

---

## 9. Firebase Local Emulator Suite

**Good for:** the offline/CI test story for the entire Firebase stack in one
command (`firebase emulators:start`) — Firestore, Realtime Database,
Storage, Auth, Functions, Hosting, Pub/Sub, and Extensions all run locally
with an inspectable Emulator UI at `localhost:4000`
[Firebase Local Emulator Suite docs](https://firebase.google.com/docs/emulator-suite).
Auth + Functions + Firestore/RTDB interop automatically (a Functions trigger
fires against local emulator writes, an Auth-emulated user's claims are
visible to local Security Rules) with no extra wiring.

**Limits:** the emulators are approximations, not bit-identical
reimplementations — Rules evaluation, index-requirement enforcement, and
some quota/error behaviors can diverge from production (a query that needs a
composite index in production may silently work in the emulator). Data
Connect/SQL Connect and FCM are **not** meaningfully covered by the suite
(Data Connect has its own local Postgres-backed toolkit; FCM has no send
emulation at all).

**Failure modes agents hit most:**
- Treating "passes against the emulator" as equivalent to "will pass in
  production," particularly for composite-index requirements and quota
  edges.
- Forgetting to reset/clear emulator data between test runs (the Firestore
  emulator exposes a REST endpoint for this specifically because it's easy
  to forget).

---

## 10. Firebase Data Connect / SQL Connect (Cloud SQL PostgreSQL)

**Good for:** teams that want a real relational schema, SQL joins,
transactions across tables, and generated typed client SDKs, without giving
up Firebase Auth/App Check integration — the antidote to Firestore's
no-joins model when the data is genuinely relational (e.g., the nursing
shift-scheduling profile's hospital/shift/employee relationships).

**Status as of 2026 (important, and easy to get wrong):** Data Connect
reached **general availability in April 2025**
[Data Connect is now generally available!](https://firebase.blog/posts/2025/04/dataconnect-general-availability/),
and at Google Cloud Next 2026 Google announced its evolution into
**Firebase SQL Connect**, adding native SQL support (write plain SQL, not
only GraphQL-style queries/mutations) alongside realtime sync and an offline
cache layer on top of the same underlying Cloud SQL PostgreSQL instance
[Realtime PostgreSQL: From Data Connect to SQL Connect](https://firebase.blog/posts/2026/04/whats-new-sql-connect/).
Treat "Data Connect" and "SQL Connect" as the same underlying product across
this naming transition; check `firebase.google.com/docs/sql-connect` for the
current name/docs URL at implementation time.

**Limits/pricing:** two billable components — the Data Connect/SQL Connect
service itself, and the Cloud SQL PostgreSQL instance underneath it. Pricing
was free during public preview; **permanent costs apply post-GA**, with
real-world minimums cited around **$9.37/month** once the always-on Cloud
SQL instance is counted, following standard Cloud SQL instance pricing (not
a Firestore-style per-operation model)
[Data Connect Pricing](https://firebase.blog/posts/2024/11/dataconnect-pricing/).
This is the one product on this list with a **non-zero cost floor even at
zero usage**, because there is always a Cloud SQL instance running — plan
prototype/hobby-tier apps accordingly.

**Offline/realtime:** SQL Connect specifically adds realtime query
subscriptions and an offline cache on top of Postgres, closing much of the
gap with Firestore's offline story while keeping a relational schema.

**Security-rules model:** not Firestore-style declarative Rules — access
control is expressed through the generated GraphQL/SQL operations plus
Firebase Auth-gated request context; fine-grained row-level policy is closer
to traditional Postgres row-level security than to Firestore Rules.

**Multi-region/consistency:** inherits Cloud SQL's regional model — a single
primary instance per region with optional read replicas; not natively
multi-region the way Firestore is.

**Vendor lock-in:** low for the data (it's plain PostgreSQL — `pg_dump`
works), moderate for the generated-SDK/schema-definition layer.

**Emulator/test story:** ships its own local Postgres-backed emulator/dev
toolkit distinct from the Local Emulator Suite (VS Code extension +
`firebase emulators` integration exists but the local dev loop is centered
on the Data Connect toolkit and schema files, not the classic suite).

**Failure modes agents hit most:**
- Assuming a $0 idle cost the way Firestore's free tier behaves — the Cloud
  SQL instance bills whether or not the app has any traffic.
- Generating code against the older "Data Connect" GraphQL-only mental model
  when the product has since added native SQL — verify current docs rather
  than reusing a 2025-era code sample verbatim.

---

## 11. Other relevant GCP services (non-Firebase-branded)

These sit alongside Firebase for workloads Firebase's own products don't
cover well.

- **Cloud SQL** (standalone, outside Data Connect): the general-purpose
  managed Postgres/MySQL/SQL Server option when an app wants full SQL
  control without the Data Connect schema/codegen layer — e.g., a
  self-hosted-style backend running on Cloud Run that talks to Cloud SQL
  directly via a standard ORM.
- **Memorystore** (managed Redis/Memcached): session state, rate limiting,
  matchmaking queues, leaderboards needing sub-millisecond reads — the
  natural fit for the multiplayer-game profile's matchmaking and live
  leaderboard state, which neither Firestore nor RTDB serves as cheaply at
  high read/write frequency.
- **Pub/Sub**: durable, at-least-once pub/sub messaging for decoupling
  services (e.g., a Cloud Run game-server fleet publishing match-result
  events for a Cloud Function to fan out). Pricing is throughput-based
  (~$40/TiB after a 10 GiB/month free tier) plus storage
  [Pub/Sub pricing](https://cloud.google.com/pubsub/pricing). Note: **Pub/Sub
  Lite is being shut down in 2026** (shutdown window extended from March 18
  to June 30, 2026, per search results as of this writing) — do not design
  around Lite; use standard Pub/Sub.
- **Cloud Tasks / Cloud Scheduler**: Cloud Tasks for reliable, rate-limited,
  retryable async work dispatched to an HTTP endpoint (e.g., Cloud Run);
  Cloud Scheduler for cron-style recurring triggers (the streak-reset job in
  the behavior-change profile, the shift-reminder job in the nursing
  profile). Both are billed per job/task rather than by data volume.
- **BigQuery (Firestore/Analytics export)**: Firestore supports a managed
  BigQuery export of collections for analytics/SQL reporting without
  querying production Firestore directly; Google Analytics for Firebase
  also exports raw events to BigQuery on the Blaze plan. This is the
  standard path when a product needs ad hoc analytics SQL that Firestore's
  query model can't express.

---

## 12. HIPAA / BAA posture — read this before touching PHI

Google's own **Covered Products** list under the Google Cloud HIPAA BAA (as
observed 2026-05-20 per search results) includes **Identity Platform**,
**Firestore**, **Cloud Storage**, and **Cloud Run** (including Cloud Run
functions, i.e., 2nd-gen Cloud Functions) among the covered/relevant items
for this workstream's services; it does **not** include Firebase
Authentication (the free/branded product, as distinct from Identity
Platform), Realtime Database, Firebase Hosting, Crashlytics, or Analytics for
Firebase
[HIPAA Compliance on Google Cloud](https://cloud.google.com/security/compliance/hipaa),
cross-checked against secondary summaries
[Is Firebase HIPAA Compliant? BAA, Covered Services](https://www.accountablehq.com/post/is-firebase-hipaa-compliant-baa-covered-services-and-how-to-use-it-safely).

**Practical consequence used in `nursing-shift-productivity.yaml`:** a HIPAA-
posture app must (a) sign a BAA with Google Cloud (not merely enable
Firebase), (b) use **Identity Platform** rather than plain Firebase Auth for
any flow touching PHI-adjacent identity, (c) keep PHI in **Firestore** and/or
**Cloud Storage** and **not** in Realtime Database, and (d) route
PHI-touching compute through **Cloud Run** rather than assuming every
Firebase-branded surface is automatically covered just because Firestore is.
**Verify the current Covered Products list directly at
`cloud.google.com/security/compliance/hipaa` before any real PHI design** —
this list changes without much fanfare and this document's snapshot is dated.

---

## 13. Non-Firebase comparison point: Supabase

Included because the brief above is explicit that one backend does not fit
every app, and Supabase is the most direct open-source-friendly alternative
to the Firebase/GCP stack for a team that wants Postgres-native semantics
and a lower/more predictable cost floor than Data Connect's always-on Cloud
SQL instance, or that wants an eventual self-host/exit path Firebase does
not offer.

**Good for:** apps that are relational-first from day one (proper joins,
foreign keys, transactions), want row-level security expressed in plain
Postgres RLS policies instead of a bespoke rules language, and want a
realistic self-host exit path (Supabase is Postgres + a set of open-source
services; the whole stack can, in principle, be run outside Supabase's
managed offering).

**Pricing (2026):** Free plan: 500 MB database, up to 50,000 MAUs (auth is
priced by monthly active users, not gated by feature), unlimited API
requests. Pro: **$25/month** per project, bundling Auth + Storage + Edge
Functions + Realtime + Postgres, with a $10/month compute credit covering
one Micro compute instance — most early apps land right at $25/month until
they cross 100K MAU or need bigger compute. Team: **$599/month** (adds
SOC 2/ISO 27001, 14-day backup retention, priority support). Typical
production Pro-tier cost in the wild: **$25–$130/month**
[Supabase Pricing in 2026 (UI Bakery)](https://uibakery.io/blog/supabase-pricing),
[Supabase Pricing 2026 (Makerkit)](https://makerkit.dev/blog/saas/supabase-pricing).

**Offline/realtime:** Supabase Realtime streams Postgres changes
(logical-replication-based) to subscribed clients — comparable in spirit to
Firestore's `onSnapshot` but layered on top of a real relational schema
rather than being native to the document model; official offline-first
client caching is less mature/batteries-included than Firestore's mobile
SDKs, which is the main reason Firestore still wins for offline-heavy mobile
apps (see the nursing-shift profile).

**Security-rules model:** plain PostgreSQL Row-Level Security (RLS)
policies — more expressive and more portable (standard SQL) than Firestore
Rules, at the cost of needing real SQL/RLS competence on the team.

**Multi-region/consistency:** single-primary Postgres per project by
default (read replicas available on higher tiers); not natively
multi-region the way Firestore is — this is Supabase's clearest
disadvantage against Firestore for a globally distributed low-latency app.

**Vendor lock-in:** low. Postgres is the actual database; `pg_dump` and
standard Postgres tooling work, and the open-source components (GoTrue for
auth, Realtime, Storage API) can theoretically be self-hosted, though the
managed platform's operational conveniences (backups, connection pooling,
dashboard) are what most teams actually pay for.

**Emulator/test story:** the Supabase CLI runs a full local stack via Docker
(Postgres + Auth + Storage + Realtime + Studio), arguably a more complete
*local* dev environment than Firebase's suite since it's the *same* Postgres
engine locally and in production, not an approximation.

**Failure modes agents hit most:**
- Assuming Supabase Realtime/offline behaves like Firestore's mobile offline
  cache and shipping a mobile app with sync bugs under flaky connectivity.
- Underestimating RLS policy complexity — a team without real SQL/Postgres
  experience will write RLS policies as slowly and error-pronely as a team
  without Firestore-Rules experience writes those.
- Treating "self-hostable in principle" as "already self-hosted" and
  building on the assumption of infinite portability without ever testing an
  actual self-host migration.

**When to reach for a self-hosted Postgres+auth stack instead of either:**
regulatory/data-residency requirements neither Firebase's project-wide
location choice nor Supabase's managed regions satisfy, or an existing
internal Postgres investment/DBA team. This repository's own default
posture (`docs/06-software-factory.md`) is to prefer managed services for a
V1 unless a concrete constraint forces self-hosting — self-hosted Postgres+
auth is noted here as the honest fourth option, not adopted as a default.

---

## 14. Summary decision table

| Need | Default pick | Reach for instead when |
|---|---|---|
| Mobile-first offline-capable structured data | Firestore | data is genuinely relational and joins matter → Data Connect/SQL Connect or Supabase |
| Extremely high-frequency small realtime state (presence, cursors) | Realtime Database | payloads are large/structured → Firestore listeners |
| Event-driven backend glue | Cloud Functions (2nd gen) | long-lived connections / custom runtime / authoritative game server → Cloud Run |
| User media | Cloud Storage for Firebase | — |
| Abuse/billing-drain mitigation | App Check | — |
| Push notifications | FCM (HTTP v1 only) | — |
| Local/offline test loop | Local Emulator Suite | Data Connect/SQL Connect (own toolkit) |
| Relational schema, joins, transactions | Data Connect/SQL Connect | cost-sensitive hobby tier / need self-host exit → Supabase |
| Matchmaking, leaderboards, rate limiting | Memorystore (Redis) | — |
| Async decoupled events | Pub/Sub | simple retryable HTTP dispatch → Cloud Tasks |
| Cron-style jobs | Cloud Scheduler | — |
| SQL analytics over product data | BigQuery export | — |
| PHI-bearing identity/data | Identity Platform + Firestore/Cloud Storage + Cloud Run, under a signed BAA | never Firebase Auth or RTDB for PHI |
| Full relational control + self-host optionality | Supabase or self-hosted Postgres+auth | — |

## References (accessed 2026-09-17/18)

- [Firebase Pricing — Google](https://firebase.google.com/pricing)
- [Google Firebase Pricing Explained (2026) — back4app](https://blog.back4app.com/firebase-pricing/)
- [Firestore pricing — Google Cloud](https://cloud.google.com/firestore/pricing)
- [Cloud Firestore locations — Firebase](https://firebase.google.com/docs/firestore/locations)
- [HIPAA Compliance on Google Cloud](https://cloud.google.com/security/compliance/hipaa)
- [Is Firebase HIPAA Compliant? BAA, Covered Services, and How to Use It Safely — Accountable](https://www.accountablehq.com/post/is-firebase-hipaa-compliant-baa-covered-services-and-how-to-use-it-safely)
- [Data Connect is now generally available! — Firebase Blog](https://firebase.blog/posts/2025/04/dataconnect-general-availability/)
- [Data Connect Pricing — Firebase Blog](https://firebase.blog/posts/2024/11/dataconnect-pricing/)
- [Realtime PostgreSQL: From Data Connect to SQL Connect — Firebase Blog](https://firebase.blog/posts/2026/04/whats-new-sql-connect/)
- [Get started using App Check with reCAPTCHA Enterprise — Firebase](https://firebase.google.com/docs/app-check/web/recaptcha-enterprise-provider)
- [FCM Deprecation: Your Guide to Navigating the Change — Customer.io](https://customer.io/learn/mobile-marketing/fcm-deprecation)
- [Firebase Local Emulator Suite — Firebase docs](https://firebase.google.com/docs/emulator-suite)
- [Connect your app to the Authentication Emulator — Firebase docs](https://firebase.google.com/docs/emulator-suite/connect_auth)
- [Supabase Pricing in 2026 — UI Bakery](https://uibakery.io/blog/supabase-pricing)
- [Supabase Pricing in 2026: What You'll Actually Pay — Makerkit](https://makerkit.dev/blog/saas/supabase-pricing)
- [Pub/Sub pricing — Google Cloud](https://cloud.google.com/pubsub/pricing)
- [Agones moves to the CNCF (2026-03-23) — CNCF blog](https://www.cncf.io/blog/2026/03/23/agones-moves-to-the-cncf-a-new-era-for-open-source-multiplayer-game-infrastructure/)
- [Agones — GoogleForGames GitHub](https://github.com/googleforgames/agones)

**Caveat on sourcing:** several of the above (pricing figures especially)
were cross-checked against SEO/aggregator sites rather than only
`cloud.google.com`/`firebase.google.com` primary pages, because several
primary pricing pages were not directly fetched during this pass. Treat
every dollar figure in this document as **directional, not contractual** —
re-verify against the primary pricing page immediately before it drives a
budget decision.
