// The Monty web app: chat with Monty, watch its browser, take over when it asks. No build step, no framework.
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
  // A proxy in front of a server that is down or restarting answers for it: that is not reaching Monty either.
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
      if (response.ok) throw new Error('Monty sent a reply this page could not read. Reload to see where things stand.');
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
  const offline = new Error('Could not reach Monty. Check your connection, and try again.');
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
  $('auth-title').textContent = state.signingUp ? 'Make room for Monty' : 'Welcome back';
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
    $('notifications-label').textContent = 'Notify me when Monty needs me';  // this browser's subscription is gone
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
  for (const id of ['layout', 'files', 'signins', 'integrations', 'schedules', 'browser-button']) $(id).inert = open;
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

// --- a chat ---

function messageBubble(role, text) {
  // role: 'user', 'assistant', or 'event' (a line recording an approval or a hand-off)
  const bubble = element('div', '', `msg ${role}`);
  if (role === 'assistant') bubble.append(renderMarkdown(text)); else bubble.textContent = text;
  return bubble;
}

function emptyChat() {
  const empty = element('div', '', 'empty');
  empty.innerHTML = '<span class="monty-mark" aria-hidden="true">m<span>•</span></span>' +
    '<h2>What can I take off your list?</h2>' +
    '<p>Give me a task on the web. I’ll work in my browser and ask when I need your help.</p>';
  const suggestions = element('div', '', 'suggestions');
  const examples = [
    ['↗', 'Find something worth the trip', 'Find the three cheapest flights to Lisbon next Friday.'],
    ['▣', 'Pick up where you left off', 'Check the status of my last order on the shop.'],
    ['◷', 'Make it a regular thing', 'Every Tuesday at 9, check the price of my usual shopping list.'],
  ];
  for (const [icon, title, text] of examples) {
    const suggestion = button('', 'suggestion', () => { $('message').value = text; $('message').focus(); });
    const symbol = element('span', icon);
    symbol.setAttribute('aria-hidden', 'true');
    const copy = element('span');
    copy.append(element('strong', title), element('small', text));
    suggestion.append(symbol, copy);
    suggestions.append(suggestion);
  }
  empty.append(suggestions);
  return empty;
}

async function openChat(threadId) {
  newPage();
  state.threadId = threadId;
  state.draft = null;
  state.draftLost = false;
  state.browserClosed = false;
  state.chatShown = false;
  if (threadId === null) {
    $('title').textContent = 'New chat';
    $('messages').replaceChildren(emptyChat());
    renderRun(null);
    return;
  }
  $('title').textContent = 'Loading…';
  $('messages').replaceChildren();  // never the last chat's messages under this chat's address
  renderRun(null);
  $('send').disabled = true;  // until the chat has loaded and says whether it is still working
  await loadChat();
}

async function loadChat() {
  const load = ++state.chatLoads;
  let thread;
  try {
    thread = await api(`/api/threads/${state.threadId}`);
  } catch (error) {
    if (error.status === 404) { location.hash = '#/new'; return; }
    if (load === state.chatLoads && error.name !== 'AbortError' && !state.chatShown) {
      $('title').textContent = 'Could not load this chat';  // the newest load failed: say so, and let the user act
      $('send').disabled = false;
    }
    throw error;
  }
  if (load !== state.chatLoads) return;  // a later load is drawing this chat
  state.chatShown = true;
  $('title').textContent = thread.title || 'Monty';
  const box = $('messages');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.replaceChildren(...thread.messages.map((m) => messageBubble(m.role, m.text)));
  if (!thread.messages.length) {  // a scheduled task's chat, before its first run
    box.append(element('p', 'Nothing here yet. Each time this scheduled task runs, what Monty did shows up here.', 'empty-chat'));
  }
  renderRun(thread.run);
  renderDraft();
  if (atBottom) box.scrollTop = box.scrollHeight;
  follow(thread.run);
}

function renderRun(run) {
  state.run = run;
  const active = Boolean(run && ACTIVE.includes(run.status));
  const working = Boolean(run && WORKING.includes(run.status));
  $('send').hidden = active;
  $('send').disabled = false;
  $('stop').hidden = !active;
  $('stop').disabled = false;
  $('message').placeholder = !active ? 'What would you like Monty to do?'
    : run.status === 'waiting' ? 'Monty is waiting for you: answer above.' : 'Monty is on it. Stop it, or wait to send your next message.';
  $('status').hidden = !working;
  renderStatus();
  renderAsk(run && run.status === 'waiting' ? run.ask : null);
  if (working && run.activity.length) startWatching(); else stopWatching();
  updateBrowserButton();
}

function renderStatus() {
  const run = state.run;
  if (!run || !WORKING.includes(run.status)) return;
  // The draft's activity while the model thinks or writes; the run's own log ("Opening example.com") while it acts.
  const logged = run.activity.length ? run.activity[run.activity.length - 1] : 'Working…';
  const now = (state.draft && state.draft.activity) || logged;
  $('status').textContent = state.draftLost ? `${now} · Reconnecting…` : now;
}

