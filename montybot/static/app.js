// monty-bot web app: chat with the bot, watch its browser, take over when it asks. No build step, no framework.
'use strict';

const $ = (id) => document.getElementById(id);
const state = { thread: null, run: null, poll: null, screenTimer: null, liveAsk: null, signingUp: false };

async function api(path, options = {}) {
  const init = { credentials: 'same-origin', ...options, headers: { ...(options.headers || {}) } };
  if (init.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  const type = response.headers.get('Content-Type') || '';
  const data = type.includes('application/json') ? await response.json() : null;
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
  state.thread = id;
  state.run = null;
  hideBrowser();
  $('ask').hidden = true;
  $('status').hidden = true;
  if (id === null) {
    $('title').textContent = 'New chat';
    $('messages').replaceChildren(emptyChat());
    return;
  }
  await refresh();
}

async function refresh() {
  if (state.thread === null) return;
  let thread;
  try {
    thread = await api(`/api/threads/${state.thread}`);
  } catch (error) {
    if (error.status === 404) { location.hash = '#/new'; return; }
    throw error;
  }
  $('title').textContent = thread.title || 'monty-bot';
  const box = $('messages');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.replaceChildren(...thread.messages.map((m) => message(m.role, m.text)));
  if (atBottom) box.scrollTop = box.scrollHeight;
  renderRun(thread.run);
  const active = thread.run && ['queued', 'running', 'waiting'].includes(thread.run.status);
  if (active && state.poll === null) state.poll = setInterval(() => refresh().catch(console.error), 1500);
  if (!active) stopPolling();
}

function stopPolling() {
  if (state.poll !== null) clearInterval(state.poll);
  state.poll = null;
}

function renderRun(run) {
  state.run = run;
  const working = run && (run.status === 'queued' || run.status === 'running');
  $('status').hidden = !working;
  if (working) $('status').textContent = run.activity.length ? run.activity[run.activity.length - 1] : 'Working…';
  $('send').disabled = Boolean(run && ['queued', 'running', 'waiting'].includes(run.status));
  $('browser-button').hidden = !(run && run.activity.length && working);
  renderAsk(run && run.status === 'waiting' ? run.ask : null);
  if (working && run.activity.length) watchBrowser(); else stopWatching();
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
    const send = button('Answer', '', () => answer(ask, { text: input.value }));
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
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = text;
  if (kind) b.className = kind;
  b.addEventListener('click', onClick);
  return b;
}

async function answer(ask, body) {
  await api(`/api/asks/${ask.id}`, { method: 'POST', body });
  $('ask').hidden = true;
  await refresh();
}

async function takeOver(ask) {
  const link = await api(`/api/runs/${state.run.id}/live`);
  state.liveAsk = ask.id;
  stopWatching();
  $('browser-label').textContent = 'You have the browser. Give it back when you are done.';
  $('screen').hidden = true;
  $('live').hidden = false;
  $('live').src = link.url;
  $('browser').hidden = false;
}

// --- the bot's browser, while it works ---

function watchBrowser() {
  if (state.screenTimer !== null || state.liveAsk !== null) return;
  const tick = async () => {
    if (state.run === null || $('browser').hidden) return;
    const response = await fetch(`/api/runs/${state.run.id}/screen`, { credentials: 'same-origin' });
    if (!response.ok) return;
    const url = URL.createObjectURL(await response.blob());
    const old = $('screen').src;
    $('screen').src = url;
    if (old.startsWith('blob:')) URL.revokeObjectURL(old);
  };
  if (window.matchMedia('(min-width: 900px)').matches) showScreen();
  state.screenTimer = setInterval(() => tick().catch(() => {}), 1000);
  tick().catch(() => {});
}

function showScreen() {
  $('browser-label').textContent = "The bot's browser";
  $('live').hidden = true;
  $('screen').hidden = false;
  $('browser').hidden = false;
}

function stopWatching() {
  if (state.screenTimer !== null) clearInterval(state.screenTimer);
  state.screenTimer = null;
  if (state.liveAsk === null) hideBrowser();
}

function hideBrowser() {
  $('browser').hidden = true;
  $('live').src = 'about:blank';
}

$('browser-button').addEventListener('click', showScreen);
$('close-browser').addEventListener('click', () => { $('browser').hidden = true; });

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
      try {
        await api(`/api/sign-ins/${encodeURIComponent(s.site)}`, { method: 'DELETE' });
        await openSignins();
      } catch (error) { alert(error.message); }
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

async function enableNotifications() {
  if (!('Notification' in window)) { alert('This browser cannot show notifications.'); return; }
  if (await Notification.requestPermission() !== 'granted') return;
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
  const key = await api('/api/push/key');
  if (!key.public_key) return;
  const registration = await navigator.serviceWorker.register('/sw.js');
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true, applicationServerKey: key.public_key,
  });
  await api('/api/push/subscriptions', { method: 'POST', body: subscription.toJSON() });
  $('enable-notifications').textContent = 'Notifications are on';
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
  window.addEventListener('hashchange', () => route().catch(console.error));
  await route();
}

start().catch(console.error);
