// The Sammy web app: chat with Sammy, watch its browser, take over when it asks. No build step, no framework.
//
// Staying correct while the user moves around: `newPage()` tears down everything that belongs to what is on screen.
// It aborts `page`, whose signal every read (`api()` GET) uses, and closes the run's event stream and the browser
// screenshots, so an old chat can never draw over a new one. The user's own actions (sending, answering,
// stopping) are not tied to it: they always finish.
//
// Telemetry: `telemetry` records the user's actions, and does nothing unless the server sends telemetry to Logfire.
// Once signed in, `startTelemetry()` asks the server, and only then loads `telemetry.js`, which says what is sent.
'use strict';

const $ = (id) => document.getElementById(id);
const ACTIVE = ['queued', 'running', 'waiting'];
const WORKING = ['queued', 'running'];
const TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;
const desktop = window.matchMedia('(min-width: 900px)');

const state = {
  threadId: null,  // the open chat, or null for a new one
  run: null,  // the open chat's latest run, as the server last described it
  draft: null,  // the running run's live draft: { text, activity }
  draftLost: false,  // the live connection dropped, so the draft may be behind
  chatLoads: 0,  // numbers each chat load, so only the latest one is drawn
  threadLoads: 0,  // the same for the chat list
  takeoverAskId: null,  // the hand-off whose live view is open
  chatShown: false,  // the open chat has been drawn at least once
  browserClosed: false,  // the user closed the browser panel in this chat, so it does not open by itself again
  threadsShown: '',  // the chat list as last drawn, so an unchanged list is not redrawn under the user's focus
  signingUp: false,
  pushKey: null,  // the server's web push key, or null when it sends no notifications
  offlineNoticed: false,  // a background refresh has told the user they are offline, since the server last answered
  takeoverEnd: null,  // ends the open live view's span
  takeoverClosedBy: null,  // why the app closed the live view, for that span
  serverName: null,  // a name for the add-server form, from a chat's "Add an MCP server"
  pending: [],  // the files in the composer, for the next message: see `addFiles`
};
let page = new AbortController();
let events = null;  // the open run's EventSource
let watching = null;  // the AbortController of the screenshot loop, while it runs

function newPage() {
  page.abort();
  page = new AbortController();
  closeEvents();
  stopWatching();
  closeTakeover('left the chat');
  hideNotice();
}

// --- telemetry ---

const NO_TELEMETRY = {
  span: async (name, attributes, action) => action(() => {}),  // traces `action(note)`; returns what it does
  begin: () => () => {},  // starts a span; returns what ends it
  log: () => {},
  error: () => {},
};
const telemetry = { ...NO_TELEMETRY };
let telemetryRun = null;  // once started: the started telemetry's stop function, or null when it is off
let telemetryUser = null;  // whose telemetry is running
let telemetryStopping = Promise.resolve();

function startTelemetry(userId) {
  // In the background, and never failing: the app works the same without it. It keeps running when a session ends
  // (its last export would be refused, and the SDK cannot start again after that), and starts again for another user.
  if (telemetryRun && telemetryUser === userId) return telemetryRun;
  if (telemetryRun) stopTelemetry();  // signed in again, as someone else: the new session sends what is left
  telemetryUser = userId;
  telemetryRun = telemetryStopping.then(async () => {
    const response = await fetch('/api/telemetry', { credentials: 'same-origin' });
    const settings = response.ok ? await response.json() : null;
    if (!settings || !settings.enabled) return null;
    const module = await import('/static/telemetry.js');
    const stop = module.start(settings, { id: userId });
    Object.assign(telemetry, module.facade);
    return stop;
  }).catch((error) => {
    console.warn('Telemetry is off:', error);
    return null;
  });
  return telemetryRun;
}

function stopTelemetry() {
  // Sends what is left. Resolves once done; never fails.
  const run = telemetryRun;
  telemetryRun = null;
  telemetryUser = null;
  if (!run) return telemetryStopping;
  telemetryStopping = run.then(async (stop) => {
    Object.assign(telemetry, NO_TELEMETRY);
    if (stop) await stop();
  }).catch((error) => console.warn('Telemetry did not stop cleanly:', error));
  return telemetryStopping;
}

async function api(path, { method = 'GET', body } = {}) {
  // Reads belong to the page on screen; actions always finish.
  const init = { method, credentials: 'same-origin', headers: {}, signal: method === 'GET' ? page.signal : undefined };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    throw offlineError(error);
  }
  // A proxy in front of a server that is down or restarting answers for it: that is not reaching Sammy either.
  if ([502, 503, 504].includes(response.status)) throw offlineError(new Error('gateway'));
  if (state.offlineNoticed) {  // the server answers again: take back the offline notice, and say it again next time
    state.offlineNoticed = false;
    telemetry.log('back online');
    if ($('notice-text').textContent === offlineError(new Error()).message) hideNotice();
  }
  let data = null;
  if ((response.headers.get('Content-Type') || '').includes('application/json')) {
    try {
      data = await response.json();
    } catch (error) {
      if (error.name !== 'SyntaxError') throw offlineError(error);  // the connection dropped mid-reply
      if (response.ok) throw new Error('Sammy sent a reply this page could not read. Reload to see where things stand.');
    }
  }
  if (response.status === 401 && !['/api/signin', '/api/me'].includes(path)) signedOut();
  if (!response.ok) {
    const error = new Error(problem(response.status, data && typeof data.detail === 'string' ? data.detail : null));
    error.status = response.status;
    throw error;
  }
  return data;
}

