// The session tunnel — one WebSocket, one function instance.
//
// WHY THIS EXISTS. Locally the API is one process, so a run started by POST
// /api/run is the same run GET /api/run/status reports and the same run whose
// narration the SSE stream carries. Hosted on Vercel it is not: Fluid Compute
// spreads a browser's requests across function instances, and every piece of
// this app's live state (the run manager's phases, the ask engine's queue, the
// /tmp working copy the pipeline writes into) belongs to ONE instance. Measured
// on the deployment: two GET /api/run/status calls seconds apart, one reporting
// the live run, the next reporting the committed restored one. The UI looks
// broken because it is talking to two servers.
//
// Vercel's own answer (https://vercel.com/docs/functions/websockets): "A single
// WebSocket connection is pinned to one Vercel Function instance. Messages sent
// over that connection reach the same function instance for the lifetime of the
// connection." So the browser opens ONE WebSocket per page session and sends
// every API call through it as an envelope. The tunnel dispatches each envelope
// into the SAME Express app in-process — same routes, same validation, same
// multer, same run manager, same ask engine — and pushes the same live events
// the SSE endpoint pushes. One connection, one instance, coherent state.
//
// It is an ADDITIONAL transport, never a replacement. Every HTTP route and the
// SSE endpoint keep working untouched, so curl, the tests, and the browser
// fallback path all behave exactly as they did.
//
// Protocol (JSON text frames).
//
//   server → client, on connect:
//     {type:"welcome", instanceId, hosted, maxSessionSeconds}
//       instanceId identifies the process. If it changes after a reconnect the
//       client is on a different instance and must re-sync — the honest signal
//       that hosted state moved, not something to paper over.
//
//   client → server:
//     {id, type:"request", method, path:"/api/…?query", headers?, body?,
//      bodyEncoding:"utf8"|"base64"}
//     {type:"subscribe",   asOf:"YYYY-MM-DD"}
//     {type:"unsubscribe", asOf:"YYYY-MM-DD"}
//     {id?, type:"ping"}
//
//   server → client:
//     {id, type:"response", status, headers, body, bodyEncoding}
//     {type:"event", asOf, event:{…}}   every event the subscription yields,
//                                       forwarded verbatim and unfiltered
//     {id?, type:"error", error}        a malformed envelope answers, never crashes
//     {id, type:"pong"}
//
// HOW AN ENVELOPE BECOMES A REQUEST. The tunnel opens one extra HTTP listener
// on 127.0.0.1:0 — the SAME Express app, in the SAME process, holding the SAME
// run manager and ask engine — and replays each envelope against it over
// loopback. That is deliberately unclever: Node's own HTTP parser builds a real
// IncomingMessage, so express.json(), multer's multipart parsing, res.type(),
// status codes and headers behave identically to a request that arrived from a
// browser, because they ARE that. Nothing about the request path is simulated.
//
// The obvious alternative — an in-memory injector (light-my-request) — was
// tried and rejected on evidence: to make its mock objects survive Express's
// per-request `Object.setPrototypeOf(req, app.request)`, it reparents the
// app's request/response prototypes to its own. That poisons the app for every
// REAL HTTP request afterwards (measured: `res.writeHead` then throws
// "Cannot set properties of undefined (setting 'headers')"). This server has
// to serve both transports at once, so an injector that rewrites the app is
// not an option. A loopback hop costs well under a millisecond and rewrites
// nothing.

const crypto = require("node:crypto");
const http = require("node:http");
const { WebSocketServer } = require("ws");

// Identifies this process for the lifetime of the process. On Vercel that is
// one function instance; locally it is the one server you started.
const INSTANCE_ID = crypto.randomBytes(6).toString("hex");

// Response bodies that are text go over the wire as text; anything else is
// base64 so the tunnel can never corrupt a byte. Every route in this API
// answers JSON or CSV, so the base64 branch is a safety net, not a hot path.
const TEXT_TYPE_RE = /^text\/|json|csv|xml|javascript|svg/i;

// Hop-by-hop headers describe the HTTP connection the mock response never had.
const DROP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "transfer-encoding",
  "upgrade",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
]);

// Headers a client may not dictate: they describe the body the tunnel builds.
const CLIENT_HEADER_BLOCKLIST = new Set([
  "content-length",
  "connection",
  "host",
  "transfer-encoding",
  "upgrade",
]);

function cleanHeaders(headers) {
  const out = {};
  for (const [k, v] of Object.entries(headers || {})) {
    const key = String(k).toLowerCase();
    if (DROP_HEADERS.has(key)) continue;
    out[key] = Array.isArray(v) ? v.join(", ") : String(v);
  }
  return out;
}

