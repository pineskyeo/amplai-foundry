// Isolated offline document acceptance. No user browser, profile, network or API.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { spawn } from "node:child_process";

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
class Client {
  constructor(url) { this.socket = new WebSocket(url); this.pending = new Map(); this.sequence = 0; this.events = []; }
  async open() {
    await new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener("error", reject, { once: true });
    });
    this.socket.addEventListener("message", event => {
      const item = JSON.parse(event.data);
      if (!item.id) { this.events.push(item); return; }
      const pending = this.pending.get(item.id);
      if (!pending) return;
      clearTimeout(pending.timer); this.pending.delete(item.id);
      if (item.error) pending.reject(new Error(item.error.message)); else pending.resolve(item.result);
    });
    return this;
  }
  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++this.sequence;
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error("Browser timeout: " + method)); }, 12000);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    assert.ok(!result.exceptionDetails, "Browser evaluation failed");
    return result.result.value;
  }
  async wait(expression) {
    for (let n = 0; n < 150; n++) { if (await this.evaluate(expression)) return; await delay(40); }
    throw new Error("Browser state did not converge");
  }
  close() {
    for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(new Error("Browser closed")); }
    this.pending.clear(); this.socket.close();
  }
}

const [input, evidence] = process.argv.slice(2);
assert.ok(input && evidence, "Provide an explicit generated bundle and evidence directory");
const directory = await fs.realpath(input);
await fs.mkdir(evidence, { recursive: true });
const pages = (await fs.readdir(directory)).filter(name => name.endsWith(".html")).sort();
assert.ok(pages.includes("index.html") && pages.length > 1);
const available = [];
for (const candidate of [process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  // A packaged browser comes before a confined one. A snap gets its own private
  // /tmp and user namespace, so it never reaches the profile or the bundle this
  // process created, and it stays alive without ever serving the endpoint.
  "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium-browser", "/usr/bin/chromium"
].filter(Boolean)) {
  try { await fs.access(candidate); available.push(candidate); } catch {}
}
assert.ok(available.length, "ENVIRONMENT_UNAVAILABLE: Chrome/Chromium required; not a skipped PASS");
// Ubuntu 24.04 and later confine unprivileged user namespaces with AppArmor, so
// Chrome's own process sandbox cannot start there. That sandbox is unrelated to
// the isolation this fixture asserts, which comes from the disposable profile,
// the NOTFOUND resolver rule and the offline emulation below.
let restrictedUserns = false;
try {
  const setting = await fs.readFile("/proc/sys/kernel/apparmor_restrict_unprivileged_userns", "utf8");
  restrictedUserns = setting.trim() === "1";
} catch {}
const attempts = [];
const start = async executable => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "amplai-document-browser-"));
  const attempt = { executable, profile, exited: false, stopped: null, diagnostics: "", port: null };
  attempt.child = spawn(executable, ["--headless=new", "--disable-background-networking", "--disable-extensions",
    "--no-first-run", "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", `--user-data-dir=${profile}`,
    ...(restrictedUserns ? ["--no-sandbox"] : []), "about:blank"], { stdio: ["ignore", "ignore", "pipe"] });
  attempt.child.stderr.on("data", chunk => { attempt.diagnostics = (attempt.diagnostics + chunk).slice(-2000); });
  attempt.exit = new Promise(resolve => attempt.child.once("exit", (status, signal) => {
    attempt.exited = true; attempt.stopped = signal ?? status; resolve();
  }));
  attempts.push(attempt);
  // The endpoint is announced on stderr as well as recorded in the profile.
  for (let n = 0; n < 300 && !attempt.port && !attempt.exited; n++) {
    const announced = /DevTools listening on ws:\/\/127\.0\.0\.1:([0-9]+)\//.exec(attempt.diagnostics);
    if (announced) { attempt.port = announced[1]; break; }
    try {
      const [recorded] = (await fs.readFile(path.join(profile, "DevToolsActivePort"), "utf8")).trim().split(/\r?\n/);
      if (/^[0-9]+$/.test(recorded)) { attempt.port = recorded; break; }
    } catch {}
    await delay(40);
  }
  return attempt;
};
let client;
try {
  let browser;
  for (const executable of available) {
    browser = await start(executable);
    if (browser.port) break;
  }
  // Report why every candidate refused instead of an unattributable failure.
  assert.ok(browser && browser.port, "Isolated browser did not start: " + attempts.map(item =>
    `executable=${item.executable} no_sandbox=${restrictedUserns} exit=${item.stopped}`
    + ` stderr=${item.diagnostics.trim()}`).join(" | "));
  const target = await (await fetch(`http://127.0.0.1:${browser.port}/json/new?about:blank`, { method: "PUT" })).json();
  client = await new Client(target.webSocketDebuggerUrl).open();
  const version = await client.send("Browser.getVersion");
  await client.send("Page.enable");
  await client.send("Runtime.enable");
  await client.send("Network.enable");
  await client.send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0 });
  // A debugger-injected script bypasses normal page execution rules. Use an
  // ordinary script on an isolated control page to prove the no-script setting.
  await client.send("Emulation.setScriptExecutionDisabled", { value: true });
  const control = 'data:text/html,<h1>Control</h1><script>window.SCRIPT_EXECUTION_CANARY=true</script>';
  await client.send("Page.navigate", { url: control });
  await client.wait("document.querySelector('h1')?.textContent === 'Control' && document.readyState === 'complete'");
  assert.equal(await client.evaluate("typeof window.SCRIPT_EXECUTION_CANARY"), "undefined");
  await client.send("Emulation.setScriptExecutionDisabled", { value: false });
  await client.send("Page.reload");
  await client.wait("window.SCRIPT_EXECUTION_CANARY === true");
  await client.send("Emulation.setScriptExecutionDisabled", { value: true });
  await client.send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
  for (const name of pages) {
    const url = pathToFileURL(path.join(directory, name)).href;
    await client.send("Page.navigate", { url });
    await client.wait(`location.href === ${JSON.stringify(url)} && document.readyState === 'complete'`);
    assert.ok(await client.evaluate("document.querySelector('main') && document.querySelector('nav') && document.querySelector('style')"));
    assert.equal(await client.evaluate("typeof window.SCRIPT_EXECUTION_CANARY"), "undefined");
    assert.equal(await client.evaluate("typeof window.BAD"), "undefined");
    assert.equal(await client.evaluate("document.querySelectorAll('script,iframe,img,object,embed,link[rel=stylesheet]').length"), 0);
    assert.equal(await client.evaluate("document.documentElement.outerHTML.includes('PRIVATE_')"), false);
    assert.equal(await client.evaluate("Array.from(document.querySelectorAll('a[href^=\"#\"]')).every(a => document.getElementById(decodeURIComponent(a.hash.slice(1))))"), true);
    assert.equal(await client.evaluate("document.documentElement.scrollWidth <= innerWidth"), true);
  }
  await client.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
  await client.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
  assert.equal(await client.evaluate("document.activeElement.className"), "skip");
  await client.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  await client.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  await client.wait("document.activeElement.id === 'content'");
  const desktop = await client.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  await fs.writeFile(path.join(evidence, "desktop.png"), Buffer.from(desktop.data, "base64"));
  await client.send("Emulation.setDeviceMetricsOverride", { width: 375, height: 812, deviceScaleFactor: 1, mobile: true });
  assert.equal(await client.evaluate("innerWidth"), 375);
  assert.equal(await client.evaluate("document.documentElement.scrollWidth <= innerWidth"), true);
  const mobile = await client.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  await fs.writeFile(path.join(evidence, "mobile.png"), Buffer.from(mobile.data, "base64"));
  const guideUrl = pathToFileURL(path.join(directory, pages.find(name => name !== "index.html"))).href;
  await client.send("Page.navigate", { url: guideUrl });
  await client.wait(`location.href === ${JSON.stringify(guideUrl)} && document.readyState === 'complete'`);
  assert.equal(await client.evaluate("document.documentElement.scrollWidth <= innerWidth"), true);
  const guideMobile = await client.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  await fs.writeFile(path.join(evidence, "guide-mobile.png"), Buffer.from(guideMobile.data, "base64"));
  await client.send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
  const guideDesktop = await client.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  await fs.writeFile(path.join(evidence, "guide-desktop.png"), Buffer.from(guideDesktop.data, "base64"));
  await client.send("Emulation.setEmulatedMedia", { media: "print" });
  assert.equal(await client.evaluate("matchMedia('print').matches && getComputedStyle(document.querySelector('nav')).display === 'none'"), true);
  const printed = await client.send("Page.printToPDF", { printBackground: true, preferCSSPageSize: true });
  const pdf = Buffer.from(printed.data, "base64");
  assert.ok(pdf.subarray(0, 5).toString() === "%PDF-" && pdf.length > 1000);
  await fs.writeFile(path.join(evidence, "print.pdf"), pdf);
  await client.send("Emulation.setScriptExecutionDisabled", { value: false });
  await client.send("Page.reload");
  await client.wait("document.readyState === 'complete' && document.querySelector('main')");
  assert.equal(await client.evaluate("typeof window.BAD"), "undefined");
  assert.equal(await client.evaluate("document.querySelectorAll('script,iframe,img,object,embed').length"), 0);
  assert.equal(client.events.filter(x => x.method === "Page.javascriptDialogOpening").length, 0);
  assert.equal(client.events.filter(x => x.method === "Network.requestWillBeSent" && /^https?:/.test(x.params.request.url)).length, 0);
  const result = { verdict: "PASS", browser: version.product, pages: pages.length, offline: true,
    javascript_disabled: true, javascript_enabled_xss_safe: true,
    private_canaries_absent: true, executable_source_absent: true,
    keyboard_skip_link: true, desktop_no_overflow: true, mobile_width: 375, mobile_no_overflow: true,
    print_media: true, pdf_generated: true, http_requests: 0, environment: "isolated local browser fixture; not production" };
  await fs.writeFile(path.join(evidence, "browser.json"), JSON.stringify(result, null, 2) + "\n");
  process.stdout.write(JSON.stringify(result) + "\n");
} finally {
  if (client) client.close();
  for (const attempt of attempts) {
    if (!attempt.exited) { attempt.child.kill("SIGTERM"); await Promise.race([attempt.exit, delay(3000)]); }
    if (!attempt.exited) { attempt.child.kill("SIGKILL"); await attempt.exit; }
    // Only this process's newly-created disposable browser profiles are removed.
    assert.ok(attempt.profile.startsWith(path.join(os.tmpdir(), "amplai-document-browser-")));
    await fs.rm(attempt.profile, { recursive: true, force: true });
  }
}
