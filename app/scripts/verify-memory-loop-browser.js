// Visible browser acceptance for the case's load-bearing two-week loop.
//
// Start the built app (npm run build && npm start), then run:
//   npm run verify:memory-loop-browser
//
// It resets prior demo state when there is any, drives Apr 27 through the real
// correction gate, runs May 4 without resetting durable memory, and captures
// the three states a screen recording must show. Screenshots and the observed
// DOM text go to the OS temp directory, never DATA/OUTPUTS/ or git.

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require("playwright-core");

const base = process.env.PETFOLK_BROWSER_BASE_URL || "http://127.0.0.1:4600";
const artifactDir = process.env.PETFOLK_BROWSER_ARTIFACTS ||
  path.join(os.tmpdir(), "petfolk-memory-loop-browser");
const headless = process.env.PETFOLK_BROWSER_HEADLESS === "1";
const keepOpen = process.env.PETFOLK_BROWSER_KEEP_OPEN === "1";
const pace = process.env.PETFOLK_BROWSER_PACE || (headless ? "fast" : "recording");

if (!new Set(["fast", "recording"]).has(pace)) {
  throw new Error("PETFOLK_BROWSER_PACE must be 'recording' or 'fast'.");
}

function duration(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0) {
    throw new Error(`${name} must be a non-negative number of milliseconds.`);
  }
  return value;
}

// Visible runs default to a deliberate screen-recording cadence. The pauses
// hold the load-bearing states long enough to read; they do not slow or fake
// pipeline events. Headless runs and PETFOLK_BROWSER_PACE=fast skip them.
const recording = pace === "recording";
const pacing = {
  slowMo: duration("PETFOLK_BROWSER_SLOW_MO_MS", recording ? 450 : 0),
  transition: duration("PETFOLK_BROWSER_TRANSITION_MS", recording ? 1500 : 0),
  decision: duration("PETFOLK_BROWSER_DECISION_MS", recording ? 2000 : 0),
  scene: duration("PETFOLK_BROWSER_SCENE_HOLD_MS", recording ? 6000 : 0),
  final: duration("PETFOLK_BROWSER_FINAL_HOLD_MS", recording ? 10000 : 0),
};

async function hold(page, milliseconds, label) {
  if (!milliseconds) return;
  process.stdout.write(`Holding ${label} for ${(milliseconds / 1000).toFixed(1)}s\n`);
  await page.waitForTimeout(milliseconds);
}

function chromePath() {
  const candidates = [
    process.env.PETFOLK_CHROME_PATH,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  const found = candidates.find((candidate) => fs.existsSync(candidate));
  if (!found) {
    throw new Error(
      "Chrome was not found. Set PETFOLK_CHROME_PATH to a Chrome/Chromium executable."
    );
  }
  return found;
}

async function resetIfNeeded() {
  if (process.env.PETFOLK_BROWSER_RESET === "0") return;
  const response = await fetch(`${base}/api/reset`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ all: true, by: "Browser acceptance" }),
  });
  // A brand-new checkout has no Mondays to reset. That is already the desired
  // clean state; every other error is real.
  if (!response.ok && response.status !== 400) {
    throw new Error(`Reset failed (${response.status}): ${await response.text()}`);
  }
}

async function selectMonday(page, monday) {
  await page.locator(".week-select select").selectOption(monday);
  await hold(page, pacing.transition, `${monday} selection`);
}

async function waitForRun(page) {
  await page.getByText(/four phases complete/).waitFor({ timeout: 240000 });
}

async function main() {
  fs.mkdirSync(artifactDir, { recursive: true });
  await resetIfNeeded();

  const context = await chromium.launchPersistentContext(
    path.join(artifactDir, "chrome-profile"),
    {
      executablePath: chromePath(),
      headless,
      slowMo: pacing.slowMo,
      viewport: { width: 1512, height: 980 },
    }
  );
  const page = context.pages()[0] || await context.newPage();
  const shots = {
    aprDigest: path.join(artifactDir, "01-apr-27-creates-memory.png"),
    mayBefore: path.join(artifactDir, "02-may-04-loads-memory-before-analysis.png"),
    mayDigest: path.join(artifactDir, "03-may-04-memory-result.png"),
  };

  try {
    await page.goto(`${base}/admin`, { waitUntil: "networkidle" });
    await hold(page, pacing.transition, "opening admin state");
    await selectMonday(page, "2026-04-27");
    await page.getByRole("button", { name: "Run pipeline" }).click();

    await page.locator(".corr-accept").first().waitFor({ timeout: 120000 });
    await hold(page, pacing.scene, "Apr 27 correction decisions");
    while (await page.locator(".corr-accept").count()) {
      await page.locator(".corr-accept").first().click();
      await hold(page, pacing.decision, "accepted correction decision");
    }
    await waitForRun(page);

    await page.goto(`${base}/?week=2026-04-27`, { waitUntil: "networkidle" });
    await page.locator(".memory-band").waitFor();
    const aprMemory = await page.locator(".memory-band").innerText();
    await page.screenshot({ path: shots.aprDigest, fullPage: true });
    await hold(page, pacing.scene, "Apr 27 durable-memory result");

    await page.goto(`${base}/admin`, { waitUntil: "networkidle" });
    await hold(page, pacing.transition, "return to admin");
    await selectMonday(page, "2026-05-04");
    const mayProjected = await page.locator(".memory-band").innerText();
    await hold(page, pacing.scene, "May 4 building-on-Apr-27 preview");
    await page.getByRole("button", { name: "Run pipeline" }).click();

    await page.getByText("Step 0 · Durable memory loaded", { exact: true })
      .waitFor({ timeout: 120000 });
    const mayBefore = await page.locator(".memory-band").innerText();
    const finishedBeforeCapture = Boolean(
      await page.getByText(/four phases complete/).count()
    );
    await page.screenshot({ path: shots.mayBefore, fullPage: true });
    await hold(page, pacing.scene, "May 4 durable memory loaded before analysis");

    await waitForRun(page);
    await page.goto(`${base}/?week=2026-05-04`, { waitUntil: "networkidle" });
    await page.locator(".memory-result-lines").waitFor({ timeout: 30000 });
    const mayMemory = await page.locator(".memory-band").innerText();
    const mayLedger = await page.locator("section").filter({
      hasText: "Recommendation ledger",
    }).innerText();
    const mayDataChecks = await page.locator(".strip").innerText();
    await page.screenshot({ path: shots.mayDigest, fullPage: true });
    await hold(page, pacing.final, "May 4 re-check result");

    const observed = {
      aprMemory,
      mayProjected,
      mayBefore,
      finishedBeforeCapture,
      mayMemory,
      mayLedger,
      mayDataChecks,
      pace,
      pacing,
      screenshots: shots,
    };
    const report = path.join(artifactDir, "observed.json");
    fs.writeFileSync(report, JSON.stringify(observed, null, 2) + "\n");
    process.stdout.write(`${JSON.stringify({ report, ...observed }, null, 2)}\n`);

    if (keepOpen) await new Promise(() => {});
  } finally {
    if (!keepOpen) await context.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
