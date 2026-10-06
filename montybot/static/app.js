// monty-bot web app: chat with the bot, watch its browser, take over when it asks. No build step, no framework.
'use strict';

const $ = (id) => document.getElementById(id);
const state = { thread: null, run: null, poll: null, screenTimer: null, liveAsk: null, signingUp: false, routing: false,
  events: null, streamRun: null, preview: null, previewBubble: null, streamError: false, view: 0, refresh: 0 };

async function api(path, options = {}, current = () => true) {
  const init = { credentials: 'same-origin', ...options, headers: { ...(options.headers || {}) } };
  if (init.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  const type = response.headers.get('Content-Type') || '';
  const data = type.includes('application/json') ? await response.json() : null;
  if (response.status === 401 && path !== '/api/signin' && current()) signedOut();
  if (!response.ok) {
    const detail = data && typeof data.detail === 'string' ? data.detail : `Request failed (${response.status})`;
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return data;
}

function show(id) {
  for (const screen of ['signin', 'main']) $(screen).hidden = screen !== id;
}

function signedOut() {
  // The session ended (signed out elsewhere, or expired): stop asking the server and offer to sign in again.
  stopPolling();
  stopStream();
  state.view++;
  state.thread = null;
  state.run = null;
  state.liveAsk = null;
  stopWatching();
  show('signin');
}

// --- signing in ---

$('signup-button').addEventListener('click', () => {
  state.signingUp = !state.signingUp;
  $('signin-button').textContent = state.signingUp ? 'Create account' : 'Sign in';
  $('signup-button').textContent = state.signingUp ? 'I have an account' : 'Create an account';
  $('password').autocomplete = state.signingUp ? 'new-password' : 'current-password';
});

$('signin-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('signin-error').textContent = '';
  try {
    await api(state.signingUp ? '/api/signup' : '/api/signin', {
      method: 'POST', body: { email: $('email').value, password: $('password').value },
    });
    await start();
  } catch (error) {
    $('signin-error').textContent = error.message;
  }
});

$('signout').addEventListener('click', async () => {
  signedOut();
  try {
    await stopNotifications();
  } catch (error) {
    console.error(error);
  }
  await api('/api/signout', { method: 'POST', body: {} });
  location.hash = '';
  location.reload();
});

// --- chats ---

async function loadThreads() {
  const threads = await api('/api/threads');
  const list = $('threads');
  list.replaceChildren(...threads.map((thread) => {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.textContent = thread.title || 'Untitled';
    button.className = thread.id === state.thread ? 'current' : '';
    button.addEventListener('click', () => { location.hash = `#/t/${thread.id}`; closeDrawer(); });
    item.append(button);
    return item;
  }));
}

function closeDrawer() { $('drawer').classList.remove('open'); }
$('menu-button').addEventListener('click', () => $('drawer').classList.toggle('open'));
$('new-chat').addEventListener('click', () => { location.hash = '#/new'; closeDrawer(); });

function message(role, text) {
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  div.textContent = text;
  return div;
}

function emptyChat() {
  const div = document.createElement('div');
  div.className = 'empty';
  div.innerHTML = '<h2>What should I do?</h2><p>For example: “Find the three cheapest flights to Lisbon next Friday”, ' +
    '“Check my last order on the shop”, or “Every Tuesday, put my shopping list in my cart”.</p>';
  return div;
}

async function openThread(id) {
  stopPolling();
  stopStream();
  state.view++;
  state.thread = id;
  state.run = null;
  state.liveAsk = null;
  stopWatching();
  hideBrowser();
  $('ask').hidden = true;
  $('ask').dataset.id = '';
  $('status').hidden = true;
  if (id === null) {
    $('title').textContent = 'New chat';
    $('messages').replaceChildren(emptyChat());
    return;
  }
  await refresh();
}

async function refresh() {
  const id = state.thread;
  const view = state.view;
  const refreshId = ++state.refresh;
  const current = () => state.thread === id && state.view === view && state.refresh === refreshId;
  if (id === null) return;
  let thread;
  try {
    thread = await api(`/api/threads/${id}`, {}, current);
  } catch (error) {
    if (!current()) return;
    if (error.status === 404) { location.hash = '#/new'; return; }
    throw error;
  }
  if (!current()) return;  // another chat, sign-out, or a newer refresh meanwhile
  $('title').textContent = thread.title || 'monty-bot';
  const box = $('messages');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.replaceChildren(...thread.messages.map((m) => message(m.role, m.text)));
  renderRun(thread.run);
  renderPreview();
  if (atBottom) box.scrollTop = box.scrollHeight;
  const active = thread.run && ['queued', 'running', 'waiting'].includes(thread.run.status);
  if (active) schedulePoll(); else stopPolling();
}

function schedulePoll() {
  // One refresh at a time: the next is scheduled when this one has finished.
  if (state.poll !== null || state.thread === null) return;
  state.poll = setTimeout(() => {
    state.poll = null;
    refresh().catch((error) => {
      console.error(error);
      if (error.status !== 401) schedulePoll();  // try again, unless signed out
    });
  }, 1500);
}

function stopPolling() {
  if (state.poll !== null) clearTimeout(state.poll);
  state.poll = null;
}

function renderRun(run) {
  state.run = run;
  const working = run && (run.status === 'queued' || run.status === 'running');
  $('status').hidden = !working;
  if (working) renderActivity();
  $('send').disabled = Boolean(run && ['queued', 'running', 'waiting'].includes(run.status));
  renderAsk(run && run.status === 'waiting' ? run.ask : null);
  if (working && run.activity.length) watchBrowser(); else stopWatching();
  updateBrowserButton();
  followRun(run);
}

function updateBrowserButton() {
  // Opens the bot's browser while it works and the panel is closed.
  const run = state.run;
  const working = run && (run.status === 'queued' || run.status === 'running') && run.activity.length;
  $('browser-button').hidden = !(working && $('browser').hidden);
}

// --- provisional assistant text: full replacement snapshots, never durable history ---

function stopStream() {
  if (state.events !== null) state.events.close();
  state.events = null;
  state.streamRun = null;
  state.streamError = false;
  state.preview = null;
  if (state.previewBubble !== null) state.previewBubble.remove();
  state.previewBubble = null;
}

function renderActivity() {
  const run = state.run;
  if (!run || !['queued', 'running'].includes(run.status)) return;
  const activity = state.preview && state.preview.activity;
  const saved = run.activity.length ? run.activity[run.activity.length - 1] : 'Working…';
  $('status').textContent = state.streamError ? `${saved} · Live preview unavailable; checking for updates…` : activity || saved;
}

function renderPreview() {
  if (state.previewBubble !== null) state.previewBubble.remove();
  state.previewBubble = null;
  if (!state.preview || !state.preview.text) return;
  const box = $('messages');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const bubble = document.createElement('div');
  bubble.className = 'msg assistant';
  const label = document.createElement('small');
  label.textContent = state.streamError ? 'Live draft · connection lost; may be incomplete' : 'Live draft · not saved yet';
  const text = document.createElement('div');
  text.textContent = state.preview.text;  // assistant text only, never HTML or tool payloads
  bubble.append(label, text);
  box.append(bubble);
  state.previewBubble = bubble;
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function followRun(run) {
  if (!run || !['queued', 'running', 'waiting'].includes(run.status)) { stopStream(); return; }
  if (state.streamRun === run.id) return;
  stopStream();
  if (typeof EventSource === 'undefined') return;  // polling still follows asks and completion
  const view = state.view;
  const thread = state.thread;
  const source = new EventSource(`/api/runs/${run.id}/events`);
  state.events = source;
  state.streamRun = run.id;
  const current = () => state.events === source && state.view === view && state.thread === thread;
  source.onopen = () => {
    if (!current()) return;
    state.streamError = false;
    renderActivity();
    renderPreview();
  };
  source.addEventListener('preview', (event) => {
    if (!current()) return;
    let preview;
    try { preview = JSON.parse(event.data); } catch { return; }
    if (!Number.isInteger(preview.revision) || typeof preview.text !== 'string' || typeof preview.activity !== 'string') return;
    state.preview = { revision: preview.revision, text: preview.text, activity: preview.activity };
    renderPreview();
    renderActivity();
  });
  source.addEventListener('status', (event) => {
    if (!current()) return;
    let status;
    try { status = JSON.parse(event.data); } catch { return; }
    if (status.id !== run.id || status.thread_id !== thread) return;
    state.refresh++;  // discard any poll response captured before this authoritative SSE update
    renderRun(status);
    if (!['queued', 'running', 'waiting'].includes(status.status)) {
      // renderRun closes the source and removes the draft BEFORE loading committed history.
      refresh().catch(() => { if (state.view === view && state.thread === thread) schedulePoll(); });
    }
  });
  source.onerror = () => {
    if (!current()) return;
    // EventSource retries automatically. Do not present the draft as complete or log event data.
    state.streamError = true;
    renderActivity();
    renderPreview();
    schedulePoll();  // also detects expired auth/ownership, which EventSource cannot expose
  };
}

// --- what the bot asks ---

function renderAsk(ask) {
  const box = $('ask');
  if (ask === null) {
    box.hidden = true;
    box.dataset.id = '';
    if (state.liveAsk !== null) { state.liveAsk = null; hideBrowser(); }
    return;
  }
  if (box.dataset.id === ask.id) return;  // already shown; keep what the user is typing
  box.dataset.id = ask.id;
  box.hidden = false;
  const prompt = document.createElement('p');
  prompt.textContent = ask.prompt;
  const row = document.createElement('div');
  row.className = 'row';
  if (ask.kind === 'question') {
    const input = document.createElement('textarea');
    input.rows = 2;
    input.placeholder = 'Your answer';
    const send = button('Answer', '', async () => {
      if (!input.value.trim()) { input.focus(); return; }
      await answer(ask, { text: input.value });
    });
    row.append(input, send);
  } else if (ask.kind === 'approval') {
    row.append(
      button('Approve', 'good', () => answer(ask, { approved: true })),
      button('Deny', 'bad', () => answer(ask, { approved: false, reason: 'the user said no' })),
    );
  } else {
    row.append(button('Take over the browser', '', () => takeOver(ask)));
  }
  box.replaceChildren(prompt, row);
  notify(ask);
}

function button(text, kind, onClick) {
  // Disabled while its action is pending; a failure is shown to the user.
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = text;
  if (kind) b.className = kind;
  b.addEventListener('click', async () => {
    b.disabled = true;
    try {
      await onClick();
    } catch (error) {
      alert(error.message);
    } finally {
      b.disabled = false;
    }
  });
  return b;
}

async function answer(ask, body) {
  const view = state.view;
  const thread = state.thread;
  const runId = state.run && state.run.id;
  const current = () => state.view === view && state.thread === thread && state.run &&
    state.run.id === runId && state.run.ask && state.run.ask.id === ask.id;
  try {
    await api(`/api/asks/${ask.id}`, { method: 'POST', body }, current);
  } catch (error) {
    if (!current()) return;
    throw error;
  }
  if (!current()) return;
  $('ask').hidden = true;
  $('ask').dataset.id = '';
  schedulePoll();
  await refresh();
}

async function takeOver(ask) {
  const view = state.view;
  const thread = state.thread;
  const runId = state.run && state.run.id;
  const current = () => state.view === view && state.thread === thread && state.run &&
    state.run.id === runId && state.run.status === 'waiting' && state.run.ask && state.run.ask.id === ask.id;
  if (!current()) return;
  let link;
  try {
    link = await api(`/api/runs/${runId}/live`, { method: 'POST', body: {} }, current);
  } catch (error) {
    if (!current()) return;  // an old request must not alert or sign out a newer view
    throw error;
  }
  if (!current()) return;
  state.liveAsk = ask.id;
  stopWatching();
  $('browser-label').textContent = 'You have the browser. Give it back when you are done.';
  $('screen').hidden = true;
  $('live').hidden = false;
  $('live').src = link.url;
  $('browser').hidden = false;
  updateBrowserButton();
}

// --- the bot's browser, while it works ---

function watchBrowser() {
  if (state.screenTimer !== null || state.liveAsk !== null) return;
  const view = state.view;
  const thread = state.thread;
  const runId = state.run && state.run.id;
  const askId = state.run && state.run.ask ? state.run.ask.id : null;
  const tick = async () => {
    const timer = state.screenTimer;
    const current = () => state.view === view && state.thread === thread && state.run &&
      state.run.id === runId && ['queued', 'running'].includes(state.run.status) &&
      (state.run.ask ? state.run.ask.id : null) === askId && state.liveAsk === null &&
      state.screenTimer === timer && !$('browser').hidden;
    if (!current()) return;
    const response = await fetch(`/api/runs/${runId}/screen`, { credentials: 'same-origin' });
    if (!current() || !response.ok) return;
    const blob = await response.blob();
    if (!current()) return;
    const url = URL.createObjectURL(blob);
    if (!current()) { URL.revokeObjectURL(url); return; }
    const old = $('screen').src;
    $('screen').src = url;
    if (old.startsWith('blob:')) URL.revokeObjectURL(old);
  };
  const next = () => {
    // One screenshot at a time: the next is asked for a second after this one arrived.
    const timer = setTimeout(() => {
      tick().catch(() => {}).finally(() => { if (state.screenTimer === timer) next(); });
    }, 1000);
    state.screenTimer = timer;
  };
  if (window.matchMedia('(min-width: 900px)').matches) showScreen();
  next();
  tick().catch(() => {});
}

function showScreen() {
  $('browser-label').textContent = "The bot's browser";
  $('live').hidden = true;
  $('screen').hidden = false;
  $('browser').hidden = false;
  updateBrowserButton();
}

function stopWatching() {
  if (state.screenTimer !== null) clearTimeout(state.screenTimer);
  state.screenTimer = null;
  if (state.liveAsk === null) hideBrowser();
}

function hideBrowser() {
  $('browser').hidden = true;
  $('live').src = 'about:blank';
  const old = $('screen').src || '';
  $('screen').removeAttribute('src');
  $('screen').hidden = true;
  if (old.startsWith('blob:')) URL.revokeObjectURL(old);
  updateBrowserButton();
}

$('browser-button').addEventListener('click', showScreen);
$('close-browser').addEventListener('click', () => { $('browser').hidden = true; updateBrowserButton(); });

// --- sending ---

$('composer').addEventListener('submit', async (event) => {
  event.preventDefault();
  const text = $('message').value.trim();
  if (!text) return;
  $('send').disabled = true;
  try {
    const path = state.thread === null ? '/api/threads' : `/api/threads/${state.thread}/messages`;
    const created = await api(path, { method: 'POST', body: { text } });
    $('message').value = '';
    if (state.thread === null) {
      location.hash = `#/t/${created.thread_id}`;
      await loadThreads();
    } else {
      schedulePoll();  // the new run is followed even if this refresh fails
      await refresh();
    }
  } catch (error) {
    alert(error.message);
    $('send').disabled = false;
  }
});

$('message').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && window.matchMedia('(min-width: 900px)').matches) {
    event.preventDefault();
    $('composer').requestSubmit();
  }
});