function renderDraft() {
  // The reply as it is written. It is replaced by the saved reply when the run finishes.
  const old = $('messages').querySelector('.msg.draft');
  if (old) old.remove();
  if (!state.draft || !state.draft.text) return;
  const box = $('messages');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const bubble = messageBubble('assistant', state.draft.text);
  bubble.classList.add('draft');
  if (state.draftLost) bubble.prepend(element('small', 'Connection lost; this may be incomplete'));
  box.append(bubble);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

// --- following a run as it works: the server's event stream ---

function follow(run) {
  if (!run || !ACTIVE.includes(run.status)) { closeEvents(); return; }
  if (events && events.runId === run.id && events.readyState !== EventSource.CLOSED) return;
  closeEvents();
  const source = new EventSource(`/api/runs/${run.id}/events`);
  source.runId = run.id;
  events = source;
  const live = () => events === source;  // events may still arrive after it closed
  const ids = { run_id: run.id, thread_id: state.threadId };
  source.onopen = () => {
    if (!live()) return;
    telemetry.log(source.opened ? 'run events reconnected' : 'run events opened', { ...ids, after_lost: state.draftLost });
    source.opened = true;
    state.draftLost = false;
    renderStatus();
    renderDraft();
  };
  source.addEventListener('preview', (event) => {
    if (!live()) return;
    const draft = JSON.parse(event.data);
    if (!Number.isInteger(draft.revision) || typeof draft.text !== 'string' || typeof draft.activity !== 'string') return;
    state.draft = { text: draft.text, activity: draft.activity };
    renderDraft();
    renderStatus();
  });
  source.addEventListener('status', (event) => {
    if (!live()) return;
    const status = JSON.parse(event.data);
    if (status.id !== run.id || status.thread_id !== state.threadId) return;
    const before = state.run;
    if (!before || before.status !== status.status || (before.ask && before.ask.id) !== (status.ask && status.ask.id)) {
      telemetry.log('run status', {
        ...ids, status: status.status, previous_status: before && before.status, ask_id: status.ask && status.ask.id,
      });
    }
    renderRun(status);
    report(loadThreads());
    if (!ACTIVE.includes(status.status)) {
      telemetry.log('run events ended', { ...ids, status: status.status });
      closeEvents();  // and the draft: the saved reply replaces it
      report(loadChat());
    }
  });
  source.onerror = () => {
    if (!live()) return;
    // EventSource reconnects by itself, and the server ends each stream every few minutes on purpose. Only if it is
    // still not back after a moment: warn, and reload the chat (which says whether the run ended or the user was
    // signed out).
    if (source.lostTimer) return;  // one check at a time, however often the browser retries
    source.lostTimer = setTimeout(() => {
      source.lostTimer = null;
      if (!live() || source.readyState === EventSource.OPEN) return;
      telemetry.log('run events lost', ids, 'warning');
      state.draftLost = true;
      renderStatus();
      renderDraft();
      reportUnlessOffline(loadChat());
    }, 2000);
  };
}

function closeEvents() {
  if (events) events.close();
  events = null;
  state.draft = null;
  state.draftLost = false;
  renderDraft();
}

// --- stopping a run ---

$('stop').addEventListener('click', () => {
  const run = state.run;
  if (!run) return;
  $('stop').disabled = true;
  const before = page;
  report(telemetry.span('stop run', { run_id: run.id, thread_id: state.threadId }, (note) => (
    api(`/api/runs/${run.id}/stop`, { method: 'POST', body: {} })
      .catch((error) => {
        if (error.status !== 409) throw error;
        note({ outcome: 'finished already' });  // it finished meanwhile
      })))
    .then(() => { if (page === before) return Promise.all([loadChat(), loadThreads()]); })
    .finally(() => { $('stop').disabled = false; }));
});

// --- what the bot asks ---

function renderAsk(ask) {
  const box = $('ask');
  if (!ask || ask.id !== state.takeoverAskId) closeTakeover('hand-off over');  // that hand-off is over
  if (ask === null) {
    box.hidden = true;
    box.dataset.id = '';
    return;
  }
  if (box.dataset.id === ask.id) return;  // already shown: keep what the user is typing
  box.dataset.id = ask.id;
  box.hidden = false;
  telemetry.log('ask shown', { ...askIds(ask), prompt: ask.prompt });
  const row = element('div', '', 'row');
  if (ask.kind === 'question') {
    const input = element('textarea');
    input.rows = 2;
    input.maxLength = 20000;
    input.placeholder = 'Your answer';
    input.setAttribute('aria-label', 'Your answer to Monty');
    row.append(input, button('Answer', '', async () => {
      if (!input.value.trim()) { input.focus(); return; }
      await answer(ask, { text: input.value });
    }));
  } else if (ask.kind === 'approval') {
    row.append(
      button('Approve', 'good', () => answer(ask, { approved: true })),
      button('Deny', 'bad', () => answer(ask, { approved: false, reason: 'the user said no' })),
    );
  } else if (ask.kind === 'connect') {
    box.replaceChildren(...connectCard(ask));
    return;
  } else {
    row.append(button('Take over the browser', '', () => takeOver(ask)));
  }
  box.replaceChildren(element('p', ask.prompt), row);  // the server's push tells the user if they are away
}

function connectCard(ask) {
  // Monty needs a service connected. The server answers the ask by itself once the sign-in completes; "I've connected
  // it" is for when that page never came back, and the run checks for itself either way.
  const offered = ask.integration || {};
  const name = offered.name || 'the app';
  const head = element('div', '', 'connect-head');
  head.append(logo(offered.logo, name), element('strong', `Connect ${name}`));
  const row = element('div', '', 'row');
  const note = element('p', '', 'connect-note');
  const notNow = button('Not now', 'secondary', () => answer(ask, { connected: false }));
  const done = button("I've connected it", 'secondary', () => answer(ask, { connected: true }));
  done.hidden = true;
  const started = (text) => { done.hidden = false; note.textContent = text; };
  if (offered.provider === 'mcp' && offered.url) {
    row.append(button(`Connect ${name}`, 'good', async () => {
      const signingIn = await connectPreset(offered);
      started(signingIn ? `Finish signing in to ${name} in the window that opened. Monty carries on once you have.`
        : `${name} is connected.`);
    }));
  } else if (offered.provider === 'composio') {
    row.append(button(`Connect ${name}`, 'good', async () => {
      await openSignIn('connect app', { app: offered.key }, () => (
        api(`/api/integrations/apps/${encodeURIComponent(offered.key)}/connect`, { method: 'POST', body: {} })));
      started(`Finish signing in to ${name} in the window that opened. Monty carries on once you have.`);
    }));
  } else if (offered.server_id) {
    row.append(button(`Sign in to ${name}`, 'good', async () => {
      await openSignIn('sign in to mcp server', {}, () => (
        api(`/api/integrations/servers/${offered.server_id}/sign-in`, { method: 'POST', body: {} })));
      started(`Finish signing in to ${name} in the window that opened. Monty carries on once you have.`);
    }));
  } else {
    note.textContent = `${name} isn't one of the apps Monty connects in one click. If it has an MCP server, add it in Integrations.`;
    row.append(button('Add an MCP server', 'good', () => {
      state.serverName = name;  // the form starts with it
      location.hash = '#/integrations';
    }));
  }
  row.append(done, notNow);
  return [head, element('p', ask.prompt), note, row];
}

function logo(url, name) {
  // The app's logo, or its first letter while there is none (or it fails to load).
  const mark = element('span', (name || '?').trim().charAt(0).toUpperCase(), 'app-logo');
  mark.setAttribute('aria-hidden', 'true');
  if (!url) return mark;
  const image = element('img');
  image.src = url;
  image.alt = '';
  image.loading = 'lazy';
  image.referrerPolicy = 'no-referrer';
  image.addEventListener('error', () => image.remove());
  mark.append(image);
  return mark;
}

async function openSignIn(action, attributes, request) {
  // A sign-in happens in a window of its own. It is opened now, in the click, as a window opened after the request
  // would be blocked; and it is cut from this page (`opener`), as the sign-in pages are other sites. The page they
  // come back to tells this one on a BroadcastChannel. No address means nothing to sign in to (a server that is
  // ready as added): the window closes, and this page shows what changed. Says whether a sign-in opened.
  const popup = window.open('about:blank', '_blank');
  if (popup) popup.opener = null;
  let link;
  try {
    link = await telemetry.span(action, attributes, request);
  } catch (error) {
    if (popup) popup.close();
    throw error;
  }
  if (!link.url) {
    if (popup) popup.close();
    await signInsChanged();
    return false;
  }
  const target = new URL(link.url, location.href);
  if (!['http:', 'https:'].includes(target.protocol)) {  // never a javascript: or other address, whoever sent it
    if (popup) popup.close();
    throw new Error('Monty sent a sign-in address this page cannot open.');
  }
  if (popup) popup.location.href = target.href; else location.assign(target.href);  // pop-ups blocked: go there instead
  return true;
}

async function connectPreset(listed) {
  // A listed MCP server (PostHog): added under its own name, then signed in to, from the one click.
  return openSignIn('connect mcp server', { app: listed.key }, async () => {
    const created = await api('/api/integrations/servers', {
      method: 'POST', body: { name: listed.name, url: listed.url, headers: {} },
    });
    return { url: created.sign_in_url };
  });
}

async function signInsChanged() {
  // What is connected changed, and a waiting chat may carry on.
  if (location.hash === '#/integrations') await loadIntegrations();
  else if (!$('layout').hidden && state.threadId) await loadChat();
}

if ('BroadcastChannel' in window) {
  // A sign-in finished in another window.
  new BroadcastChannel('montybot-integrations').addEventListener('message', () => reportUnlessOffline(signInsChanged()));
}

function askIds(ask) {
  return { ask_id: ask.id, ask_kind: ask.kind, run_id: state.run && state.run.id, thread_id: state.threadId };
}

async function answer(ask, body) {
  const before = page;
  const buttons = [...$('ask').querySelectorAll('button')];
  for (const each of buttons) each.disabled = true;  // Approve and Deny together: one answer only
  const name = ask.kind === 'approval' ? (body.approved ? 'approve' : 'deny')
    : ask.kind === 'connect' ? (body.connected ? 'connected' : 'not now') : 'answer question';
  try {
    await telemetry.span(name, { ...askIds(ask), answer: body.text, reason: body.reason }, () => (
      api(`/api/asks/${ask.id}`, { method: 'POST', body })));
  } catch (error) {
    for (const each of buttons) each.disabled = false;
    if (error.status !== 409) throw error;  // 409: answered already, or the run moved on; the reload shows it
  }
  if (page !== before) return;  // the user went elsewhere meanwhile
  if (state.run && state.run.ask && state.run.ask.id === ask.id) {
    $('ask').hidden = true;
    $('ask').dataset.id = '';
  }
  await loadChat();
}

async function takeOver(ask) {
  // The live view's link names the hand-off: it is never recorded.
  const ids = askIds(ask);
  await telemetry.span('take over the browser', ids, async (note) => {
    const before = page;
    let link;
    try {
      link = await api(`/api/runs/${state.run.id}/live`, { method: 'POST', body: {} });
    } catch (error) {
      if (error.status !== 404) throw error;
      note({ outcome: 'hand-off over' });
      if (page === before) await loadChat();  // the hand-off ended meanwhile: show where the run is now
      return;
    }
    if (page !== before || !state.run || !state.run.ask || state.run.ask.id !== ask.id) {  // left, or it ended
      note({ outcome: 'moved on' });
      return;
    }
    state.takeoverAskId = ask.id;
    $('live').src = link.url;
    // A modal dialog: the page behind cannot be reached. The live view's own "Back to chat" closes it.
    $('takeover').showModal();
    state.takeoverEnd = telemetry.begin('live view open', ids);
    $('live').focus();
  });
}

function closeTakeover(reason) {
  // The hand-off goes on until the user gives the browser back; "Take over" opens it again.
  if (!$('takeover').open) return;
  state.takeoverClosedBy = reason;
  $('takeover').close();
}
window.addEventListener('message', (event) => {
  // The live view's own "Back to chat" button.
  if (event.source !== $('live').contentWindow || event.origin !== location.origin) return;
  if (event.data && event.data.kind === 'close-takeover') closeTakeover('back to chat');
});
$('takeover').addEventListener('close', () => {  // also after Escape
  if (state.takeoverEnd) state.takeoverEnd({ closed_by: state.takeoverClosedBy || 'escape' });
  state.takeoverEnd = null;
  state.takeoverClosedBy = null;
  state.takeoverAskId = null;
  $('live').src = 'about:blank';
  const takeOverAgain = $('ask').hidden ? null : $('ask').querySelector('button');
  (takeOverAgain || $('message')).focus();
});

// --- the bot's browser, while it works ---

function startWatching() {
  // A screenshot a second while the browser panel is open.
  if (watching && watching.runId === state.run.id) return;
  stopWatching();  // another run's screenshots
  watching = new AbortController();
  watching.runId = state.run.id;
  const signal = watching.signal;
  const runId = state.run.id;
  if (desktop.matches && !state.browserClosed) showBrowser();
  (async () => {
    while (!signal.aborted) {
      if (!$('browser').hidden) await showScreenshot(runId, signal).catch(() => {});
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  })();
}

async function showScreenshot(runId, signal) {
  const response = await fetch(`/api/runs/${runId}/screen`, { credentials: 'same-origin', signal });
  if (response.status !== 200) return;  // 204: no picture yet
  const url = URL.createObjectURL(await response.blob());
  if (signal.aborted) { URL.revokeObjectURL(url); return; }
  const old = $('screen').src;
  $('screen').src = url;
  $('screen').hidden = false;  // hidden until the first picture, rather than a broken image
  if (old.startsWith('blob:')) URL.revokeObjectURL(old);
}

function stopWatching() {
  if (watching) watching.abort();
  watching = null;
  $('browser').hidden = true;
  const old = $('screen').src || '';
  $('screen').removeAttribute('src');
  $('screen').hidden = true;
  if (old.startsWith('blob:')) URL.revokeObjectURL(old);
  updateBrowserButton();
}

function showBrowser() {
  $('browser').hidden = false;
  updateBrowserButton();
}

function updateBrowserButton() {
  // "Watch browser" opens the panel while the bot works and the panel is closed.
  $('browser-button').hidden = !(watching && $('browser').hidden && !$('layout').hidden);
  const browserCoversChat = !desktop.matches && !$('browser').hidden && !$('layout').hidden;
  $('chat').inert = browserCoversChat;
  document.querySelector('.bar').inert = browserCoversChat;
  if (browserCoversChat) $('drawer').inert = true; else syncDrawer();
}

$('browser-button').addEventListener('click', () => { showBrowser(); $('close-browser').focus(); });
$('close-browser').addEventListener('click', () => {
  state.browserClosed = true;
  $('browser').hidden = true;
  updateBrowserButton();
  if (!$('browser-button').hidden) $('browser-button').focus(); else $('message').focus();
});

// --- sending ---

$('composer').addEventListener('submit', (event) => {
  event.preventDefault();
  if ($('send').hidden && !$('ask').hidden) {  // Monty waits for an answer: take the user there
    $('ask').querySelector('textarea, button').focus();
    return;
  }
  if ($('send').disabled || $('send').hidden) return;  // Enter obeys the same guard as the button
  const text = $('message').value.trim();
  if (!text) return;
  $('send').disabled = true;  // the chat enables it again once it has loaded the new message's run
  report(send(text).catch((error) => { $('send').disabled = false; throw error; }));
});

function send(text) {
  const threadId = state.threadId;
  return telemetry.span('send message', { thread_id: threadId, new_chat: threadId === null, text }, (note) => (
    sendMessage(text, note)));
}

async function sendMessage(text, note) {
  const before = page;
  const threadId = state.threadId;
  const path = threadId === null ? '/api/threads' : `/api/threads/${threadId}/messages`;
  let created;
  try {
    created = await api(path, { method: 'POST', body: { text, timezone: TIMEZONE } });
  } catch (error) {
    if (page !== before) throw error;
    if (error.status === 409) await loadChat();  // Monty started on this chat elsewhere (a schedule, another tab)
    if (error.status !== 404 || threadId === null) throw error;
    // The chat was deleted with its schedule; the message is still in the box. Show the notice once the new
    // chat has opened, as opening a page clears notices.
    location.hash = '#/new';
    await new Promise((resolve) => window.addEventListener('hashchange', resolve, { once: true }));
    throw new Error('That chat was deleted. Send your message again to start a new chat.');
  }
  note({ thread_id: created.thread_id, run_id: created.run_id });
  if ($('message').value.trim() === text) $('message').value = '';
  if (page !== before) {
    await loadThreads();  // the user went elsewhere: the chat is in the list
  } else if (threadId === null) {
    location.hash = `#/t/${created.thread_id}`;  // opens the new chat
  } else {
    await loadChat();
    await loadThreads();
  }
}

$('message').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && desktop.matches) {  // not mid-word in an IME
    event.preventDefault();
    $('composer').requestSubmit();
  }
});