function offlineError(error) {
  // The browser's own words for a failed request ("Failed to fetch", ...) mean nothing to the user.
  if (error.name === 'AbortError') return error;
  const offline = new Error('Could not reach Sammy. Check your connection, and try again.');
  offline.offline = true;
  return offline;
}

function problem(status, detail) {
  // The server's words where they are meant for the user; plain ones where they are not.
  if (status === 404) return 'That is no longer here. It may have been deleted, or the task may have finished.';
  if (status === 422) return 'Please check what you typed, and try again.';
  return detail || `Something went wrong (${status}). Please try again.`;
}

function showError(error) {
  if (error.name === 'AbortError') return;  // a request of a page the user has left
  console.error(error);
  telemetry.error(error);
  showNotice(error.message);
}

function report(promise) {
  // For event handlers: show what went wrong.
  promise.catch(showError);
}

function reportUnlessOffline(promise) {
  // For refreshes nobody asked for, which retry by themselves: being offline is said once (until the server is
  // reached again), not on every retry, so a dismissed notice stays dismissed.
  promise.catch((error) => {
    if (!error.offline) showError(error);
    else if (!state.offlineNoticed) {
      state.offlineNoticed = true;
      telemetry.log('offline notice', {}, 'warning');
      showError(error);
    }
  });
}

function showNotice(text) {
  $('notice-text').textContent = text;
  $('notice').hidden = false;
}
function hideNotice() { $('notice').hidden = true; }
$('close-notice').addEventListener('click', hideNotice);
document.querySelector('.skip-link').addEventListener('click', (event) => {
  event.preventDefault();  // a #message hash would be routed as a page
  $('message').focus();
});

function element(tag, text = '', className = '') {
  const made = document.createElement(tag);
  if (text) made.textContent = text;
  if (className) made.className = className;
  return made;
}

function button(text, className, onClick) {
  // Disabled while its action runs; a failure is shown to the user.
  const made = element('button', text, className);
  made.type = 'button';
  made.addEventListener('click', () => {
    made.disabled = true;
    const done = Promise.resolve().then(onClick).finally(() => { made.disabled = false; });
    report(done);
  });
  return made;
}

// --- signing in and out ---

function show(screen) {
  for (const id of ['signin', 'main']) $(id).hidden = id !== screen;
}

function signedOut() {
  newPage();
  state.threadId = null;
  state.run = null;
  state.threadsShown = '';
  show('signin');
}

$('signup-button').addEventListener('click', () => {
  state.signingUp = !state.signingUp;
  $('signin-button').textContent = state.signingUp ? 'Create account' : 'Sign in';
  $('signup-button').textContent = state.signingUp ? 'I have an account' : 'Create an account';
  $('password').autocomplete = state.signingUp ? 'new-password' : 'current-password';
  $('auth-title').textContent = state.signingUp ? 'Make room for Sammy' : 'Welcome back';
  $('auth-description').textContent = state.signingUp ? 'Create an account to start your first chat.' : 'Sign in to pick up where you left off.';
  $('signin-error').textContent = '';
  $('email').focus();
});

$('signin-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('signin-error').textContent = '';
  $('signin-button').disabled = true;
  $('signup-button').disabled = true;
  const signedIn = state.signingUp ? 'signed up' : 'signed in';
  const began = performance.now();
  try {
    await api(state.signingUp ? '/api/signup' : '/api/signin', {
      method: 'POST', body: { email: $('email').value, password: $('password').value },
    });
  } catch (error) {
    $('signin-error').textContent = error.message;
    return;
  } finally {
    $('signin-button').disabled = false;
    $('signup-button').disabled = false;
  }
  const took = performance.now() - began;
  try {
    $('password').value = '';  // not left filled in for whoever sees the sign-in screen next
    await start();
  } catch (error) {
    showError(error);  // signed in, but the first chat or list did not load: a notice on the page that opened
    return;
  } finally {
    // Telemetry starts once signed in, so signing in is recorded afterwards. No email, and never the password.
    if (telemetryRun) telemetryRun.then(() => telemetry.log(signedIn, { duration_ms: Math.round(took) }));
  }
  if (!$('signin').hidden && !$('signin-error').textContent) {
    // Signed in, yet still signed out: the browser did not keep the session cookie. The account exists now either way.
    if (state.signingUp) $('signup-button').click();  // back to signing in
    $('signin-error').textContent = 'You were signed in, but this browser did not save your sign-in. Allow cookies for this site, then sign in.';
  }
});

$('signout').addEventListener('click', () => report(signOut()));

