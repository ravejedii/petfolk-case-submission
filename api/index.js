// Vercel entry point — the same Express app, pointed at a writable working copy.
//
// Routing: vercel.json rewrites /api/(.*) here. Measured on a throwaway
// deployment first — a rewrite hands the function the ORIGINAL request path
// (/api/digest/2026-05-04), which is what Express needs, while a [...path]
// catch-all filename only ever matched a single path segment.
//
// This file adds no behaviour and computes nothing. It exists because a
// deployed function differs from a laptop in exactly two ways:
//
//   1. the deployment filesystem is READ-ONLY except /tmp, and the pipeline
//      writes DATA/TRANSLATION/ and DATA/OUTPUTS/ relative to its own location, so the repo's
//      runtime folders are copied into /tmp once per instance and the server is
//      pointed there with PETFOLK_REPO_ROOT (an override the server already had);
//   2. there is no Python on the Vercel Node runtime, so the deploy carries its
//      own interpreter (vercel-build.sh) and PETFOLK_PYTHON points at it.
//
// Everything else — the run manager spawning `python -m pipeline.run`, the ask
// engine spawning `python -m pipeline.ask`, the harness, the thread — is the
// unmodified local code path. `npm start` on a clone never loads this file.
//
// State honesty: /tmp belongs to one warm function instance. A run's artifacts
// live as long as that instance does; a cold start begins again from the
// committed DATA/OUTPUTS/ (both Mondays), which is what a reviewer should see first.

const fs = require("fs");
const path = require("path");

// Where the deployment's read-only copy of the repo lives (/var/task).
const TASK_ROOT = path.resolve(__dirname, "..");
// The writable working copy the pipeline actually runs against.
const WORK_ROOT = process.env.PETFOLK_WORK_ROOT || "/tmp/petfolk-work";

// Folders the pipeline reads or writes at runtime. DATA/ carries all three of
// INPUTS/ (raw, read-only), TRANSLATION/ (the corrected working copy) and
// OUTPUTS/ (run artifacts), so one entry covers the data a run needs — a
// session reset rebuilds TRANSLATION/ from INPUTS/ exactly as it does locally.
const RUNTIME_DIRS = ["pipeline", "PROMPTS", "DATA"];

function prepareWorkingCopy() {
  if (fs.existsSync(path.join(WORK_ROOT, "pipeline"))) return false; // warm instance
  fs.mkdirSync(WORK_ROOT, { recursive: true });
  for (const dir of RUNTIME_DIRS) {
    const src = path.join(TASK_ROOT, dir);
    if (fs.existsSync(src)) {
      fs.cpSync(src, path.join(WORK_ROOT, dir), { recursive: true });
    }
  }
  return true;
}

const copied = prepareWorkingCopy();

process.env.PETFOLK_REPO_ROOT = WORK_ROOT;
process.env.PETFOLK_PYTHON = path.join(TASK_ROOT, "pybin", "bin", "python3");
// multer stages uploads under the server folder locally; that path is read-only
// here, so the upload session dirs live beside the working copy instead.
process.env.PETFOLK_UPLOADS_DIR = path.join(WORK_ROOT, "uploads");
process.env.PYTHONDONTWRITEBYTECODE = "1";
// Marks the deployed environment for anything that needs to know (health, docs).
process.env.PETFOLK_HOSTED = "1";

if (copied) {
  console.log(`[vercel] working copy prepared at ${WORK_ROOT}`);
  console.log(`[vercel] python: ${process.env.PETFOLK_PYTHON}`);
}

// Export the http.Server, not the bare Express app.
//
// Vercel's Node runtime accepts either; an http.Server is what lets the
// function answer the WebSocket upgrade on /api/ws
// (https://vercel.com/docs/functions/websockets). That upgrade is the whole
// point of the deployed build: a single WebSocket is pinned to ONE function
// instance, so a browser session's run state, /tmp working copy and live
// events stop scattering across instances. Plain HTTP requests to this
// function are served by the same app exactly as before.
const api = require("../app/server/index.js");

module.exports = api.server || api;