// --- files ---

async function openFiles() {
  $('file-list').replaceChildren();
  $('files-status').textContent = 'Loading…';
  $('files-title').focus();
  let data;
  try {
    data = await api('/api/files');
  } catch (error) {
    if (error.name !== 'AbortError') $('files-status').textContent = 'Could not load files. Try Refresh.';
    return;
  }
  $('files-status').textContent = data.truncated ? 'Showing a partial list (up to 1,000 entries, 16 folders deep).' :
    (data.files.length ? '' : 'No files yet. Ask Monty to download or make a file.');
  $('file-list').replaceChildren(...data.files.map((file) => {
    const tooLarge = file.size > data.max_download_bytes;
    // Traced by size only: file names stay out of telemetry.
    const download = button('Download', 'secondary', () => (
      telemetry.span('download file', { size: file.size }, (note) => downloadFile(file.path, note))));
    download.disabled = tooLarge;
    if (tooLarge) download.title = 'Over the 20 MB download limit';
    const item = element('li');
    item.append(element('span', `${shownPath(file.path)} (${shownSize(file.size)})`), download);
    return item;
  }));
}

function shownPath(path) {
  return path.replace(/^\/work\//, '');  // where Monty's code sees the user's files; the user just has "their files"
}

function shownSize(bytes) {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

async function downloadFile(path, note = () => {}) {
  let response;
  try {
    response = await fetch('/api/files/download', {
      method: 'POST', credentials: 'same-origin', signal: page.signal,
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }),
    });
  } catch (error) {
    if (error.name !== 'AbortError') $('files-status').textContent = 'Download failed. Try again.';
    return;
  }
  note({ 'http.response.status_code': response.status });
  if (response.status === 401) { signedOut(); return; }
  if (!response.ok) {
    $('files-status').textContent = response.status === 413 ? 'This file is over the 20 MB download limit.' :
      'File unavailable. Refresh and try again.';
    return;
  }
  let blob;
  try {
    blob = await response.blob();
  } catch (error) {
    if (error.name !== 'AbortError') $('files-status').textContent = 'Download failed. Try again.';  // dropped mid-file
    return;
  }
  note({ bytes: blob.size });
  const url = URL.createObjectURL(blob);
  const link = element('a');
  link.href = url;
  const disposition = response.headers.get('Content-Disposition') || '';
  link.download = decodeURIComponent(disposition.split("filename*=UTF-8''")[1] || 'download');
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$('refresh-files').addEventListener('click', () => report(openFiles()));

// --- integrations: apps through Composio, and the user's own MCP servers ---

let listing = null;  // what the page lists (`catalog.entries`), once loaded: it changes rarely
let moreAppsOpen = null;  // whether the user opened or closed More apps; until they do, open if one is connected
let moreConnected = false;
const STATES = { connected: 'Connected', needs_sign_in: 'Needs you to sign in', broken: 'Not working' };
const SIGNING_IN = 'Finish signing in in the window that opened. This page updates when you have.';

async function openIntegrations() {
  $('integrations-title').focus();
  if (state.serverName) {
    $('server-name').value = state.serverName;
    state.serverName = null;
    showServerForm(true);
  }
  await loadIntegrations();
}

async function loadIntegrations() {
  $('integrations-status').textContent = 'Loading…';
  let data;
  try {
    data = await api('/api/integrations');
    if (!listing) listing = await api('/api/integrations/apps');
  } catch (error) {
    if (error.name !== 'AbortError') $('integrations-status').textContent = error.message;
    return;
  }
  $('integrations-status').textContent = '';
  renderIntegrations(listing, data.connections);
}

function renderIntegrations(entries, connections) {
  // Each listed integration is drawn with the user's connection to it: an app's account by its key, a listed MCP
  // server by its host. The user's other servers are under Custom; an account for an app no longer listed stays
  // under More apps, where it can still be disconnected.
  const unmatched = [...connections];
  const take = (matches) => {
    const index = unmatched.findIndex(matches);
    return index < 0 ? null : unmatched.splice(index, 1)[0];
  };
  const rows = entries.map((entry) => ({
    entry,
    connection: take((c) => (entry.provider === 'mcp' ? c.provider === 'mcp' && Boolean(entry.host)
      && c.detail === entry.host : c.provider === 'composio' && c.key === entry.key)),
  }));
  const kinds = new Map();
  for (const row of rows.filter((r) => r.entry.featured)) {
    if (!kinds.has(row.entry.kind)) kinds.set(row.entry.kind, []);
    kinds.get(row.entry.kind).push(row);
  }
  $('integration-groups').replaceChildren(...[...kinds].map(([kind, group]) => {
    const section = element('section', '', 'integration-group');
    const title = element('h3', group[0].entry.kind_label, 'group-title');
    title.id = `integrations-${kind}`;
    title.append(' ', element('span', '', 'group-count'));
    const list = element('ul', '', 'integration-list');
    list.setAttribute('aria-labelledby', title.id);
    list.append(...group.map(integrationRow));
    section.setAttribute('aria-labelledby', title.id);
    section.append(title, list);
    return section;
  }));
  for (const old of $('custom-list').querySelectorAll(':scope > li:not(#add-server-row)')) old.remove();
  $('add-server-row').before(...unmatched.filter((c) => c.provider === 'mcp').map((connection) => (
    integrationRow({ entry: null, connection }))));
  const more = [...rows.filter((r) => !r.entry.featured),
    ...unmatched.filter((c) => c.provider !== 'mcp').map((connection) => ({ entry: null, connection }))];
  $('more-list').replaceChildren(...more.map(integrationRow));
  moreConnected = more.some((r) => r.connection);
  filterIntegrations();
}

function integrationRow({ entry, connection }) {
  // A logo, the name with how its connection is doing, a line about it, and what can be done with it.
  const name = entry ? entry.name : connection.name;
  const detail = (entry && entry.description) || (connection && connection.detail) || '';
  const item = element('li', '', connection ? 'integration-row' : 'integration-row available');
  const heading = element('span', '', 'integration-name');
  heading.append(element('strong', name));
  if (connection) heading.append(element('span', STATES[connection.state] || connection.state, `badge ${connection.state}`));
  const about = element('span', detail, 'integration-detail');
  about.title = detail;  // the whole of it, where the line is cut short
  const words = element('span', '', 'integration-words');
  words.append(heading, about);
  const summary = element('span', '', 'integration-about');
  summary.append(logo((entry && entry.logo) || (connection && connection.logo), name), words);
  item.append(summary, integrationActions(entry, connection, name));
  item.dataset.search = [name, entry ? entry.key : connection.key, detail, (entry && entry.kind_label) || '']
    .join(' ').toLowerCase();
  return item;
}

function integrationActions(entry, connection, name) {
  const actions = element('span', '', 'list-actions');
  const add = (text, className, onClick) => {
    const made = button(text, className, onClick);
    made.setAttribute('aria-label', `${text} ${name}`);  // each row has one: say which
    actions.append(made);
  };
  if (!connection) add('Connect', 'secondary', () => connectEntry(entry));
  else if (connection.state === 'needs_sign_in') add('Sign in', 'good', () => reconnect(connection));
  else if (connection.state !== 'connected') add('Reconnect', 'secondary', () => reconnect(connection));
  if (connection) {
    const verb = connection.state === 'connected' ? 'Disconnect' : 'Remove';
    add(verb, 'secondary', () => removeConnection(connection, `${verb} ${name}?`));
  }
  return actions;
}

function filterIntegrations() {
  // Search narrows every group, and a group with nothing left is hidden. More apps is open while searching.
  const typed = $('integration-search').value.trim();
  const query = typed.toLowerCase();
  let matches = 0;
  const filter = (list) => {
    let shown = 0;
    for (const row of list.querySelectorAll(':scope > li[data-search]')) {
      row.hidden = Boolean(query) && !row.dataset.search.includes(query);
      if (!row.hidden) shown += 1;
    }
    matches += shown;
    return shown;
  };
  for (const section of $('integration-groups').children) {
    const shown = filter(section.querySelector('ul'));
    section.querySelector('.group-count').textContent = String(shown);
    section.hidden = !shown;
  }
  const custom = filter($('custom-list'));
  $('custom-count').textContent = custom ? String(custom) : '';
  const more = filter($('more-list'));
  $('more-count').textContent = String(more);
  $('more-apps').hidden = !more;
  $('more-apps').open = Boolean(query) || (moreAppsOpen === null ? moreConnected : moreAppsOpen);
  $('integration-empty').hidden = !query || matches > 0;
  $('integration-empty').textContent = `No integration matches “${typed}”. If it has an MCP server, add it under Custom.`;
}

$('integration-search').addEventListener('input', filterIntegrations);
$('more-apps').addEventListener('toggle', () => {
  if (!$('integration-search').value.trim()) moreAppsOpen = $('more-apps').open;  // not the search's opening it
});

async function connectEntry(entry) {
  if (entry.provider !== 'mcp') {
    await connectApp(entry.key);
    return;
  }
  const signingIn = await connectPreset(entry);
  if (signingIn) await loadIntegrations();  // listed as needing a sign-in until the window says it is done
  $('integrations-status').textContent = signingIn ? SIGNING_IN : `${entry.name} is connected.`;
}

async function connectApp(slug) {
  await openSignIn('connect app', { app: slug }, () => (
    api(`/api/integrations/apps/${encodeURIComponent(slug)}/connect`, { method: 'POST', body: {} })));
  $('integrations-status').textContent = SIGNING_IN;
}

async function reconnect(connection) {
  if (connection.provider === 'composio') {
    await connectApp(connection.key);
    return;
  }
  await openSignIn('sign in to mcp server', {}, () => (
    api(`/api/integrations/servers/${connection.id}/sign-in`, { method: 'POST', body: {} })));
  $('integrations-status').textContent = SIGNING_IN;
}

async function removeConnection(connection, question) {
  if (!confirm(`${question} Monty will no longer be able to use it.`)) return;
  const path = connection.provider === 'composio' ? `/api/integrations/apps/accounts/${encodeURIComponent(connection.id)}`
    : `/api/integrations/servers/${connection.id}`;
  await telemetry.span('remove integration', { provider: connection.provider }, () => api(path, { method: 'DELETE' }));
  await loadIntegrations();
}

function showServerForm(open) {
  $('server-panel').hidden = !open;
  $('show-server-form').setAttribute('aria-expanded', String(open));
}

$('show-server-form').addEventListener('click', () => {
  const open = $('server-panel').hidden;
  showServerForm(open);
  if (open) $('server-name').focus();
});

$('server-form').addEventListener('submit', (event) => {
  event.preventDefault();
  report(addServer());
});

async function addServer() {
  // Traced without the address or the header: either can hold a key.
  $('server-error').textContent = '';
  const headerName = $('server-header-name').value.trim();
  const headers = headerName ? { [headerName]: $('server-header-value').value } : {};
  $('add-server').disabled = true;
  let created;
  try {
    created = await telemetry.span('add mcp server', { with_header: Boolean(headerName) }, () => (
      api('/api/integrations/servers', {
        method: 'POST', body: { name: $('server-name').value, url: $('server-url').value, headers },
      })));
  } catch (error) {
    $('server-error').textContent = error.message;
    return;
  } finally {
    $('add-server').disabled = false;
  }
  $('server-form').reset();
  showServerForm(false);
  await loadIntegrations();
  // An OAuth server is listed as needing a sign-in: its Sign in button opens it, from the user's own click.
  $('integrations-status').textContent = created.sign_in_url ? `${created.connection.name} needs you to sign in: use its Sign in button.`
    : `${created.connection.name} is connected.`;
}

// --- saved sign-ins and schedules ---

async function openSignins() {
  $('signins-title').focus();
  const sites = await api('/api/sign-ins');
  $('signin-list').replaceChildren(...(sites.length ? sites.map((s) => {
    const item = element('li');
    item.append(element('span', s.site), button('Forget', 'secondary', async () => {
      await telemetry.span('forget saved browser data', { site: s.site }, () => (
        api(`/api/sign-ins/${encodeURIComponent(s.site)}`, { method: 'DELETE' })));
      await openSignins();
    }));
    return item;
  }) : [element('li', 'No saved browser data yet. Sign in through browser takeover when Monty asks.')]));
}

async function openSchedules() {
  $('schedules-title').focus();
  const schedules = await api('/api/schedules');
  $('schedule-list').replaceChildren(...(schedules.length ? schedules.map((s) => {
    const name = element('span');
    name.append(element('strong', s.name), element('span', `${s.when}${s.paused ? ' (paused)' : ''}`, 'list-detail'));
    const actions = element('span', '', 'list-actions');
    actions.append(
      button('Open chat', 'secondary', () => { location.hash = `#/t/${s.thread_id}`; }),
      button(s.paused ? 'Resume' : 'Pause', 'secondary', async () => {
        const action = s.paused ? 'resume' : 'pause';
        await telemetry.span(`${action} schedule`, { schedule_id: s.id, thread_id: s.thread_id }, () => (
          api(`/api/schedules/${s.id}/${action}`, { method: 'POST', body: {} })));
        await openSchedules();
      }),
      button('Delete', 'bad', async () => {
        if (!confirm(`Delete "${s.name}"? Monty will stop running it.`)) return;
        await telemetry.span('delete schedule', { schedule_id: s.id, thread_id: s.thread_id }, () => (
          api(`/api/schedules/${s.id}`, { method: 'DELETE' })));
        await Promise.all([openSchedules(), loadThreads()]);  // its chat goes too if it never ran
      }),
    );
    const item = element('li');
    item.append(name, actions);
    return item;
  }) : [element('li', 'No scheduled tasks yet. Tell Monty what to do and when in a chat.')]));
}

$('open-files').addEventListener('click', () => { location.hash = '#/files'; closeDrawer(); });
$('open-signins').addEventListener('click', () => { location.hash = '#/sign-ins'; closeDrawer(); });
$('open-schedules').addEventListener('click', () => { location.hash = '#/schedules'; closeDrawer(); });
$('open-integrations').addEventListener('click', () => { location.hash = '#/integrations'; closeDrawer(); });
for (const back of document.querySelectorAll('.page .back')) {
  back.addEventListener('click', () => { location.hash = state.threadId ? `#/t/${state.threadId}` : '#/new'; });
}

// --- notifications: a push to the phone when the bot needs the user ---

function base64urlBytes(text) {
  const base64 = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4);
  return Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
}

