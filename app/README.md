# Petfolk Monday Digest — web app

React front-end + Node API for the Monday digest. The Python pipeline
(`../pipeline/`) does all the thinking; this app runs it, serves its output,
and renders it. **Neither the API nor the UI computes a single analytical
number** — every figure on screen comes verbatim from files the pipeline wrote
in `../DATA/OUTPUTS/`, and every AI sentence was written by a pipeline module that
harness-checked it first.

## Run it locally

Requires **Node.js >= 20** (built and tested on Node 26.5.0).

```bash
npm install
npm run dev
```

Then open **http://localhost:5173**.

- `npm run dev` starts both processes: the API on **:4600** and the Vite dev
  server on **:5173**, which proxies `/api` to :4600 — including the WebSocket
  upgrade at `/api/ws` (`ws: true`), so dev uses the same transport the built
  app does. `PETFOLK_API=http://localhost:4622 npm run web` points the proxy at
  an API on another port.
- Single-port alternative: `npm run build` then `npm start` serves the built
  app *and* the API together on **http://localhost:4600**.
- `npm test` runs the Node test suite (`node --test server/test/*.test.js`) —
  **82 tests**, ~10s, each in its own temp repo root. Three restored-console checks
  skip until local run artifacts exist.
- `npm run verify:memory-loop-browser` drives the canonical Apr 27 → accept
  genuinely new correction decisions → May 4 flow in visible Chrome and saves
  the three proof screenshots plus observed DOM text under the OS temp folder.
  Run the built single-port app first. Set `PETFOLK_BROWSER_HEADLESS=1` for CI,
  `PETFOLK_BROWSER_KEEP_OPEN=1` to leave May 4 visible, or
  `PETFOLK_BROWSER_BASE_URL=http://127.0.0.1:4611` for another port. This uses
  the machine's installed Chrome via `playwright-core`; it downloads no browser
  and writes no generated run artifacts into git.

For the **Run pipeline** button to work, the repo's Python venv must exist at
`../.venv` with the pipeline's requirements installed (from the repo root:
`python3 -m venv .venv && .venv/bin/pip install pandas numpy pytest`).

A run is four real steps, not one: `pipeline.validate` (propose only) →
**pause** while a human accepts/declines each correction in the panel (skipped
when there are none, or with `autoAccept`) → `pipeline.validate --accept <ids>`
→ `pipeline.run`. Re-running the same Monday is idempotent — the pipeline's
ledger replays rather than duplicates.

## The two views

- **`/` — Monday Digest**: Dr. Priya's page. Renders
  `DATA/OUTPUTS/<monday>/digest.json` verbatim, in the locked section order: top
  signals (with click-open score receipts), Claimed vs. Verified, the
  recommendation ledger, and the click-deep "Data checks: N ran, M
  corrections" strip with the ⓘ harness explanation. Week selector covers
  every Monday found on disk. While a run is mid-flight the page renders the
  *partial* digest and lights up on its own when the full one lands.
- **`/admin` — AI Strategy Lead**: drag the 4 CSVs in (recognized by their
  header columns and staged to `server/uploads/`, never into `DATA/INPUTS/`), pick
  a Monday, run the real pipeline, and watch the four phases execute with
  real events only — pipeline stdout/stderr, its own run-log entries, and
  artifacts landing in `DATA/OUTPUTS/`. With all four tables staged, Run executes
  the pipeline **on the uploaded files** (`PETFOLK_INPUTS_DIR`); otherwise it
  runs on the repo's `DATA/INPUTS/` — the console says which, honestly. Feed
  language is plain English by default (translated server-side in one place,
  `server/lib/plain.js`); the Engineer view toggle shows the raw lines. The
  **Reset** button beside Run (confirm-guarded) puts the Monday back to a clean
  start — see `POST /api/reset` below.

Both views mount the same **side panel**, rendered exclusively from
`GET /api/thread`: per-phase narration written by `pipeline.narrate` from that
phase's real artifacts, the corrections **accept/decline** card the run pauses
on until a human decides, decision records, and "Ask this Monday" questions
with their cited, harness-checked answers. Answers **stream** as they are
written (`GET /api/ask/stream`), shown as a dashed "draft — not verified yet"
until the harness passes them; a rejected draft is cleared and the panel says
so. A model switcher picks the tier to ask (`GET /api/llm-modes`).

## API

