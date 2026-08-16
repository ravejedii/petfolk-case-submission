// How the UI talks to the API — one session, one server.
//
// THE PROBLEM THIS SOLVES. Every view here talks to the Node API only; the UI
// computes nothing analytical, it renders what the pipeline wrote. That works
// on a laptop because there is one API process. Hosted on Vercel there is not:
// Fluid Compute spreads a browser's requests across function instances, and a
// run's state — the run manager's phases, the ask queue, the /tmp working copy
// the pipeline writes into — belongs to ONE of them. Measured on the
// deployment: GET /api/run/status reporting a live run, then seconds later the
// committed restored one. Polling, corrections, asks and uploads were each
// rolling dice on reaching the instance that owned the run.
//
// THE FIX. Vercel pins a single WebSocket connection to a single function
// instance for its lifetime (https://vercel.com/docs/functions/websockets), so
// the page opens ONE socket at /api/ws and sends every API call through it as
// a request envelope. The server dispatches each envelope into the same
// Express app in-process and pushes the same live events the SSE endpoint
// pushes (app/server/lib/ws-tunnel.js). One connection, one instance, coherent
// state — and the same code path locally, where the socket simply reaches the
// only server there is.
//
// FALLBACK, NOT MAGIC. If the socket cannot open (an old browser, a proxy that
// blocks upgrades), everything degrades to exactly what it was before: fetch()
// per call and an EventSource for live events. Nothing about the API changed —
// the HTTP routes and GET /api/ask/stream are untouched — so the fallback is
// the original app, not a reduced one. getTransportState() reports which pipe
// is actually carrying the session; it never claims one it does not have.
//
// ROLLOVER. One socket pins one instance for the socket's lifetime, and hosted
// that lifetime is finite: Vercel closes the connection when the function
// reaches maxDuration. Measured on a preview deployment — a lone socket was cut
// at 311s (close code 1006), and a reconnect half a second later landed on a
// DIFFERENT instance (9801d809866e → 320a5fcecec6), which hosted means the run's
// /tmp artifacts, its thread and the in-memory run manager are all somewhere the
// page can no longer reach. A five-minute session is not a session.
//
// So the page hands the session from socket to socket: it opens a REPLACEMENT
// while the current socket is still carrying traffic, checks that the
// replacement's `welcome` reports the SAME instanceId, moves the live
// subscriptions and new requests onto it, and only then retires the old one.
//
// WHEN it does that is not a preference, it is measured. Vercel routes a new
// connection to the instance that most recently accepted one; an instance
// holding nothing but an older socket stops being that target. Against the
// preview deployment:
//
//   every 60s   0 of 4 replacements co-located — each landed on a brand-new
//               instance, and retrying every 10s (6 attempts, 60s) never got
//               back: all six landed on the same wrong instance.
//   every 30s   9 of 9 co-located. The session held instance 0095eaff0ce7
//               across nine hand-offs and was still answering on it at t=460s,
//               well past the 311s where a lone socket dies. Break the cadence
//               (a 180s stall) and the very next attempt landed elsewhere.
//
// Hence a short cadence, not a single dash before the cut: the socket is always
// young, and the instance never stops being the one new connections reach. The
// price is one extra connection every ~25s per open page — a handshake and a
// subscribe, no work — which is what keeping a run reachable costs.
//
// It is a hand-off, not a repair, and it is honest about the difference: a
// replacement that lands on a different instance is CLOSED and the working
// socket keeps the session (retry shortly, bounded — and the measurement says
// that usually loses, so it is a long shot, not the plan), the UI never sees
// `instance_changed` for a same-instance rollover, and `reconnects` still counts
// only the times the session actually moved. Locally (maxSessionSeconds null)
// nothing rolls over — there is no cut to get ahead of.

const WS_PATH = "/api/ws";
// How long to wait for the socket before serving the page over fetch instead.
// Short enough that a blocked upgrade never looks like a hung app.
const FALLBACK_AFTER_MS = 2000;
// A tunnelled request that never comes back. The slowest real call is a reset
// or a ledger decision, both of which await a python process.
const REQUEST_TIMEOUT_MS = 120000;
const BACKOFF_MS = [250, 500, 1000, 2000, 4000, 8000, 15000, 30000];