async function showNotificationButton() {
  // Only where notifications can work: this browser can show them and this server can send them.
  if (!('Notification' in window) || !('serviceWorker' in navigator) || !('PushManager' in window)) return;
  const response = await fetch('/api/push/key', { credentials: 'same-origin' });  // not tied to the page on screen
  state.pushKey = response.ok ? (await response.json()).public_key : null;
  $('enable-notifications').hidden = !state.pushKey;
  $('notifications-label').textContent = 'Notify me when Monty needs me';
  const registration = state.pushKey && await navigator.serviceWorker.getRegistration('/sw.js');
  const subscription = registration && await registration.pushManager.getSubscription();
  if (!subscription || Notification.permission !== 'granted') return;
  // Say "on" only once the server has it for whoever is signed in now. A subscription another account made on
  // this device would send that account's pushes here: drop it.
  try {
    await api('/api/push/subscriptions', { method: 'POST', body: subscription.toJSON() });
    $('notifications-label').textContent = 'Notifications are on';
  } catch (error) {
    if (error.status !== 409) throw error;
    await subscription.unsubscribe();
  }
}

async function enableNotifications(note) {
  // Traced by the browser's answer only: the subscription's address and keys stay out of telemetry.
  const permission = await Notification.requestPermission();
  note({ permission });
  if (permission !== 'granted') {
    throw new Error('Notifications are blocked for this site. Allow them in your browser settings, then try again.');
  }
  const registration = await navigator.serviceWorker.register('/sw.js');
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true, applicationServerKey: base64urlBytes(state.pushKey),
  });
  await api('/api/push/subscriptions', { method: 'POST', body: subscription.toJSON() });
  $('notifications-label').textContent = 'Notifications are on';
}