function requestHeaders(headers) {
  const out = {};
  for (const [k, v] of Object.entries(headers || {})) {
    const key = String(k).toLowerCase();
    if (CLIENT_HEADER_BLOCKLIST.has(key)) continue;
    if (typeof v !== "string" && typeof v !== "number") continue;
    out[key] = String(v);
  }
  return out;
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

// A tunnelled call that never comes back would strand its envelope id. The
// slowest real route awaits a python process (a reset, a ledger decision).
const DISPATCH_TIMEOUT_MS = 180000;

// The loopback dispatcher: one private listener carrying the same app.
function createDispatcher(app) {
  const agent = new http.Agent({ keepAlive: true, maxSockets: 16 });
  let listening = null;
  let internal = null;

  const start = () => {
    if (listening) return listening;
    listening = new Promise((resolve, reject) => {
      internal = http.createServer(app);
      // 127.0.0.1 only — this listener is an internal detail of the process,
      // never a second public port.
      internal.once("error", (err) => {
        // A failed bind must not poison every later request: forget the
        // attempt so the next envelope can try again.
        listening = null;
        internal = null;
        reject(err);
      });
      internal.listen(0, "127.0.0.1", () => {
        internal.removeAllListeners("error");
        // A socket error after the bind is a dropped loopback connection; the
        // request's own error handler reports it. Nothing here may throw.
        internal.on("error", () => {});
        internal.on("clientError", (err, socket) => socket.destroy());
        // Nothing should be kept alive by this listener; the transport the
        // browser actually talks to owns the process lifetime.
        internal.unref();
        resolve(internal.address().port);
      });
    });
    return listening;
  };

  const dispatch = async ({ method, url, headers, payload }) => {
    const port = await start();
    const outHeaders = { ...headers };
    if (payload) outHeaders["content-length"] = String(payload.length);
    return new Promise((resolve, reject) => {
      const req = http.request(
        { host: "127.0.0.1", port, method, path: url, headers: outHeaders, agent },
        (res) => {
          const chunks = [];
          res.on("data", (c) => chunks.push(c));
          res.on("end", () =>
            resolve({
              statusCode: res.statusCode,
              headers: res.headers,
              body: Buffer.concat(chunks),
            })
          );
          res.on("error", reject);
        }
      );
      req.setTimeout(DISPATCH_TIMEOUT_MS, () => {
        req.destroy(new Error("the route did not answer in time"));
      });
      req.on("error", reject);
      if (payload) req.end(payload);
      else req.end();
    });
  };

  const close = () => {
    agent.destroy();
    if (internal) internal.close();
    internal = null;
    listening = null;
  };

  return { dispatch, close };
}

/**
 * Attach the tunnel to an http.Server.
 *
 * @param {import("http").Server} server  the server the Express app is on
 * @param {Function} app                  the Express app (any (req,res) handler)
 * @param {object}   opts
 * @param {Function} opts.subscribe       (asOf, send) => unsubscribe. The same
 *                                        helper the SSE route uses, so both
 *                                        transports deliver identical events.
 * @param {string}  [opts.path]           WebSocket path (default /api/ws)
 * @param {number}  [opts.maxSessionSeconds] advertised connection lifetime
 *                                        (Vercel closes at maxDuration)
 * @returns {import("ws").WebSocketServer}
 */
function attachTunnel(server, app, opts = {}) {
  const {
    subscribe,
    path: wsPath = "/api/ws",
    maxSessionSeconds = null,
  } = opts;

  const wss = new WebSocketServer({
    server,
    path: wsPath,
    // The largest thing that ever crosses this is four CSVs; 32MB is generous
    // and still bounded, so one frame cannot exhaust the instance.
    maxPayload: 32 * 1024 * 1024,
  });

  const dispatcher = createDispatcher(app);
  server.on("close", () => dispatcher.close());

  wss.on("connection", (ws) => {
    const subs = new Map(); // asOf -> unsubscribe

    const sendJson = (obj) => {
      if (ws.readyState !== ws.OPEN) return;
      try {
        ws.send(JSON.stringify(obj));
      } catch {
        // the socket went away mid-write; close cleans up
      }
    };

    sendJson({
      type: "welcome",
      instanceId: INSTANCE_ID,
      hosted: Boolean(process.env.PETFOLK_HOSTED),
      maxSessionSeconds,
    });

    const fail = (id, status, error) => {
      if (id) {
        sendJson({
          id,
          type: "response",
          status,
          headers: { "content-type": "application/json; charset=utf-8" },
          body: JSON.stringify({ error }),
          bodyEncoding: "utf8",
        });
      } else {
        sendJson({ type: "error", error });
      }
    };

    const handleRequest = async (msg) => {
      const id = typeof msg.id === "string" && msg.id ? msg.id : null;
      if (!id) return fail(null, 400, "A request envelope needs a string id.");

      const url = typeof msg.path === "string" ? msg.path : "";
      if (!url.startsWith("/api/")) {
        return fail(id, 400, "Tunnelled requests must target an /api/ path.");
      }
      // The SSE endpoint never ends; through a request/response envelope it
      // would hang the id forever. Subscriptions are what this transport has.
      if (url.startsWith("/api/ask/stream")) {
        return fail(
          id,
          400,
          'Use {"type":"subscribe","asOf":…} — the tunnel carries live events ' +
            "on the socket, not as a request that never completes."
        );
      }

      const method = String(msg.method || "GET").toUpperCase();
      let payload;
      if (typeof msg.body === "string") {
        payload =
          msg.bodyEncoding === "base64"
            ? Buffer.from(msg.body, "base64")
            : Buffer.from(msg.body, "utf8");
      }

      let res;
      try {
        res = await dispatcher.dispatch({
          method,
          url,
          headers: requestHeaders(msg.headers),
          payload,
        });
      } catch (err) {
        return fail(id, 502, `Tunnel dispatch failed: ${err.message}`);
      }

      const headers = cleanHeaders(res.headers);
      const contentType = headers["content-type"] || "";
      const asText = !contentType || TEXT_TYPE_RE.test(contentType);
      sendJson({
        id,
        type: "response",
        status: res.statusCode,
        headers,
        body: asText
          ? res.body.toString("utf8")
          : res.body.toString("base64"),
        bodyEncoding: asText ? "utf8" : "base64",
      });
    };

    const handleSubscribe = (msg) => {
      const asOf = typeof msg.asOf === "string" ? msg.asOf : "";
      if (!DATE_RE.test(asOf)) {
        return sendJson({
          type: "error",
          error: "subscribe needs asOf=YYYY-MM-DD (the digest Monday).",
        });
      }
      if (typeof subscribe !== "function") {
        return sendJson({ type: "error", error: "No subscription source." });
      }
      if (subs.has(asOf)) subs.get(asOf)();
      // Whatever the run manager and ask engine emit is forwarded as-is: the
      // tunnel never filters or reshapes an event, so a new event type reaches
      // the panel the day the pipeline starts emitting it.
      const unsubscribe = subscribe(asOf, (event) =>
        sendJson({ type: "event", asOf, event })
      );
      subs.set(asOf, unsubscribe);
    };

    const handleUnsubscribe = (msg) => {
      const asOf = typeof msg.asOf === "string" ? msg.asOf : "";
      const un = subs.get(asOf);
      if (un) {
        un();
        subs.delete(asOf);
      }
    };

    ws.on("message", (raw) => {
      let msg;
      try {
        msg = JSON.parse(typeof raw === "string" ? raw : raw.toString("utf8"));
      } catch {
        return sendJson({ type: "error", error: "Frame was not JSON." });
      }
      if (!msg || typeof msg !== "object") {
        return sendJson({ type: "error", error: "Frame was not an envelope." });
      }
      switch (msg.type) {
        case "request":
          // Errors inside dispatch already answer the envelope; this catch is
          // the last stop so one bad frame can never take the instance down.
          handleRequest(msg).catch((err) =>
            fail(typeof msg.id === "string" ? msg.id : null, 500, err.message)
          );
          return;
        case "subscribe":
          return handleSubscribe(msg);
        case "unsubscribe":
          return handleUnsubscribe(msg);
        case "ping":
          return sendJson({
            id: typeof msg.id === "string" ? msg.id : undefined,
            type: "pong",
            instanceId: INSTANCE_ID,
          });
        default:
          return sendJson({
            type: "error",
            error: `Unknown envelope type: ${String(msg.type)}`,
          });
      }
    });

    const cleanup = () => {
      for (const un of subs.values()) {
        try {
          un();
        } catch {
          /* a subscription that already ended */
        }
      }
      subs.clear();
    };
    ws.on("close", cleanup);
    ws.on("error", cleanup);
  });

  wss.dispatcher = dispatcher;
  return wss;
}

module.exports = { attachTunnel, INSTANCE_ID };