// Rollover (hosted only). How often the session moves to a fresh socket.
// Measured: 30s holds one instance indefinitely, 60s never co-locates (see the
// header), so 25s is the same behaviour with a margin — not a tuning knob to
// raise without re-measuring.
const ROLLOVER_EVERY_SECONDS = 25;
// …and never later than this before the function's own cut, whatever the
// cadence says, so the hand-off always has room for a handshake and a retry.
const ROLLOVER_LEAD_SECONDS = 60;
// A replacement that landed on another instance. The old socket carries on and
// this asks again — a long shot (a missed cadence usually means the instance
// stopped being the one new connections reach) but a cheap one.
const ROLLOVER_RETRY_MS = 10000;
const ROLLOVER_MAX_ATTEMPTS = 6;
// A replacement that never says `welcome` is not a replacement.
const ROLLOVER_WELCOME_TIMEOUT_MS = 15000;
// After the hand-off the old socket stays open only long enough for the
// requests already on it to come back.
const ROLLOVER_DRAIN_MS = 30000;

const canUseWs =
  typeof window !== "undefined" && typeof window.WebSocket === "function";

const state = {
  mode: canUseWs ? "connecting" : "fallback", // connecting | ws | fallback
  instanceId: null,
  hosted: false,
  maxSessionSeconds: null,
  connectedAt: null,
  reconnects: 0, // the session landed on a different server than before
  rollovers: 0, // the session was handed to a fresh socket on the SAME server
};

let socket = null;
let attempts = 0;
let fallbackTimer = null;
let reconnectTimer = null;
let seq = 0;

// The replacement socket, and the sockets that have handed off and are waiting
// for their last answers. Only `socket` is active; the rest are bookkeeping.
const rollover = { timer: null, candidate: null, candidateTimer: null, attempts: 0 };
const retiring = new Set();

const inflight = new Map(); // id -> {resolve, reject, timer, method, ws}
const outbox = []; // envelopes queued while the socket is opening
const listeners = new Map(); // asOf -> Set<handler>
const sources = new Map(); // asOf -> {kind, close}
const lastHello = new Map(); // asOf -> the most recent server snapshot
const transportWatchers = new Set();

// ---------------------------------------------------------------------------
// Transport state (for anything that wants to show which pipe is carrying it)
// ---------------------------------------------------------------------------

export function getTransportState() {
  return {
    mode: state.mode,
    instanceId: state.instanceId,
    hosted: state.hosted,
    maxSessionSeconds: state.maxSessionSeconds,
    connectedAt: state.connectedAt,
    reconnects: state.reconnects,
    rollovers: state.rollovers,
  };
}

export function onTransportChange(fn) {
  transportWatchers.add(fn);
  return () => transportWatchers.delete(fn);
}

function announceTransport() {
  const snapshot = getTransportState();
  for (const fn of transportWatchers) {
    try {
      fn(snapshot);
    } catch {
      /* a watcher that throws must not take the transport down */
    }
  }
}

// ---------------------------------------------------------------------------
// The socket
// ---------------------------------------------------------------------------