async function stopNotifications() {
  // On sign-out, so this browser no longer gets this account's pushes.
  if (!('serviceWorker' in navigator)) return;
  const registration = await navigator.serviceWorker.getRegistration('/sw.js');
  const subscription = registration && await registration.pushManager.getSubscription();
  if (!subscription) return;
  try {
    await api('/api/push/subscriptions', { method: 'DELETE', body: { endpoint: subscription.endpoint } });
  } finally {
    await subscription.unsubscribe();
  }
}

$('enable-notifications').addEventListener('click', () => (
  report(telemetry.span('enable notifications', {}, enableNotifications))));

// --- routing ---

const PAGES = { '#/files': ['files', 'Files', openFiles], '#/sign-ins': ['signins', 'Saved browser data', openSignins],
  '#/integrations': ['integrations', 'Integrations', openIntegrations], '#/schedules': ['schedules', 'Schedules', openSchedules] };
const PAGE_BUTTONS = { '#/files': 'open-files', '#/sign-ins': 'open-signins', '#/integrations': 'open-integrations',
  '#/schedules': 'open-schedules' };

async function route() {
  const hash = location.hash;
  const found = PAGES[hash];
  for (const [path, [id]] of Object.entries(PAGES)) $(id).hidden = path !== hash;
  for (const [path, id] of Object.entries(PAGE_BUTTONS)) {
    if (path === hash) $(id).setAttribute('aria-current', 'page'); else $(id).removeAttribute('aria-current');
  }
  $('layout').hidden = Boolean(found);
  if (found) {
    newPage();
    $('title').textContent = found[1];
    await Promise.all([found[2](), loadThreads()]);
    return;
  }
  const match = hash.match(/^#\/t\/([0-9a-f-]{36})$/);
  try {
    await openChat(match ? match[1] : null);
  } finally {
    await loadThreads();  // also when the chat failed to load: the list is where the user tries again
  }
}

async function start() {
  let me;
  try {
    me = await api('/api/me');
  } catch (error) {
    if (error.status !== 401) $('signin-error').textContent = error.message;  // offline, not signed out
    show('signin');
    return;
  }
  startTelemetry(me && me.id);  // in the background: nothing waits for it
  show('main');
  showNotificationButton().catch(console.error);  // optional: the button simply stays hidden
  await route();
}

window.addEventListener('hashchange', () => { if (!$('main').hidden) report(route()); });
report(start());