// --- saved sign-ins and schedules ---

async function openSignins() {
  const list = $('signin-list');
  const sites = await api('/api/sign-ins');
  list.replaceChildren(...(sites.length ? sites.map((s) => {
    const item = document.createElement('li');
    const name = document.createElement('span');
    name.textContent = s.site;
    item.append(name, button('Forget', 'secondary', async () => {
      await api(`/api/sign-ins/${encodeURIComponent(s.site)}`, { method: 'DELETE' });
      await openSignins();
    }));
    return item;
  }) : [Object.assign(document.createElement('li'), { textContent: 'None yet.' })]));
  $('signins').hidden = false;
}

async function openSchedules() {
  const list = $('schedule-list');
  let schedules = [];
  try { schedules = await api('/api/schedules'); } catch (error) { if (error.status !== 404) throw error; }
  list.replaceChildren(...(schedules.length ? schedules.map((s) => {
    const item = document.createElement('li');
    const name = document.createElement('span');
    name.textContent = `${s.name}: ${s.when}${s.paused ? ' (paused)' : ''}`;
    const actions = document.createElement('span');
    actions.append(
      button(s.paused ? 'Resume' : 'Pause', 'secondary', async () => {
        await api(`/api/schedules/${s.id}/${s.paused ? 'resume' : 'pause'}`, { method: 'POST', body: {} });
        await openSchedules();
      }),
      button('Delete', 'bad', async () => {
        await api(`/api/schedules/${s.id}`, { method: 'DELETE' });
        await openSchedules();
      }),
    );
    item.append(name, actions);
    return item;
  }) : [Object.assign(document.createElement('li'), { textContent: 'None yet.' })]));
  $('schedules').hidden = false;
}

