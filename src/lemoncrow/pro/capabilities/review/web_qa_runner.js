'use strict';
// LemonCrow web QA runner (`lc review qa run`).
//
// Replays one scenario's saved steps in a real headless Chromium and writes a
// JSON result. Page content is data only: nothing read from the page is
// evaluated, and nothing on the page chooses an action. The runner follows the
// saved steps and no others. Python decides the outcome from this result.
//
// usage: node web_qa_runner.js SPEC.json RESULT.json

const fs = require('fs');
const path = require('path');
const { createRequire } = require('module');

const [specPath, resultPath] = process.argv.slice(2);
const spec = JSON.parse(fs.readFileSync(specPath, 'utf8'));
const result = {
  setup_error: null,
  browser_version: '',
  playwright_version: '',
  steps: [],
  console: [],
  page_errors: [],
  screenshots: [],
  final_url: '',
  navigation: null,
  blocked_navigation: '',
};
const MAX_CONSOLE = 100;
const MAX_PAGE_ERRORS = 50;
const POLL_MS = 100;
const SECRET_REF = /\$\{secrets\.([A-Za-z_][A-Za-z0-9_]{0,63})\}/g;
const BOT_STATUSES = [403, 429, 503];
const BOT_MARKERS = /cf-chl|challenge-platform|g-recaptcha|h-captcha|hcaptcha|px-captcha|captcha-delivery/i;
const BOT_TITLES = /^(just a moment|attention required|access denied|verify you are human)/i;

class StepError extends Error {
  constructor(status, category, message) {
    super(message);
    this.status = status;
    this.category = category;
  }
}

function firstLine(err) {
  return String((err && err.message) || err).split('\n')[0].slice(0, 500);
}

function originAllowed(raw) {
  if (raw === 'about:blank') return true;
  let url;
  try {
    url = new URL(raw);
  } catch (_err) {
    return false;
  }
  const scheme = url.protocol.replace(':', '');
  const port = Number(url.port || (scheme === 'https' ? 443 : 80));
  const host = url.hostname.toLowerCase();
  return spec.allowed_origins.some(
    (o) => o.scheme === scheme && o.port === port && (o.wildcard ? host.endsWith('.' + o.host) : host === o.host),
  );
}

function locate(page, step) {
  if (step.role) return page.getByRole(step.role, step.name ? { name: step.name, exact: true } : {});
  if (step.label) return page.getByLabel(step.label, { exact: true });
  return page.getByText(step.text, { exact: true });
}

function resolveValue(template) {
  return (template || '').replace(SECRET_REF, (_match, name) => {
    const value = process.env[spec.secret_env_prefix + name];
    if (value === undefined) throw new StepError('could_not_run', 'missing_secret', `secret ${name} is not set`);
    return value;
  });
}