| Route | What it does |
| --- | --- |
| `GET /api/health` | What's on disk: repo root, pipeline/python/ledger/validation-report presence, and every Monday with output. |
| `GET /api/llm-modes` | Which tiers this machine can actually ask, best first (`claude-cli` → `api` → `openai` → `template`), plus the default. Reads key **names** from the environment and from the gitignored `.env.local` — never values. Capability reporting only: the pipeline still decides, still degrades, and still names the tier that wrote each answer. |
| `GET /api/weeks` | The Mondays on disk, each flagged `has_digest` / `has_signals`. |
| `GET /api/digest/:asOf` | Serves `DATA/OUTPUTS/<asOf>/digest.json` byte-for-byte. If only `signals.json` exists, assembles a partial digest (flagged `"partial": true`, verdicts pending). 400 on a non-date; 404 (listing the weeks that do exist) if neither file is there. |
| `GET /api/ledger` | `DATA/OUTPUTS/ledger.csv` + `ledger_log.jsonl` parsed to JSON. 404 until the first run creates them. |
| `POST /api/ledger/decide {recId, action, note?, newCheckBy?, asOf?, by?}` | A leader acting on a tracked recommendation. `action` is `close` \| `relaunch` (needs `newCheckBy`) \| `escalate` \| `dismiss` (needs `note` — the reason is what the next run's memory keeps). The server decides nothing: it shells out to `python -m pipeline.ledger --decide … --json` and posts the decision into the Monday's thread. `asOf` defaults to the running (or latest) Monday. |
| `POST /api/run {asOf, uploadId?, autoAccept?}` | Spawns the real pipeline for that Monday and returns 202 with `{inputs: {mode: "uploaded" \| "repo"}}`. With `uploadId`, the whole run executes with `PETFOLK_INPUTS_DIR=<staging dir>`: `pipeline.validate --accept all` first, then `pipeline.run` — 400 (naming the missing tables) if the session doesn't hold all 4. `autoAccept: true` applies every proposed correction without stopping; the default pauses the run at "awaiting corrections" until `POST /api/run/corrections` decides. |
| `GET /api/run/status[?asOf=…]` | Phase-tagged progress, scoped to a Monday: the live run when it is that Monday's, otherwise that Monday's last run restored from `DATA/OUTPUTS/<asOf>/run_state.json` + `console.jsonl` (or rebuilt from the pipeline's own run logs) — so a server restart or reload replays the finished run instead of claiming nothing happened. After a reset it reports a clean start. |
| `POST /api/run/corrections {accept: [ids], by?}` | The accept/decline decision for a run paused at "awaiting corrections". Listed ids are applied, the rest declined (logged by the pipeline, never applied); `DATA/TRANSLATION/` is rebuilt and the digest run continues. 409 when no run is waiting. |
| `POST /api/reset {asOf, by?}` | Session reset. Stops any run and any answer in flight, archives that Monday's thread, console, state snapshot **and run artifacts** (`digest.json`, `verdicts.json`, `facts.json`, `signals.json`, `narratives.json`, the run logs) into `DATA/OUTPUTS/<asOf>/thread_archive/` — nothing deleted — and rebuilds canonical `DATA/TRANSLATION/` from `DATA/INPUTS/` via `pipeline.validate --accept all`. The digest page then shows its "no digest yet" state and the **next run creates the Monday digest from scratch on screen**. |
| `POST /api/upload[?uploadId=…]` | Identifies each uploaded CSV by sniffing its header columns (never its filename), stages it under its canonical table name, and rejects files matching no table. One upload session = one staging dir; pass `uploadId` to add files to an existing session. |
| `GET /api/thread?asOf=…` | The Monday's conversation: `entries` (append-only from `DATA/OUTPUTS/<asOf>/thread.jsonl`), the server-owned `pending` question, and `narrating` (phases whose narrative is being written). 400 without a valid `asOf`. |
| `POST /api/thread/message {text, asOf?, by?, llmMode?}` | Ask a question. The question is persisted to the thread at once and `pipeline.ask` answers it in the background, so this returns 202 with the queue position. Rejects empty text and text over 1000 characters. `llmMode` is the panel's model switcher — only a tier `GET /api/llm-modes` reports is accepted (400 otherwise), and it is a request, not a promise: an unreachable tier still degrades down the ladder and the answer names the tier that actually wrote it. |
| `GET /api/ask/stream?asOf=…` | Server-sent events for one Monday's answer as it is written: `hello` (state on connect, including text so far), `start`, `generating`, `delta` (**provisional** text), `verifying`, `redo`, `tier_fallback`, `final` (the validated entry — the only text with citations, a tier label and a verified count), `note`/`cancelled`. Comment heartbeats every 15s. A client that ignores this stream still sees the same thread via `GET /api/thread`. |
| `POST /api/thread/cancel {asOf}` | Stops the answer being generated. The question stays in the thread and the stop is recorded; 409 when nothing is pending. |
| `GET /api/ask-log[?asOf=…]` | `DATA/OUTPUTS/ask_log.jsonl` with `{asked, answered, refused}` counts. Asked-and-answered is a digest gap; asked-and-refused is a data gap — the product-discovery instrument, not a debug file. |
| `WS /api/ws` | The session tunnel. One socket per page session carries every route above as a request envelope and every live event as a push. Additional transport only — nothing above changes — and hosted it is what pins a session to one function instance. See **Transport** below. |

When `web/dist` exists the API also serves it statically, with an SPA fallback
for `/admin`, so `node server/index.js` alone serves everything on one port.

## Transport: one session, one server (`/api/ws`)

Every route above is plain HTTP and stays that way — curl, the tests and any
non-browser client are unaffected. The **browser** additionally opens ONE
WebSocket at `/api/ws` and sends every API call through it as a request
envelope; the server dispatches each envelope into this same Express app
in-process and pushes the same live events `GET /api/ask/stream` pushes.

It exists because of a measured deployment problem. Hosted on Vercel (Fluid
Compute), requests from one browser land on **different function instances**,
and every piece of live state — the run manager's phases, the ask queue, the
`/tmp` working copy the pipeline writes into — belongs to one of them. Observed
before the fix: `GET /api/run/status` reporting a live run, then seconds later
the committed restored one. Vercel pins a single WebSocket connection to a
single function instance for its lifetime
([docs](https://vercel.com/docs/functions/websockets)), so one connection means
one instance, and the session stays coherent. Measured on a preview deployment:
20 tunnelled `/api/health` calls reported **1** `instance_id`; 24 parallel plain
HTTPS calls reported **6**.

- **Protocol** (JSON text frames) — `{id, type:"request", method, path, headers,
  body, bodyEncoding:"utf8"|"base64"}` → `{id, type:"response", status, headers,
  body, bodyEncoding}`; `{type:"subscribe", asOf}` answers with the same `hello`
  snapshot the SSE route sends, then forwards every event verbatim as
  `{type:"event", asOf, event}`, until `{type:"unsubscribe", asOf}` (closing the
  socket releases every subscription too); `{id?, type:"ping"}` →
  `{id, type:"pong", instanceId}`; `{type:"welcome", instanceId, hosted,
  maxSessionSeconds}` on connect. A frame that is not JSON, not an envelope, or
  not a known type is **answered** with `{id?, type:"error", error}` and the
  socket keeps working — one bad frame never takes the instance down.
  `/api/ask/stream` is the one path the request envelope refuses (400, naming
  `subscribe` as the way): a stream that never ends would strand its id forever.
- **Dispatch** is a loopback hop to a private `127.0.0.1` listener carrying the
  same app, so Node's own parser builds a real request and `express.json()`,
  multer and the routes behave identically. (An in-memory injector was tried and
  rejected: `light-my-request` reparents the Express app's request/response
  prototypes, which breaks every *real* HTTP response in the same process.)
- **Fallback** — if the socket cannot open, `web/src/api.js` degrades to `fetch`
  + `EventSource`, exactly as before. `getTransportState()` reports which pipe is
  carrying the session; it never claims one it does not have.
- **Reconnect** — Vercel closes the connection at the function's `maxDuration`
  (300s in `vercel.json`). The client reconnects with backoff and re-subscribes;
  if the new `instanceId` differs it emits an `instance_changed` event so the UI
  re-syncs from the server rather than trusting stale memory.
- **Rollover** — a socket pins one instance for *its* lifetime, and hosted that
  lifetime is finite, so the session is handed from socket to socket instead of
  waiting to be cut. Measured on a preview deployment: a lone socket was cut at
  **311s** (close code 1006) and a reconnect half a second later landed on a
  **different** instance — the run's `/tmp` artifacts, its thread and the
  in-memory run manager all out of reach. So `web/src/api.js` opens a
  **replacement socket while the current one is still open**, and only if its
  `welcome` reports the *same* `instanceId` does it move the live subscriptions
  across, swap it in, and retire the old one (requests already in flight there
  finish there; the seam is a duplicate `hello`, which the UI already re-syncs
  on). A replacement that lands elsewhere is closed and the **working socket
  keeps the session** — retry every 10s, six attempts, then the reconnect path
  owns it.

  The cadence is measured, not chosen. Vercel routes a new connection to the
  instance that most recently accepted one, so an instance holding only an older
  socket stops being reachable: replacing **every 60s co-located 0 of 4** times
  (and six 10s retries all landed on the same wrong instance), while replacing
  **every 30s co-located 9 of 9** — one instance held across nine hand-offs and
  still answering at t=460s, long past the 311s cut. The client ships a **25s**
  cadence for margin; raising it without re-measuring breaks the session, not
  just the optimisation. The cost is one extra connection every 25s per open
  page (a handshake and a subscribe).

  A same-instance hand-off is **not** a reconnect: no `instance_changed`,
  `reconnects` unchanged, and `getTransportState().rollovers` counts it
  separately. Locally (`maxSessionSeconds: null`) nothing rolls over.
  `?rolloverAfterSec=20` (or `localStorage["petfolk.rolloverAfterSec"]`) changes
  only the timing — a dev knob for watching a hand-off happen, never a switch
  for whether a session is allowed to roll over.

## Deploying (Vercel)

`vercel.json` rewrites `/api/(.*)` to `api/index.js`, which prepares a writable
`/tmp` copy of the runtime folders, points `PETFOLK_PYTHON` at the interpreter
`vercel-build.sh` bundles, and exports **the http.Server** (not the bare Express
app) — that export is what lets the function answer the WebSocket upgrade. The
upgrade travels through the existing rewrite; no extra route is needed. A hosted
deployment can use the `openai` tier when `PETFOLK_OPENAI_API_KEY` is configured;
otherwise it falls back honestly to deterministic wording.

Env overrides (used by the tests; defaults fit this repo): `PETFOLK_REPO_ROOT`,
`PETFOLK_PYTHON`, `PORT`.

## Packages (all of them)

Runtime:

- **express** — the Node API server (routes above, plus static serving of the built app).
- **multer** — parses the multipart CSV upload into the staging directory.
- **ws** — the `/api/ws` session tunnel (see Transport above); the only
  dependency added for it, and the client side is the browser's own WebSocket.
- **react** / **react-dom** — the UI component library and its DOM renderer.
- **react-router-dom** — client-side routing between `/` and `/admin`.
- **@vercel/analytics** — page-view beacon on the deployed site (`<Analytics />`
  in `web/src/App.jsx`); it no-ops when the app runs locally. Nothing on the page
  and no pipeline number depends on it.

Development:

- **vite** — dev server with `/api` proxy, and the production bundler (`npm run build`).
- **@vitejs/plugin-react** — teaches Vite to compile JSX with fast refresh.
- **concurrently** — lets `npm run dev` start the API and Vite in one command.
- **playwright-core** — drives the installed Chrome for the committed two-week
  browser acceptance script; it is test tooling only and downloads no browser.

No CSS framework, no TypeScript, no other dependencies — plain JS and one
hand-rolled stylesheet (`web/src/styles.css`).

## Layout

```
app/
  server/
    index.js               # Express API — the routes above, nothing analytical
    lib/csv.js             # tiny RFC-4180 CSV parser (ledger.csv, locations.csv)
    lib/partial-digest.js  # partial digest when only signals.json exists
    lib/run-manager.js     # spawns pipeline.run + pipeline.narrate, tails the
                           #   run logs, phase-tags events, owns reset/archiving
    lib/thread.js          # the per-Monday thread store (thread.jsonl, append-only)
    lib/ask-engine.js      # spawns pipeline.ask (incl. --stream), queues asks,
                           #   holds them while a run owns the artifacts
    lib/ledger-decide.js   # spawns pipeline.ledger --decide, posts to the thread
    lib/plain.js           # one place that turns pipeline events into plain English
    lib/ws-tunnel.js       # the /api/ws session tunnel: envelopes in, the same
                           #   app's responses out, live events pushed
    test/*.test.js         # node --test: ask engine, ledger decide, narrate→thread,
                           #   reset, run status, console restore, ws tunnel
    uploads/               # CSV staging (created at runtime; never touches DATA/INPUTS/)
  web/
    index.html, vite.config.mjs
    src/
      views/DigestView.jsx   # Dr. Priya's Monday page
      views/AdminView.jsx    # upload → run → live phases
      components/            # SidePanel, SignalCard, VerdictTable, LedgerSection, DataChecks
      styles.css             # light "clinical memo" theme, system fonts
```

Repository invariants: leader-facing output uses real center names only (location
IDs never reach the DOM); the leader-facing name is always "Data Validation &
Check"; data-quality detail lives only behind the bottom strip; the progress feed
shows real events, never staged animation; streamed text is provisional until the
harness passes it.