$('open-signins').addEventListener('click', () => { location.hash = '#/sign-ins'; closeDrawer(); });
$('open-schedules').addEventListener('click', () => { location.hash = '#/schedules'; closeDrawer(); });
for (const back of document.querySelectorAll('.page .back')) {
  back.addEventListener('click', () => history.back());
}

// --- notifications: a push to the phone when the bot needs the user ---

function base64urlBytes(text) {
  const base64 = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4);
  return Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
}

async function enableNotifications() {
  if (!('Notification' in window) || !('serviceWorker' in navigator) || !('PushManager' in window)) {
    alert('This browser cannot show notifications.');
    return;
  }
  const key = await api('/api/push/key');
  if (!key.public_key) { alert('Notifications are not set up on this server.'); return; }
  if (await Notification.requestPermission() !== 'granted') return;
  const registration = await navigator.serviceWorker.register('/sw.js');
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true, applicationServerKey: base64urlBytes(key.public_key),
  });
  await api('/api/push/subscriptions', { method: 'POST', body: subscription.toJSON() });
  $('enable-notifications').textContent = 'Notifications are on';
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

$('enable-notifications').addEventListener('click', () => enableNotifications().catch((e) => alert(e.message)));

function notify(ask) {
  // While the page is open but hidden; a closed page gets the push from the server instead.
  if (document.visibilityState === 'visible' || !('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  const what = { question: 'has a question', approval: 'needs your approval', handoff: 'needs you in its browser' };
  new Notification('monty-bot', { body: `monty-bot ${what[ask.kind]}.`, tag: ask.id });
}

// --- routing ---

async function route() {
  $('signins').hidden = true;
  $('schedules').hidden = true;
  const hash = location.hash;
  if (hash === '#/sign-ins') return openSignins();
  if (hash === '#/schedules') return openSchedules();
  const match = hash.match(/^#\/t\/([0-9a-f-]{36})$/);
  await openThread(match ? match[1] : null);
  await loadThreads();
}

async function start() {
  try {
    await api('/api/me');
  } catch (error) {
    show('signin');
    return;
  }
  show('main');
  if (!state.routing) {  // once, though signing in again after a 401 calls start() again
    state.routing = true;
    window.addEventListener('hashchange', () => route().catch(console.error));
  }
  await route();
}

start().catch(console.error);