function wsUrl() {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}${WS_PATH}`;
}

function setMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  announceTransport();
}

// Opens a socket and wires it up. Every socket this module owns — the first
// one, a reconnect, a rollover replacement — is created here, so they all
// behave the same; which role a socket is playing is decided at frame time by
// comparing it against `socket` and `rollover.candidate`, never by having two
// sets of handlers.
function openSocket(asCandidate) {
  let ws;
  try {
    ws = new window.WebSocket(wsUrl());
  } catch {
    return null;
  }
  if (asCandidate) rollover.candidate = ws;
  else socket = ws;

  ws.onmessage = (e) => {
    let msg;
    try {
      msg = JSON.parse(e.data);
    } catch {
      return; // a frame we cannot read changes nothing
    }
    handleFrame(msg, ws);
  };

  ws.onclose = () => handleClose(ws);

  ws.onerror = () => {
    // onclose always follows; the retry is scheduled there.
  };

  ws.onopen = () => {
    if (socket === ws) attempts = 0;
    // `welcome` (and therefore the instance identity) arrives as the first
    // frame; the queue waits for it so a request is never sent to a socket
    // whose server has not introduced itself.
  };

  return ws;
}

function connect() {
  if (!canUseWs || socket) return;
  if (!openSocket(false)) {
    scheduleReconnect();
    return;
  }

  // Until the socket proves itself, the page must not sit there waiting.
  if (fallbackTimer === null && state.mode !== "ws") {
    fallbackTimer = window.setTimeout(() => {
      fallbackTimer = null;
      if (state.mode !== "ws") {
        setMode("fallback");
        flushOutboxToFetch();
        resyncSources();
      }
    }, FALLBACK_AFTER_MS);
  }
}

function handleClose(ws) {
  const wasActive = socket === ws;
  const wasCandidate = rollover.candidate === ws;
  if (wasActive) socket = null;
  if (wasCandidate) clearCandidate();
  retiring.delete(ws);

  // Only the requests that were on THIS socket are unanswered. A retiring
  // socket's last answers and the active socket's traffic are separate books.
  failInflightFor(ws, "The session connection closed before the server answered.");

  if (wasActive) {
    // The session's socket is gone, so a hand-off is moot: drop any replacement
    // mid-handshake and let the plain reconnect path own it, including the
    // honest `instance_changed` if it lands somewhere else.
    cancelRollover();
    if (state.mode === "ws") {
      // Hosted, this is the expected end of a function's lifetime, not a bug.
      setMode("connecting");
    }
    scheduleReconnect();
    return;
  }

  if (wasCandidate) {
    console.debug("[tunnel] rollover: the replacement closed before it took over");
    retryRollover();
  }
}

function handleFrame(msg, ws) {
  if (!msg || typeof msg !== "object") return;

  // A replacement that has not taken over yet has exactly one thing to say.
  if (ws === rollover.candidate) {
    if (msg.type === "welcome") handleCandidateWelcome(ws, msg);
    return;
  }

  // Answers are honoured whichever socket carries them: a request sent on the
  // socket being retired finishes there. Ids are unique and deleted on
  // arrival, so nothing can be resolved twice.
  if (msg.type === "response") {
    const waiting = inflight.get(msg.id);
    if (!waiting) return;
    inflight.delete(msg.id);
    window.clearTimeout(waiting.timer);
    waiting.resolve(msg);
    return;
  }

  // Everything else belongs to the socket carrying the session. A retiring
  // socket may still push events the new socket is also pushing (both were
  // subscribed for a moment); delivering them twice would duplicate a live
  // draft, so they stop here.
  if (ws !== socket) return;

  if (msg.type === "welcome") {
    adoptWelcome(msg);
    return;
  }

  if (msg.type === "event") {
    deliver(msg.asOf, msg.event);
    return;
  }

  if (msg.type === "error") {
    // A protocol-level complaint with no id to answer. Nothing to resolve;
    // leaving it in the console keeps it findable without breaking the page.
    if (typeof console !== "undefined") console.warn("[tunnel]", msg.error);
  }
}

// The server introduced itself on the socket now carrying the session.
function adoptWelcome(msg) {
  const previous = state.instanceId;
  state.instanceId = msg.instanceId || null;
  state.hosted = Boolean(msg.hosted);
  state.maxSessionSeconds =
    typeof msg.maxSessionSeconds === "number" ? msg.maxSessionSeconds : null;
  state.connectedAt = new Date().toISOString();
  if (fallbackTimer !== null) {
    window.clearTimeout(fallbackTimer);
    fallbackTimer = null;
  }
  setMode("ws");
  announceTransport();
  resyncSources();
  flushOutbox();
  scheduleRollover();
  if (previous && previous !== state.instanceId) {
    // Honest signal, not a repair: the session moved to a different server
    // instance, so anything the previous one held in memory is not here.
    // Subscribers poll anyway; this tells them to do it now.
    state.reconnects += 1;
    broadcast({ type: "instance_changed", from: previous, to: state.instanceId });
  }
}

function scheduleReconnect() {
  if (!canUseWs || reconnectTimer !== null || socket) return;
  const wait = BACKOFF_MS[Math.min(attempts, BACKOFF_MS.length - 1)];
  attempts += 1;
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, wait);
}

function ensureSocket() {
  if (!canUseWs) return;
  if (!socket && reconnectTimer === null) connect();
}

function socketReady() {
  return (
    state.mode === "ws" &&
    socket &&
    socket.readyState === window.WebSocket.OPEN
  );
}

function sendOn(ws, envelope) {
  if (!ws || ws.readyState !== window.WebSocket.OPEN) return false;
  try {
    ws.send(JSON.stringify(envelope));
    return true;
  } catch {
    return false;
  }
}

function sendRaw(envelope) {
  return sendOn(socket, envelope);
}

// ---------------------------------------------------------------------------
// Rollover: get ahead of the cut instead of recovering from it
// ---------------------------------------------------------------------------

// DEV ONLY, and inert unless someone sets it by hand: `?rolloverAfterSec=30`
// or localStorage["petfolk.rolloverAfterSec"] = "30" makes the hand-off happen
// in seconds instead of minutes, which is the only practical way to watch it
// happen. It changes WHEN the rollover fires, never whether it is allowed —
// a local session (maxSessionSeconds null) still never rolls over.
function devRolloverAfterSec() {
  try {
    const fromQuery = new URLSearchParams(window.location.search).get(
      "rolloverAfterSec"
    );
    const raw =
      fromQuery !== null && fromQuery !== ""
        ? fromQuery
        : window.localStorage.getItem("petfolk.rolloverAfterSec");
    const seconds = Number(raw);
    return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
  } catch {
    return null; // no localStorage (private mode, sandboxed frame): no override
  }
}

function clearRolloverTimer() {
  if (rollover.timer !== null) {
    window.clearTimeout(rollover.timer);
    rollover.timer = null;
  }
}

function clearCandidate() {
  rollover.candidate = null;
  if (rollover.candidateTimer !== null) {
    window.clearTimeout(rollover.candidateTimer);
    rollover.candidateTimer = null;
  }
}

// Give up on a replacement without disturbing the socket that works.
function dropCandidate(ws, reason) {
  clearCandidate();
  try {
    ws.close(1000, reason);
  } catch {
    /* already closing */
  }
}

function cancelRollover() {
  clearRolloverTimer();
  if (rollover.candidate) dropCandidate(rollover.candidate, "session ended");
  rollover.attempts = 0;
}

// Called on every welcome the session adopts — the first socket, a reconnect,
// and each hand-off — so the cadence continues for as long as the page is open.
// Local sessions have no cut to get ahead of and never roll over.
function scheduleRollover() {
  clearRolloverTimer();
  rollover.attempts = 0;
  if (!canUseWs) return;
  // Local (or any server that does not advertise a cut): nothing to get ahead
  // of, so the session keeps the one socket it opened.
  if (!state.hosted) return;
  if (typeof state.maxSessionSeconds !== "number" || state.maxSessionSeconds <= 0) return;

  const dev = devRolloverAfterSec();
  const seconds =
    dev !== null
      ? dev
      : Math.min(
          ROLLOVER_EVERY_SECONDS,
          Math.max(10, state.maxSessionSeconds - ROLLOVER_LEAD_SECONDS)
        );
  rollover.timer = window.setTimeout(() => {
    rollover.timer = null;
    startRollover();
  }, seconds * 1000);
}

function startRollover() {
  if (!canUseWs || rollover.candidate) return;
  if (!socketReady()) return; // nothing to hand off; reconnect owns this now
  if (rollover.attempts >= ROLLOVER_MAX_ATTEMPTS) return;
  rollover.attempts += 1;

  const ws = openSocket(true);
  if (!ws) {
    retryRollover();
    return;
  }
  console.debug(
    "[tunnel] rollover: opening a replacement socket (attempt %d)",
    rollover.attempts
  );
  rollover.candidateTimer = window.setTimeout(() => {
    rollover.candidateTimer = null;
    console.debug("[tunnel] rollover: the replacement never said welcome");
    dropCandidate(ws, "no welcome");
    retryRollover();
  }, ROLLOVER_WELCOME_TIMEOUT_MS);
}

function retryRollover() {
  clearRolloverTimer();
  if (!socketReady()) return;
  if (rollover.attempts >= ROLLOVER_MAX_ATTEMPTS) {
    // Out of attempts: the session keeps the socket it has. When that socket is
    // finally cut, the existing reconnect path takes over and reports the
    // instance change honestly — which is exactly the behaviour without this.
    console.debug(
      "[tunnel] rollover: giving up after %d attempts; the reconnect path has it",
      rollover.attempts
    );
    return;
  }
  rollover.timer = window.setTimeout(() => {
    rollover.timer = null;
    startRollover();
  }, ROLLOVER_RETRY_MS);
}

function handleCandidateWelcome(ws, msg) {
  if (rollover.candidateTimer !== null) {
    window.clearTimeout(rollover.candidateTimer);
    rollover.candidateTimer = null;
  }
  const instanceId = msg.instanceId || null;

  // The session's socket died while this one was connecting. It is not a
  // hand-off any more, it is the reconnect — adopt it through the normal path,
  // which is what reports an instance change when there is one.
  if (!socket) {
    rollover.candidate = null;
    socket = ws;
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    attempts = 0;
    adoptWelcome(msg);
    return;
  }

  if (!instanceId || instanceId !== state.instanceId) {
    // It landed somewhere else. Taking it would move the session off the server
    // that holds this run's state — the very thing this exists to prevent — so
    // it is closed and the working socket carries on. Asking again is a long
    // shot (measured: six retries all landed on the same wrong instance) but it
    // costs one connection and the alternative is giving up early.
    console.debug(
      "[tunnel] rollover: replacement landed on %s, keeping %s",
      instanceId,
      state.instanceId
    );
    dropCandidate(ws, "different instance");
    retryRollover();
    return;
  }

  // Same instance. Move the live subscriptions across BEFORE the swap so the
  // server is already pushing to the new socket when it becomes the session's,
  // then retire the old one. Each new subscription answers with the server's
  // `hello` snapshot, which is what closes the seam: a draft in flight is
  // re-sent whole, so nothing depends on catching every delta across the gap.
  const old = socket;
  for (const [asOf, src] of sources.entries()) {
    if (src.kind === "ws") sendOn(ws, { type: "subscribe", asOf });
  }
  rollover.candidate = null;
  socket = ws;
  state.rollovers += 1;
  rollover.attempts = 0;
  // The session did NOT move: same server, same state, new pipe. `connectedAt`
  // and `reconnects` describe the session, so neither changes here, and no
  // `instance_changed` is broadcast — there was no instance change.
  console.debug(
    "[tunnel] rollover #%d: session handed to a fresh socket on %s",
    state.rollovers,
    instanceId
  );

  for (const asOf of listeners.keys()) ensureSource(asOf); // any that had none
  retireSocket(old);
  flushOutbox();
  scheduleRollover();
  announceTransport();
}

// The old socket is no longer the session's, but requests sent on it are still
// owed an answer, so it is drained rather than cut. Its subscriptions end
// immediately (the new socket has them) so the server stops pushing events
// nobody reads.
function retireSocket(old) {
  retiring.add(old);
  for (const asOf of sources.keys()) sendOn(old, { type: "unsubscribe", asOf });
  const startedAt = Date.now();
  const closeWhenDrained = () => {
    if (!retiring.has(old)) return; // it closed on its own
    const busy = [...inflight.values()].some((item) => item.ws === old);
    if (busy && Date.now() - startedAt < ROLLOVER_DRAIN_MS) {
      window.setTimeout(closeWhenDrained, 200);
      return;
    }
    retiring.delete(old);
    // Past the cap with something still in flight, closing reports it honestly
    // (a GET retries, anything that changes state rejects) — which beats
    // holding a socket the function is about to kill anyway.
    try {
      old.close(1000, "rolled over");
    } catch {
      /* already closing */
    }
  };
  closeWhenDrained();
}

// Queued requests are ones the socket never carried, so they are sent — not
// re-sent — when it opens. Only requests already on the wire live in
// `inflight`, which is what makes a dropped socket unambiguous: queued work
// still happens, sent work is reported honestly as unanswered.
function flushOutbox() {
  while (outbox.length > 0 && socketReady()) {
    const item = outbox.shift();
    // Which socket carried it is what makes a close unambiguous once there are
    // two of them during a hand-off.
    item.ws = socket;
    inflight.set(item.id, item);
    if (!sendRaw(item.envelope)) {
      inflight.delete(item.id);
      item.ws = null;
      outbox.unshift(item);
      return;
    }
  }
}

// The socket never arrived. Everything queued for it goes over fetch instead,
// so the wait costs a couple of seconds, not the request.
function flushOutboxToFetch() {
  const queued = outbox.splice(0, outbox.length);
  for (const item of queued) {
    window.clearTimeout(item.timer);
    item.viaFetch();
  }
}

function failInflightFor(ws, message) {
  for (const [id, waiting] of [...inflight.entries()]) {
    if (waiting.ws !== ws) continue; // it is on the other socket, still alive
    inflight.delete(id);
    window.clearTimeout(waiting.timer);
    waiting.reject(Object.assign(new Error(message), { status: 0 }));
  }
}

// ---------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------

function toError(status, statusText, data) {
  const err = new Error(
    (data && data.error) || `${status} ${statusText || ""}`.trim()
  );
  err.status = status;
  err.data = data;
  return err;
}

function parseBody(res) {
  const body =
    res.bodyEncoding === "base64" ? atob(res.body || "") : res.body || "";
  try {
    return JSON.parse(body);
  } catch {
    return null;
  }
}

// One request, over the socket if there is one and over fetch if there is not.
// Both paths end in the same success value and the same error shape
// (err.status, err.data), so no caller can tell which pipe carried it.
function request({ method, path, headers, body, bodyEncoding }) {
  ensureSocket();

  const viaFetch = () =>
    fetch(path, {
      method,
      headers: headers || undefined,
      body: body === undefined || body === null ? undefined : decodeForFetch(body, bodyEncoding),
    }).then(async (res) => {
      const data = await res.json().catch(() => null);
      if (!res.ok) throw toError(res.status, res.statusText, data);
      return data;
    });

  if (state.mode === "fallback") return viaFetch();

  return new Promise((resolve, reject) => {
    const id = `q${++seq}-${Math.random().toString(36).slice(2, 8)}`;
    const envelope = {
      id,
      type: "request",
      method,
      path,
      headers: headers || {},
      body: body === undefined ? null : body,
      bodyEncoding: bodyEncoding || "utf8",
    };

    const settle = (res) => {
      const data = parseBody(res);
      if (res.status < 200 || res.status >= 300) {
        reject(toError(res.status, "", data));
      } else {
        resolve(data);
      }
    };

    const timer = window.setTimeout(() => {
      inflight.delete(id);
      reject(
        Object.assign(
          new Error("The server did not answer this request in time."),
          { status: 0 }
        )
      );
    }, REQUEST_TIMEOUT_MS);

    const item = {
      id,
      envelope,
      timer,
      method,
      ws: null, // set to the socket that carries it, so a close fails only its own
      resolve: settle,
      reject: (err) => {
        // A GET is safe to repeat, so a socket that died mid-flight costs a
        // retry rather than an error in the UI. Anything that changes state is
        // NOT repeated: reporting the failure honestly beats acting twice.
        if (method === "GET") viaFetch().then(resolve, reject);
        else reject(err);
      },
      viaFetch: () => viaFetch().then(resolve, reject),
    };

    if (socketReady()) {
      item.ws = socket;
      inflight.set(id, item);
      if (!sendRaw(envelope)) {
        inflight.delete(id);
        window.clearTimeout(timer);
        item.viaFetch();
      }
    } else {
      outbox.push(item);
    }
  });
}

function decodeForFetch(body, bodyEncoding) {
  if (bodyEncoding !== "base64") return body;
  const bin = atob(body);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

export async function getJson(url) {
  return request({ method: "GET", path: url });
}

export async function postJson(url, body) {
  return request({
    method: "POST",
    path: url,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    bodyEncoding: "utf8",
  });
}

export async function postFiles(url, files) {
  const form = new FormData();
  for (const f of files) form.append("files", f, f.name);
  // The browser builds the multipart body (and the boundary in the header);
  // the tunnel carries those bytes untouched, so multer on the other side
  // parses the same upload it would parse from a fetch.
  const encoded = new Response(form);
  const contentType = encoded.headers.get("content-type");
  const buffer = await encoded.arrayBuffer();
  return request({
    method: "POST",
    path: url,
    headers: { "Content-Type": contentType },
    body: base64(buffer),
    bodyEncoding: "base64",
  });
}

function base64(arrayBuffer) {
  const bytes = new Uint8Array(arrayBuffer);
  let binary = "";
  const CHUNK = 0x8000; // btoa on the whole array blows the argument limit
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(
      null,
      bytes.subarray(i, i + CHUNK)
    );
  }
  return btoa(binary);
}

// ---------------------------------------------------------------------------
// Live events
//
// One subscription per Monday, shared by every component that asks for it. The
// first thing a subscriber receives is the server's `hello` snapshot (pending
// ask, draft text in flight, narratives in flight), then every event exactly as
// the server emitted it — no filtering here, so an event type the pipeline
// starts emitting reaches the UI the day it exists.
// ---------------------------------------------------------------------------

export function subscribeEvents(asOf, onEvent) {
  if (!asOf || typeof onEvent !== "function") return () => {};
  if (!listeners.has(asOf)) listeners.set(asOf, new Set());
  const set = listeners.get(asOf);
  const first = set.size === 0;
  set.add(onEvent);
  ensureSocket();
  if (first) ensureSource(asOf);
  else replayHello(asOf, onEvent);
  return () => {
    const set = listeners.get(asOf);
    if (!set) return;
    set.delete(onEvent);
    if (set.size === 0) {
      listeners.delete(asOf);
      lastHello.delete(asOf);
      closeSource(asOf);
    }
  };
}

// The panel and the run stepper both subscribe to the same Monday, and each
// must start from a `hello` snapshot — that is how a component that mounts
// mid-run catches up instead of showing an empty box. The socket can just be
// asked again (the server replaces the subscription and answers with a fresh
// snapshot); an EventSource cannot, so the last snapshot is replayed.
function replayHello(asOf, onEvent) {
  const src = sources.get(asOf);
  if (src && src.kind === "ws" && socketReady()) {
    sendRaw({ type: "subscribe", asOf });
    return;
  }
  const cached = lastHello.get(asOf);
  if (cached) Promise.resolve().then(() => onEvent(cached));
}

function deliver(asOf, event) {
  const set = listeners.get(asOf);
  if (!set || !event) return;
  if (event.type === "hello") lastHello.set(asOf, event);
  for (const fn of set) {
    try {
      fn(event);
    } catch {
      /* one bad handler must not stop the others */
    }
  }
}

function broadcast(event) {
  for (const asOf of listeners.keys()) deliver(asOf, event);
}

function ensureSource(asOf) {
  const want = state.mode === "ws" ? "ws" : state.mode === "fallback" ? "sse" : null;
  if (!want) return; // still deciding; resyncSources() opens it once we know
  const existing = sources.get(asOf);
  if (existing && existing.kind === want) return;
  if (existing) existing.close();

  if (want === "ws") {
    if (!socketReady()) return;
    sendRaw({ type: "subscribe", asOf });
    sources.set(asOf, {
      kind: "ws",
      close: () => {
        if (socketReady()) sendRaw({ type: "unsubscribe", asOf });
      },
    });
    return;
  }

  if (typeof window.EventSource !== "function") return;
  const es = new window.EventSource(
    `/api/ask/stream?asOf=${encodeURIComponent(asOf)}`
  );
  es.onmessage = (e) => {
    try {
      deliver(asOf, JSON.parse(e.data));
    } catch {
      // a malformed frame changes nothing: the thread poll is the record
    }
  };
  es.onerror = () => {
    // EventSource reconnects on its own; the `hello` snapshot re-syncs.
  };
  sources.set(asOf, { kind: "sse", close: () => es.close() });
}

function closeSource(asOf) {
  const src = sources.get(asOf);
  if (!src) return;
  src.close();
  sources.delete(asOf);
}

// After a reconnect (or a flip to the fallback) every live subscription is
// re-established on the pipe that now exists. The server answers each one with
// a fresh `hello`, so a page that was mid-narration picks the draft back up.
function resyncSources() {
  for (const asOf of listeners.keys()) {
    const existing = sources.get(asOf);
    const want = state.mode === "ws" ? "ws" : "sse";
    if (existing && existing.kind !== want) {
      existing.close();
      sources.delete(asOf);
    } else if (existing && existing.kind === "ws") {
      // Same kind, new socket: the old subscription died with it.
      sources.delete(asOf);
    }
    ensureSource(asOf);
  }
}

if (canUseWs) connect();
