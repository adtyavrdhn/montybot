// The web app's own telemetry: what the user did and how the app answered, sent to Logfire through the server
// (`/api/telemetry/v1/...` adds the server's token, so none is in the page). app.js loads this module only when
// `GET /api/telemetry` says the server sends telemetry; otherwise neither it nor the SDK in `vendor/` is loaded.
//
// Traced: page load and its resources (in summary), the API requests (each carries `traceparent`, so the server's
// spans for it join the trace), clicks and submits, Web Vitals (as spans and metrics), long animation frames, the
// browser session and the app's route, and the actions app.js names (`span`, `begin`, `log`, `error`).
//
// Forwarded data skips the server's scrubbing, so this module keeps the lines of `montybot/observability.py` itself:
// - Addresses are only route templates (`clean` below): no query or fragment, ids as `{thread_id}` and so on,
//   anything under `/live/` (hand-off links) as `/live/*`. Request and response bodies and headers are never read.
// - The user is only their id: never their email or name.
// - Content (messages, answers, prompts, error messages) only with `include_content`; otherwise its length or type.
// Passwords, cookies, push subscriptions and file names are never given to this module, and the live view is
// another page this one does not trace.
import * as logfire from './vendor/logfire-browser.js';

const CONTENT = ['text', 'answer', 'reason', 'prompt', 'error_message'];
const PARAMETERS = {
  threads: 'thread_id', runs: 'run_id', asks: 'ask_id', schedules: 'schedule_id', memories: 'memory_id', 'sign-ins': 'site',
};
// Under /api/integrations/: the app after `apps` (but not the word `accounts`), and the ids after `accounts` and
// `servers`. Composio's account ids look like words (`ca_OmfoGFIzpmEu`), so they are named, not guessed at.
const INTEGRATIONS = { apps: 'app', accounts: 'account_id', servers: 'server_id' };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TOKEN = /^(?=.*\d)[^/]{16,}$|^[^/]{32,}$/;  // long, and with a digit or very long: an id of some kind
const URLISH = /\b(?:https?:\/\/|blob:|data:)[^\s"'<>`()]+|(?<![\w/.])\/(?:api|live|static)\/[^\s"'<>`()]*/g;
const URL_KEYS = ['http.url', 'url.full', 'http.referrer', 'logfire.page.url.full'];
const PATH_KEYS = ['http.target', 'url.path', 'logfire.page.url.path'];
const DROPPED_KEYS = ['url.query', 'url.fragment'];
const PAGES = {
  '': '/new', '#': '/new', '#/new': '/new', '#/sign-ins': '/sign-ins', '#/integrations': '/integrations',
  '#/schedules': '/schedules',
};
const ERROR_LEVEL = 17;  // Logfire's `error`

let includeContent = false;

// --- addresses as route templates ---

export function routePath(path) {
  path = path.split(/[?#]/)[0];
  if (/^\/live(\/|$)/.test(path)) return '/live/*';  // hand-off ids and links
  const parts = path.split('/');
  const integrations = parts[1] === 'api' && parts[2] === 'integrations';
  return parts.map((part, i) => {
    const name = (parts[i - 2] === 'api' && PARAMETERS[parts[i - 1]])
      || (integrations && i > 3 && part !== 'accounts' && INTEGRATIONS[parts[i - 1]]);
    if (part && name) return `{${name}}`;
    return UUID.test(part) || TOKEN.test(part) ? '{id}' : part;
  }).join('/');
}

export function routeUrl(address) {
  let url;
  try {
    url = new URL(address, location.href);
  } catch {
    return '{url}';
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return url.protocol;  // blob:, data:, about:
  return url.origin + routePath(url.pathname);
}

function scrubText(text) {
  // Addresses inside free text: error messages and stack traces, span names.
  return text.replace(URLISH, (found) => (found.startsWith('/') ? routePath(found) : routeUrl(found)));
}

function routeName() {
  // The app's screens (app.js `route()`), as templates.
  const hash = location.hash;
  if (hash in PAGES) return PAGES[hash];
  return /^#\/t\/[0-9a-f-]{36}$/.test(hash) ? '/t/{thread_id}' : '/*';
}

function cleanAttributes(attributes) {
  for (const [key, value] of Object.entries(attributes || {})) {
    if (DROPPED_KEYS.includes(key)) delete attributes[key];
    else if (typeof value !== 'string') continue;
    else if (PATH_KEYS.includes(key)) attributes[key] = routePath(value);
    else if (URL_KEYS.includes(key) || /^(?:https?:|blob:|data:)/.test(value)) attributes[key] = routeUrl(value);
    else if (key.startsWith('exception.') || value.includes('/live/')) attributes[key] = scrubText(value);
  }
}

function clean(span) {
  cleanAttributes(span.attributes);
  for (const event of span.events || []) cleanAttributes(event.attributes);
  if (span.status && typeof span.status.message === 'string') span.status.message = scrubText(span.status.message);
  // A fetch span is named by its method alone: add the request's route, unless Logfire does (from `http.url`).
  const address = span.attributes['url.full'];
  const named = /^[A-Z]+$/.test(span.name) && typeof address === 'string' && !('http.url' in span.attributes);
  const name = named ? `${span.name} ${routePath(new URL(address, location.href).pathname)}` : scrubText(span.name);
  if (name !== span.name) Reflect.set(span, 'name', name);
}

// Runs on every span, before export: when it starts, and again when it ends, as instrumentations add attributes late.
// A span processor of its own, so it also covers the SDK's and the instrumentations' spans.
const cleaner = {
  onStart: (span) => quietly(() => clean(span)),
  onEnd: (span) => quietly(() => clean(span)),
  forceFlush: async () => {},
  shutdown: async () => {},
};

// --- what app.js records ---

function attributesOf(given) {
  // Content only when the server includes it; otherwise how long it was. No empty values.
  const attributes = {};
  for (const [key, value] of Object.entries(given || {})) {
    if (value === undefined || value === null) continue;
    if (CONTENT.includes(key) && !includeContent) {
      if (typeof value === 'string') attributes[`${key}_length`] = value.length;
    } else {
      attributes[key] = value;
    }
  }
  return attributes;
}

function quietly(record) {
  // Telemetry never gets in the user's way.
  try {
    record();
  } catch (error) {
    console.warn('telemetry:', error);
  }
}

function describeError(error) {
  // What may be said about an error: its type and status always, its message only as content.
  if (!(error instanceof Error)) return { 'error.type': typeof error, error_message: String(error) };
  return {
    'error.type': error.name || 'Error',
    'http.response.status_code': error.status,
    offline: error.offline,
    error_message: error.message,
  };
}

function markFailed(span, error) {
  if (error && error.name === 'AbortError') {  // the user left the page: not a failure
    span.setAttribute('outcome', 'aborted');
    return;
  }
  const described = describeError(error);
  span.setAttributes(attributesOf(described));
  span.setAttribute('logfire.level_num', ERROR_LEVEL);
  span.setStatus({ code: 2, message: includeContent ? scrubText(described.error_message) : described['error.type'] });
}

function span(name, attributes, action) {
  // Traces an action of the user's from start to end. `action(note)` runs at once, inside the span, so its first
  // request is the span's child; `note({...})` adds attributes learned on the way. Its result or error is the
  // caller's, as without telemetry.
  let outcome;
  quietly(() => logfire.span(name, {
    attributes: attributesOf(attributes),
    callback: (active) => {
      const note = (more) => quietly(() => active.setAttributes(attributesOf(more)));
      try {
        outcome = Promise.resolve(action(note));
      } catch (error) {
        outcome = Promise.reject(error);
      }
      // Logfire would record the error's message: this span says only what `markFailed` allows.
      return outcome.catch((error) => quietly(() => markFailed(active, error)));
    },
  }));
  return outcome || (async () => action(() => {}))();  // tracing failed before the action started
}

function begin(name, attributes) {
  // A span for something that lasts (an open live view): its duration is the time until the returned `end()`.
  let started = null;
  quietly(() => { started = logfire.startSpan(name, attributesOf(attributes)); });
  return (more) => quietly(() => {
    if (!started) return;
    started.setAttributes(attributesOf(more));
    started.end();
    started = null;
  });
}

function log(name, attributes, level = 'info') {
  quietly(() => logfire[level](name, attributesOf(attributes)));
}

function redacted(error) {
  // The error without its message: its type and where it was thrown.
  const safe = new Error('');
  safe.name = error instanceof Error ? error.name || 'Error' : typeof error;
  const frames = error instanceof Error && typeof error.stack === 'string' ? error.stack.split('\n').filter((line) => /^\s+at /.test(line)) : [];
  safe.stack = [safe.name, ...frames].join('\n');
  return safe;
}

function error(error, message = 'error shown to the user', attributes = {}) {
  if (error && error.name === 'AbortError') return;
  quietly(() => {
    const { error_message: _, ...described } = describeError(error);
    logfire.reportError(message, includeContent ? error : redacted(error), attributesOf({ ...described, ...attributes }));
  });
}

export const facade = { span, begin, log, error };

// --- starting and stopping ---

export function start(settings, user) {
  // Returns what stops it: removes its listeners, sends what is left, and shuts the SDK down.
  includeContent = Boolean(settings.include_content);
  const endpoint = (signal) => new URL(`/api/telemetry/v1/${signal}`, location.origin).href;  // the exporter needs it absolute
  // Its own requests, and polling (a screenshot a second, the chat list's refresh): neither is the user's doing, and the
  // server's spans for them would be noise.
  const ignored = [/\/api\/telemetry(\/|$)/, /\/api\/runs\/[^/]+\/screen$/, /\/api\/threads\?refresh$/];
  const shutdown = logfire.configure({
    traceUrl: endpoint('traces'),
    metrics: { metricUrl: endpoint('metrics') },
    serviceName: 'montybot-web',
    serviceVersion: settings.version || undefined,
    environment: settings.environment || undefined,
    autoInstrumentations: {
      '@opentelemetry/instrumentation-fetch': { ignoreUrls: ignored },
      '@opentelemetry/instrumentation-xml-http-request': { ignoreUrls: ignored },
      '@opentelemetry/instrumentation-user-interaction': { eventNames: ['click', 'submit'] },
    },
    resourceTiming: { detail: 'summary' },
    rum: {
      session: {
        getRouteName: routeName,
        getUser: () => (user.id ? { id: user.id } : undefined),  // the opaque id only: never email or name
        urlAttributes: (url) => ({ full: url.origin + routePath(url.pathname), path: routePath(url.pathname) }),
      },
      webVitals: { metrics: true },
      longAnimationFrames: { sessionSampleRate: 1 },
    },
    spanProcessors: [cleaner],
  });
  const uncaught = (event) => error(event.error || event.message, 'uncaught error');
  const unhandled = (event) => error(event.reason, 'unhandled rejection');
  window.addEventListener('error', uncaught);
  window.addEventListener('unhandledrejection', unhandled);
  return async () => {
    window.removeEventListener('error', uncaught);
    window.removeEventListener('unhandledrejection', unhandled);
    try {
      await shutdown();
    } finally {
      sessionStorage.removeItem('lf_browser_session');  // the next user of this tab is a new browser session
    }
  };
}