async function signOut() {
  await telemetry.span('sign out', {}, async () => {
    try {
      await stopNotifications();  // while still signed in: removing this browser's subscription needs the session
    } catch (error) {
      console.error(error);  // signing out matters more than the push subscription
    }
    $('notifications-label').textContent = 'Notify me when Sammy needs me';  // this browser's subscription is gone
  });
  // What telemetry has left goes while the session still lets it, unless that takes long.
  const userId = telemetryUser;
  await Promise.race([stopTelemetry(), new Promise((resolve) => setTimeout(resolve, 2000))]);
  try {
    await api('/api/signout', { method: 'POST', body: {} });  // first: a failed sign-out must not look like one
  } catch (error) {
    if (userId) startTelemetry(userId);  // still signed in
    throw error;
  }
  signedOut();
  location.hash = '';
  location.reload();
}

// --- the chat list ---

async function loadThreads({ refresh = false } = {}) {
  // `refresh`: the background refresh, which telemetry leaves out (`telemetry.js`): it is polling, not the user.
  const load = ++state.threadLoads;
  const threads = await api(refresh ? '/api/threads?refresh' : '/api/threads');
  if (load !== state.threadLoads) return;  // a later load is drawing the list
  // The open chat catches up when the list knows better: a run started elsewhere (a schedule, another tab), or
  // finished while the chat's event stream was down for good (an HTTP error closes it; only a reload reopens it).
  const open = $('layout').hidden ? null : threads.find((thread) => thread.id === state.threadId);
  const shownWorking = Boolean(state.run && ACTIVE.includes(state.run.status));
  const streamDown = !events || events.readyState === EventSource.CLOSED;
  if (open && (Boolean(open.status) !== shownWorking || (shownWorking && streamDown))) reportUnlessOffline(loadChat());
  const currentId = $('layout').hidden ? null : state.threadId;  // on Files or Schedules no chat is the current page
  const shown = JSON.stringify([currentId, threads]);
  if (shown === state.threadsShown) return;
  state.threadsShown = shown;
  const focusedId = $('threads').contains(document.activeElement) ? document.activeElement.dataset.id : null;
  const badges = { waiting: 'Needs you', running: 'Working', queued: 'Working' };
  $('threads').replaceChildren(...threads.map((thread) => {
    const open = element('button', '', thread.id === currentId ? 'current' : '');
    open.dataset.id = thread.id;
    open.title = thread.title || 'Untitled';
    open.append(element('span', thread.title || 'Untitled', 'thread-title'));
    if (badges[thread.status]) open.append(element('span', badges[thread.status], `badge ${thread.status}`));
    if (thread.id === currentId) open.setAttribute('aria-current', 'page');
    open.addEventListener('click', () => {
      if (location.hash === `#/t/${thread.id}`) report(route());  // the same chat: load it again
      else location.hash = `#/t/${thread.id}`;
      closeDrawer();
    });
    const item = element('li');
    item.append(open);
    return item;
  }));
  if (!threads.length) $('threads').append(element('li', 'Your next task starts with a new chat.', 'thread-empty'));
  const focused = focusedId && $('threads').querySelector(`[data-id="${focusedId}"]`);
  if (focused) focused.focus();  // the redraw replaced the button the user was on
}

setInterval(() => {
  // Another chat may start needing the user at any time.
  if (!$('main').hidden && document.visibilityState === 'visible') reportUnlessOffline(loadThreads({ refresh: true }));
}, 15000);

// --- the drawer, on small screens ---

function syncDrawer() {
  const open = !desktop.matches && $('drawer').classList.contains('open');
  $('drawer').inert = !desktop.matches && !open;
  $('drawer-backdrop').hidden = !open;
  $('menu-button').setAttribute('aria-expanded', String(open));
  for (const id of ['layout', 'signins', 'integrations', 'schedules', 'browser-button']) $(id).inert = open;
}
function closeDrawer(restoreFocus = false) {
  const focusInside = $('drawer').contains(document.activeElement);
  $('drawer').classList.remove('open');
  syncDrawer();
  if (!desktop.matches) {
    if (restoreFocus) $('menu-button').focus(); else if (focusInside) $('message').focus();
  }
}
$('menu-button').addEventListener('click', () => {
  const open = $('drawer').classList.toggle('open');
  syncDrawer();
  if (open) $('close-drawer').focus();
});
$('close-drawer').addEventListener('click', () => closeDrawer(true));
$('drawer-backdrop').addEventListener('click', () => closeDrawer(true));
desktop.addEventListener('change', () => { closeDrawer(); updateBrowserButton(); });
document.addEventListener('keydown', (event) => {
  // On small screens the drawer and the browser cover the page: Escape closes them and Tab stays inside.
  if (desktop.matches) return;
  const drawerOpen = $('drawer').classList.contains('open');
  const browserOpen = !$('browser').hidden && !$('layout').hidden;
  if (!drawerOpen && !browserOpen) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    if (drawerOpen) closeDrawer(true); else $('close-browser').click();
  }
  if (event.key === 'Tab') {
    const panel = drawerOpen ? $('drawer') : $('browser');
    const controls = [...panel.querySelectorAll('button:not(:disabled)')];
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
syncDrawer();
$('new-chat').addEventListener('click', () => {
  telemetry.log('new chat');
  location.hash = '#/new';
  closeDrawer();
});

