// The session tunnel must be the SAME API, not a second one.
//
// Hosted, a browser's requests land on different function instances and the
// run state they need lives on one of them, so the app sends everything down a
// single WebSocket — which Vercel pins to a single instance. That only works if
// a tunnelled request is indistinguishable from an HTTP one: same routes, same
// validation, same status codes, same multer. These tests lock exactly that,
// plus the two things a transport must never do — hang, or crash on a bad frame.
//
// No network beyond loopback, no python, no LLM: the server is started on an
// ephemeral port against a temp repo root.
//
// Run with: npm test   (node --test)

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");

const WebSocket = require("ws");

const AS_OF = "2026-05-04";

// A repo root with just enough on disk for the routes under test: one Monday
// with a digest, so /api/health and a subscription have something real to say.
function tempRepo() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "petfolk-ws-"));
  fs.mkdirSync(path.join(root, "DATA", "OUTPUTS", AS_OF), { recursive: true });
  fs.writeFileSync(
    path.join(root, "DATA", "OUTPUTS", AS_OF, "digest.json"),
    JSON.stringify({ as_of: AS_OF, sections: [] })
  );
  fs.mkdirSync(path.join(root, "uploads"), { recursive: true });
  return root;
}

// index.js reads its env at require time, so the harness sets it, requires
// once, and every test shares the one server — the way the deployment does.
const REPO_ROOT = tempRepo();
process.env.PETFOLK_REPO_ROOT = REPO_ROOT;
process.env.PETFOLK_UPLOADS_DIR = path.join(REPO_ROOT, "uploads");
process.env.PETFOLK_PYTHON = path.join(REPO_ROOT, "no-python");

const api = require("../index.js");
const server = api.server;

let base = null;

test.before(async () => {
  assert.ok(server instanceof http.Server, "index.js must export its http server");
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});

test.after(() => {
  // The tunnel keeps a private loopback listener (its dispatcher) alongside
  // the socket server; both are closed here so the test process exits.
  for (const client of api.tunnel.clients) client.terminate();
  api.tunnel.dispatcher.close();
  api.tunnel.close();
  server.close();
});

// A tunnel client: one socket, request/response by envelope id.
function open() {
  const ws = new WebSocket(base.replace(/^http/, "ws") + "/api/ws");
  const waiting = new Map();
  const events = [];
  const notices = [];
  let welcome = null;
  let seq = 0;

  ws.on("message", (raw) => {
    const msg = JSON.parse(raw.toString());
    if (msg.type === "welcome") {
      welcome = msg;
      return;
    }
    if (msg.type === "event") {
      events.push(msg);
      return;
    }
    if (msg.id && waiting.has(msg.id)) {
      const done = waiting.get(msg.id);
      waiting.delete(msg.id);
      done(msg);
      return;
    }
    notices.push(msg);
  });

  const ready = new Promise((resolve, reject) => {
    ws.on("open", resolve);
    ws.on("error", reject);
  });

  return {
    ws,
    events,
    notices,
    ready,
    welcome: () => welcome,
    send: (obj) => ws.send(JSON.stringify(obj)),
    request(envelope) {
      const id = `t${++seq}`;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(
          () => reject(new Error(`tunnelled ${envelope.path} never answered`)),
          5000
        );
        waiting.set(id, (msg) => {
          clearTimeout(timer);
          resolve(msg);
        });
        ws.send(JSON.stringify({ id, type: "request", ...envelope }));
      });
    },
    close: () => ws.close(),
  };
}

const settle = (ms) => new Promise((r) => setTimeout(r, ms));

async function httpJson(pathname) {
  const res = await fetch(base + pathname);
  return { status: res.status, body: await res.json() };
}

test("the socket introduces the instance it pinned the session to", async () => {
  const c = open();
  await c.ready;
  await settle(50);
  const hello = c.welcome();
  assert.equal(hello.type, "welcome");
  assert.match(hello.instanceId, /^[0-9a-f]{12}$/);
  assert.equal(typeof hello.hosted, "boolean");
  c.close();
});

test("a tunnelled GET returns exactly what the HTTP route returns", async () => {
  const c = open();
  await c.ready;

  const direct = await httpJson("/api/health");
  const tunnelled = await c.request({ method: "GET", path: "/api/health" });

  assert.equal(tunnelled.status, 200);
  assert.match(tunnelled.headers["content-type"], /application\/json/);
  const body = JSON.parse(tunnelled.body);
  assert.deepEqual(body, direct.body);
  // ...and it is THIS process answering — the whole point of the tunnel.
  assert.equal(body.instance_id, c.welcome().instanceId);
  c.close();
});

test("the query string survives the tunnel", async () => {
  const c = open();
  await c.ready;
  const res = await c.request({
    method: "GET",
    path: `/api/thread?asOf=${AS_OF}`,
  });
  assert.equal(res.status, 200);
  assert.equal(JSON.parse(res.body).asOf, AS_OF);

  const bad = await c.request({ method: "GET", path: "/api/thread?asOf=nope" });
  assert.equal(bad.status, 400);
  c.close();
});