async function poll(check, message) {
  const deadline = Date.now() + spec.step_timeout_ms;
  for (;;) {
    try {
      if (await check()) return;
    } catch (_err) {
      // A navigation in flight destroys the context; read again.
    }
    if (Date.now() >= deadline) throw new StepError('failed', 'expectation', message());
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}

function pathMatches(current, expected) {
  let url;
  try {
    url = new URL(current);
  } catch (_err) {
    return false;
  }
  const trim = (value) => value.replace(/\/+$/, '') || '/';
  const [wantPath, wantQuery] = expected.split('?', 2);
  if (trim(url.pathname) !== trim(wantPath)) return false;
  return wantQuery === undefined || url.search === '?' + wantQuery;
}

async function botWall(page, status) {
  if (!BOT_STATUSES.includes(status)) return;
  let title = '';
  let html = '';
  try {
    title = (await page.title()).trim();
    html = (await page.content()).slice(0, 200000);
  } catch (_err) {
    return;
  }
  if (BOT_TITLES.test(title) || BOT_MARKERS.test(html)) {
    throw new StepError('could_not_run', 'bot_wall', `the preview showed a bot or challenge wall (HTTP ${status})`);
  }
}

function navigationError(err) {
  const message = firstLine(err);
  if (result.blocked_navigation) {
    return new StepError(
      'could_not_run',
      'untrusted_origin',
      `navigation to ${result.blocked_navigation} was blocked: the base review configuration does not trust that origin`,
    );
  }
  if (err && err.name === 'TimeoutError') return new StepError('could_not_run', 'timeout', `the preview did not load: ${message}`);
  if (/net::ERR_|NS_ERROR_|ECONNREFUSED/.test(message)) {
    return new StepError('could_not_run', 'unreachable', `the preview could not be reached: ${message}`);
  }
  return new StepError('could_not_run', 'browser', message);
}

async function runStep(page, step, row) {
  switch (step.action) {
    case 'goto': {
      let response;
      try {
        response = await page.goto(step.url, { waitUntil: 'load' });
      } catch (err) {
        throw navigationError(err);
      }
      const status = response ? response.status() : null;
      row.http_status = status;
      if (!result.navigation) result.navigation = { status, url: page.url() };
      await botWall(page, status);
      if ([502, 503, 504].includes(status)) {
        throw new StepError('could_not_run', 'app_unavailable', `the preview answered HTTP ${status}`);
      }
      if (status !== null && status >= 400) throw new StepError('failed', 'http', `HTTP ${status} for ${step.path}`);
      return;
    }
    case 'click':
      await locate(page, step).click();
      return;
    case 'fill':
      await locate(page, step).fill(resolveValue(step.value));
      return;
    case 'expect_text':
      await poll(
        async () => (await page.locator('body').innerText()).includes(step.text),
        () => `text "${step.text}" is not on the page`,
      );
      return;
    case 'expect_visible':
      await poll(
        async () => locate(page, step).first().isVisible(),
        () => `${step.description.replace(/^expect visible /, '')} is not visible`,
      );
      return;
    case 'expect_url':
      await poll(
        async () => pathMatches(page.url(), step.expected),
        () => `the page is at ${page.url()}, not ${step.expected}`,
      );
      return;
    case 'screenshot': {
      const file = path.join(spec.screenshot_dir, `${step.screenshot}.png`);
      await page.screenshot({ path: file, fullPage: true });
      result.screenshots.push({ name: step.screenshot, file });
      return;
    }
    default:
      throw new StepError('could_not_run', 'setup', `unknown step action ${step.action}`);
  }
}

function classify(err, step) {
  if (err instanceof StepError) return err;
  const message = firstLine(err);
  if (result.blocked_navigation) return navigationError(err);
  if (/Target page, context or browser has been closed|Browser has been closed|crashed/i.test(message)) {
    return new StepError('could_not_run', 'browser', message);
  }
  if (err && err.name === 'TimeoutError') {
    return new StepError('failed', 'expectation', `${step.description}: no matching element within ${spec.step_timeout_ms} ms`);
  }
  return new StepError('failed', 'step', `${step.description}: ${message}`);
}

async function main() {
  let playwright;
  try {
    const load = createRequire(path.join(spec.playwright_dir, 'lemoncrow-qa.js'));
    playwright = load('playwright');
    result.playwright_version = load('playwright/package.json').version;
  } catch (err) {
    result.setup_error = { category: 'setup', message: `Playwright could not be loaded: ${firstLine(err)}` };
    return;
  }
  let browser;
  try {
    browser = await playwright.chromium.launch({ headless: true });
  } catch (err) {
    result.setup_error = { category: 'browser', message: `Chromium could not start: ${firstLine(err)}` };
    return;
  }
  try {
    result.browser_version = browser.version();
    const context = await browser.newContext({ viewport: spec.viewport, serviceWorkers: 'block' });
    const page = await context.newPage();
    page.setDefaultTimeout(spec.step_timeout_ms);
    page.setDefaultNavigationTimeout(spec.navigation_timeout_ms);
    // Only the scenario's own page is driven; a page that opens another is not followed.
    context.on('page', (other) => {
      if (other !== page) other.close().catch(() => {});
    });
    page.on('dialog', (dialog) => dialog.dismiss().catch(() => {}));
    page.on('console', (msg) => {
      if (msg.type() !== 'error' || result.console.length >= MAX_CONSOLE) return;
      const where = msg.location();
      result.console.push({
        level: 'error',
        text: msg.text().slice(0, 2000),
        location: where && where.url ? `${where.url}:${where.lineNumber}` : '',
      });
    });
    page.on('pageerror', (err) => {
      if (result.page_errors.length >= MAX_PAGE_ERRORS) return;
      result.page_errors.push({ message: firstLine(err), stack: String(err.stack || '').slice(0, 4000) });
    });
    await context.route('**/*', (route) => {
      const request = route.request();
      let main = false;
      try {
        main = request.isNavigationRequest() && request.frame() === page.mainFrame();
      } catch (_err) {
        main = false;
      }
      if (main && !originAllowed(request.url())) {
        result.blocked_navigation = request.url().slice(0, 2048);
        return route.abort('blockedbyclient');
      }
      return route.continue();
    });
    let stopped = false;
    for (let index = 0; index < spec.steps.length; index += 1) {
      const step = spec.steps[index];
      const row = { index: index + 1, status: 'skipped', category: '', error: '', started_at: '', duration_ms: 0, http_status: null };
      result.steps.push(row);
      if (stopped) continue;
      row.started_at = new Date().toISOString();
      const started = Date.now();
      try {
        await runStep(page, step, row);
        row.status = 'passed';
        if (result.blocked_navigation) throw navigationError(new Error('blocked'));
      } catch (err) {
        const outcome = classify(err, step);
        row.status = outcome.status;
        row.category = outcome.category;
        row.error = outcome.message.slice(0, 2000);
        stopped = true;
      }
      row.duration_ms = Date.now() - started;
    }
    result.final_url = page.url().slice(0, 2048);
  } finally {
    await browser.close().catch(() => {});
  }
}

main()
  .catch((err) => {
    result.setup_error = { category: 'runner', message: firstLine(err) };
  })
  .finally(() => {
    fs.writeFileSync(resultPath, JSON.stringify(result));
  });
