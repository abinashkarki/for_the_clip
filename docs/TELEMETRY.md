# Local system history

Clippie records explicit OpenTelemetry workflow spans into a separate local `data/telemetry.sqlite3`. There is no remote exporter, collector, account, background upload, or HTTP auto-instrumentation. The **System** button opens operational history outside the Create flow. The application database and saved production documents remain authoritative.

Each new attempt has its own run ID and trace ID. Replans, render retries and source-selection retries point to the previous attempt, with production/source IDs and saved timeline revision. Planning and automatic rendering share one run, including the queue wait. Manual renders create a new attempt. Existing productions and reviews are **not backfilled**: historical processing and authorship were not measured reliably.

The collection workflow adds `collection_prepare` and `collection_generate` workflow runs and `source_evidence` stages with collection/task/source IDs, attempt number and evidence cache use. A workflow run can contain several durable task attempts; their authoritative leases, attempt history and conservative usage ledger live in `collections.sqlite3`, not telemetry. Safe short/revision/attempt IDs and reserved/charged counters are allowlisted for later stages. Generation is connected, while provider/editorial success still requires actual run evidence. These runs retain user/QA/synthetic separation and do not change existing video-ready or human-review denominators.

## Coverage

- Source ingest, preview preparation, transcription and clip selection.
- Suggestions, recommendation cache, optional local/cloud brief writing, defaults version and the selected recipe version.
- Source indexing and cache reuse, retrieval, planning, supporting-shot selection and rendering.
- Local model calls, bounded repair attempts, tokens, model/revision/runtime, output limit, finish reason, generation speed and model-reported MLX peak memory. An isolated Qwen subprocess receives only a W3C trace parent for correlation and returns its child span; private prompt/response artifacts remain separate.
- Gemini provider model/version, token counters including cached and reasoning tokens, safe HTTP status and classified provider errors when actually reported. There is no estimated cloud price or invented cost.
- Meaningful UI actions: recommendation field overrides (once per field per suggestion), alternative/reset choices, recommendation use, actual video `playing` (once per revision per page session), and download **clicks**. These are intent/usage observations, not evidence of a completed save, completed viewing, or editorial acceptance.
- Saved timeline revisions report changed primary/picture clips and framing/caption changes. Explicit keep/revise/reject reviews are counted per production revision, taking the latest decision for that revision. Review notes are never copied into telemetry.

State polls, source/production media Range requests, metadata preloads and health checks are not traced. Failed modifying requests produce only an HTTP status event. The System page does not generate usage events from its own refreshes.

## Reading the numbers

The default group is **Your activity**. QA and synthetic activity have separate filters. API evaluations must send `X-Clippie-Observation: qa`; browser QA may set `sessionStorage.setItem('clippie-observation', 'qa')` before reloading. This explicit tag follows the queued worker context. Synthetic demos and productions using synthetic sources are labeled synthetic even without that header. No automated test is treated as a human keep decision.

Time to ready is calculated only for successful Create/replan attempts with automatic rendering; queue wait is included. The ready count and total completed video-attempt denominator are shown. Performance P50/P95 use successful samples only, with attempt and success counts. Failed/interrupted counts include all completed workflow attempts, not just video rendering. Warm/cold/mixed describe cache reuse/build **within that workflow**, not a promise that model weights were already loaded. Model subprocess loading is part of the measured model stage. Unreviewed revisions contribute no keep/revise/reject decision.

MLX peak memory is the model library's reported figure, not total process or machine RAM. Model token usage may be absent for a failed request. Source ingestion is a separate run from later production creation, so time to ready does not include an earlier import/transcription or time the user spent filling the form. Suggestion-use events link their recommendation run and production attempt. Recipe and model versions are metadata; they do not establish creative quality or factual accuracy.

## Privacy and failure behavior

Attributes pass a strict allowlist both before OTel and at the SQLite boundary. Telemetry excludes keys, source URLs/paths/titles, transcripts, prompts, model text, search queries, briefs, custom genre text, review notes, traceback text and exception messages. It stores fixed error codes/types instead. Internal diagnostic IDs can link to existing local model-run artifacts; telemetry contains no path to those artifacts. The existing app's private production manifests and diagnostic artifacts have their own content and retention behavior.

A daemon writer consumes a bounded queue of 1,024 metadata records with nonblocking inserts. Storage errors or queue overflow drop metadata and increment session diagnostics instead of stopping video work. The System page exposes pending, dropped, storage/read/recorder error counts. Counters reset on restart; they are not durable crash-loss estimates. Hard process termination may lose pending records. Surviving queued/running runs from an earlier server session become **interrupted** on startup; no inference is automatically restarted.

Metadata retention is 30 days, with caps of 5,000 completed runs, 20,000 events and 50,000 completed spans, maintained at startup and every 200 written records. Active attempts are retained. Maintenance removes associated completed orphan spans; it never deletes media, models, production documents, revisions, render files or private model diagnostics. SQLite can reuse freed pages; physical file size need not immediately shrink. Very high activity can reach caps sooner than 30 days. The System view explicitly flags sample limits.

The app drains telemetry briefly at shutdown and signals its writer to stop; production never waits indefinitely for telemetry. Because telemetry is best effort, use production state and saved artifacts when diagnosing a missing observation.

## Development

`clippie.telemetry.stage(name, **allowlisted_metadata)` creates a nested stage inside the active workflow. `telemetry.submit` carries a run across the shared worker queue. `telemetry.operation` wraps synchronous suggestions/search. Do not put source content or arbitrary error strings in metadata. Do not enable automatic exception recording or generic HTTP instrumentation without revisiting the privacy boundary.

Read-only endpoints: `GET /api/system/summary?observation=user|qa|synthetic|all&days=1..30` and `GET /api/system/runs/{id}`. `POST /api/system/events` accepts a small typed interaction schema with an idempotency event ID. It rejects arbitrary event names/attributes. Playback/download events must match the current rendered revision.

Tests cover success/failure/replan/render correlation, queue time, thread and subprocess context, privacy, event validation/idempotency, review denominators, restart interruption, retention, corrupt attribute JSON, storage/queue failures and tracer failures. Browser checks cover desktop/mobile System navigation and traces.

Implementation uses the official [OpenTelemetry Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/) and [exporter](https://opentelemetry.io/docs/languages/python/exporters/) APIs with a private local provider, rather than globally installing exporters.


## Collection usage summaries

Collection detail and recording-queue summaries read reported calls/tokens and unreported-attempt counts from persisted paid attempts in `collections.sqlite3`, separately from reservation caps/charges. These diagnostic summaries do not depend on retained telemetry and include cancelled, retried and replaced-run attempts plus late reports. Known zero is distinct from missing usage; cost stays unknown without pricing. Each child is read in a read-only snapshot; a queue total is not a group-atomic accounting decision. See the [collection contract](COLLECTION_CONTRACT.md) and [usage verification](../output/qa/COLLECTION_USAGE_QA.md). No new remote exporter or source-content telemetry was added.