test("a tunnelled POST is validated by the same route, with the same 400", async () => {
  const c = open();
  await c.ready;

  const payload = JSON.stringify({ asOf: AS_OF, question: "" });
  const direct = await fetch(base + "/api/thread/message", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: payload,
  });
  const directBody = await direct.json();

  const tunnelled = await c.request({
    method: "POST",
    path: "/api/thread/message",
    headers: { "content-type": "application/json" },
    body: payload,
    bodyEncoding: "utf8",
  });

  assert.equal(direct.status, 400, "an empty question is rejected over HTTP");
  assert.equal(tunnelled.status, direct.status);
  assert.deepEqual(JSON.parse(tunnelled.body), directBody);
  c.close();
});

test("a tunnelled multipart upload is staged by multer, same as a browser POST", async () => {
  const c = open();
  await c.ready;

  const locations =
    "location_id,location_name,opened_date,maturity_tier\n" +
    "PCC_001,Ballantyne,2023-01-02,mature\n";
  const form = new FormData();
  form.append("files", new Blob([locations], { type: "text/csv" }), "locations.csv");
  const encoded = new Response(form);
  const contentType = encoded.headers.get("content-type");
  const bytes = Buffer.from(await encoded.arrayBuffer());

  const res = await c.request({
    method: "POST",
    path: "/api/upload?uploadId=tunnel-test",
    headers: { "content-type": contentType },
    body: bytes.toString("base64"),
    bodyEncoding: "base64",
  });

  assert.equal(res.status, 200, res.body);
  const body = JSON.parse(res.body);
  assert.equal(body.upload_id, "tunnel-test");
  assert.equal(body.received[0].matched_as, "locations");
  assert.ok(
    fs.existsSync(
      path.join(REPO_ROOT, "uploads", "tunnel-test", "locations.csv")
    ),
    "the CSV must actually be on disk in the staging session"
  );
  c.close();
});

test("subscribe delivers the hello snapshot first, unfiltered", async () => {
  const c = open();
  await c.ready;
  c.send({ type: "subscribe", asOf: AS_OF });
  await settle(200);

  assert.ok(c.events.length >= 1, "a subscription must answer immediately");
  const first = c.events[0];
  assert.equal(first.type, "event");
  assert.equal(first.asOf, AS_OF);
  assert.equal(first.event.type, "hello");
  // The same four keys the SSE route sends — one live contract, two pipes.
  for (const key of ["asOf", "pending", "stream", "narrating"]) {
    assert.ok(key in first.event, `hello is missing ${key}`);
  }
  c.close();
});

test("a bad subscribe is answered, not obeyed", async () => {
  const c = open();
  await c.ready;
  c.send({ type: "subscribe", asOf: "last monday" });
  await settle(150);
  assert.equal(c.events.length, 0);
  assert.match(c.notices.map((n) => n.error || "").join(" "), /asOf=YYYY-MM-DD/);
  c.close();
});

test("bad frames get an error back and the socket keeps working", async () => {
  const c = open();
  await c.ready;

  c.ws.send("not json at all");
  c.ws.send(JSON.stringify({ type: "teleport" }));
  await settle(150);

  const errors = c.notices.filter((n) => n.type === "error").map((n) => n.error);
  assert.match(errors.join(" | "), /not JSON/i);
  assert.match(errors.join(" | "), /Unknown envelope type/i);

  // Still alive, still the same instance: one malformed frame must not cost
  // the session its pinned server.
  const res = await c.request({ method: "GET", path: "/api/health" });
  assert.equal(res.status, 200);
  assert.equal(JSON.parse(res.body).instance_id, c.welcome().instanceId);
  c.close();
});

test("the tunnel refuses paths it cannot honestly serve", async () => {
  const c = open();
  await c.ready;

  const outside = await c.request({ method: "GET", path: "/index.html" });
  assert.equal(outside.status, 400);
  assert.match(JSON.parse(outside.body).error, /\/api\//);

  // The SSE endpoint never completes; as a request envelope it would hang the
  // id forever, so the tunnel points the caller at its subscription instead.
  const sse = await c.request({
    method: "GET",
    path: `/api/ask/stream?asOf=${AS_OF}`,
  });
  assert.equal(sse.status, 400);
  assert.match(JSON.parse(sse.body).error, /subscribe/);
  c.close();
});

test("closing the socket releases its subscriptions", async () => {
  const before = require("../lib/ws-tunnel").INSTANCE_ID;
  assert.ok(before, "the process identifies itself");

  const c = open();
  await c.ready;
  c.send({ type: "subscribe", asOf: AS_OF });
  await settle(150);
  c.close();
  await settle(150);

  // A leaked subscription would keep emitting into a dead socket; the server
  // stays healthy either way, so what this asserts is that it stays healthy.
  const after = await httpJson("/api/health");
  assert.equal(after.status, 200);
});
