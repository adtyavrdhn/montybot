// monty-bot web app: chat with the bot, watch its browser, take over when it asks. No build step, no framework.
'use strict';

const $ = (id) => document.getElementById(id);
const state = { thread: null, run: null, poll: null, screenTimer: null, liveAsk: null, signingUp: false, routing: false };

async function api(path, options = {}) {
  const init = { credentials: 'same-origin', ...options, headers: { ...(options.headers || {}) } };
  if (init.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  const type = response.headers.get('Content-Type') || '';
  const data = type.includes('application/json') ? await response.json() : null;
  if (response.status === 401 && path !== '/api/signin') signedOut();
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
  try {
    await api(state.signingUp ? '/api/signup' : '/api/signin', {
      method: 'POST', body: { email: $('email').value, password: $('password').value },
    });
    await start();
  } catch (error) {
    $('signin-error').textContent = error.message;
  } finally {
    $('signin-button').disabled = false;
    $('signup-button').disabled = false;
  }
});

$('signout').addEventListener('click', async () => {
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
    if (thread.id === state.thread) button.setAttribute('aria-current', 'page');
    button.title = thread.title || 'Untitled';
    button.addEventListener('click', () => { location.hash = `#/t/${thread.id}`; closeDrawer(); });
    item.append(button);
    return item;
  }));
  if (!threads.length) {
    const empty = document.createElement('li');
    empty.className = 'thread-empty';
    empty.textContent = 'Your next task starts with a new chat.';
    list.append(empty);
  }
}

const desktop = window.matchMedia('(min-width: 900px)');
function syncDrawer() {
  const open = !desktop.matches && $('drawer').classList.contains('open');
  $('drawer').inert = !desktop.matches && !open;
  $('drawer-backdrop').hidden = !open;
  $('menu-button').setAttribute('aria-expanded', String(open));
  for (const id of ['layout', 'signins', 'schedules', 'browser-button']) $(id).inert = open;
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
    const controls = [...panel.querySelectorAll('button:not(:disabled), iframe:not([hidden])')];
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
syncDrawer();
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
  div.innerHTML = '<span class="monty-mark" aria-hidden="true">m<span>•</span></span>' +
    '<h2>What can I take off your list?</h2>' +
    '<p>Give me a task on the web. I’ll work in my browser and ask when I need your help.</p>';
  const suggestions = document.createElement('div');
  suggestions.className = 'suggestions';
  const examples = [
    ['↗', 'Find something worth the trip', 'Find the three cheapest flights to Lisbon next Friday.'],
    ['▣', 'Pick up where you left off', 'Check the status of my last order on the shop.'],
    ['◷', 'Make it a regular thing', 'Every Tuesday at 9, check the price of my usual shopping list.'],
  ];
  for (const [icon, title, text] of examples) {
    const suggestion = button('', 'suggestion', () => {
      $('message').value = text;
      $('message').focus();
    });
    const symbol = document.createElement('span');
    symbol.textContent = icon;
    symbol.setAttribute('aria-hidden', 'true');
    const copy = document.createElement('span');
    const heading = document.createElement('strong');
    heading.textContent = title;
    const detail = document.createElement('small');
    detail.textContent = text;
    copy.append(heading, detail);
    suggestion.append(symbol, copy);
    suggestions.append(suggestion);
  }
  div.append(suggestions);
  return div;
}

async function openThread(id) {
  stopPolling();
  state.thread = id;
  state.run = null;
  state.liveAsk = null;
  stopWatching();
  hideBrowser();
  $('ask').hidden = true;
  $('ask').dataset.id = '';
  $('status').hidden = true;
  if (id === null) {
    renderRun(null);
    $('title').textContent = 'New chat';
    $('messages').replaceChildren(emptyChat());
    return;
  }
  await refresh();
}

async function refresh() {
  const id = state.thread;
  if (id === null) return;
  let thread;
  try {
    thread = await api(`/api/threads/${id}`);
  } catch (error) {
    if (state.thread !== id) return;
    if (error.status === 404) { location.hash = '#/new'; return; }
    throw error;
  }
  if (state.thread !== id) return;  // the user opened another chat meanwhile
  if (!$('layout').hidden) $('title').textContent = thread.title || 'Monty';
  const box = $('messages');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.replaceChildren(...thread.messages.map((m) => message(m.role, m.text)));
  if (atBottom) box.scrollTop = box.scrollHeight;
  renderRun(thread.run);
  const active = thread.run && ['queued', 'running', 'waiting'].includes(thread.run.status);
  if (active) schedulePoll(); else stopPolling();
}

function schedulePoll() {
  // One refresh at a time: the next is scheduled when this one has finished.
  if (state.poll !== null) return;
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
  if (working) $('status').textContent = run.activity.length ? run.activity[run.activity.length - 1] : 'Working…';
  $('send').disabled = Boolean(run && ['queued', 'running', 'waiting'].includes(run.status));
  renderAsk(run && run.status === 'waiting' ? run.ask : null);
  if (working && run.activity.length) watchBrowser(); else stopWatching();
  updateBrowserButton();
}

function updateBrowserButton() {
  // Opens the bot's browser while it works and the panel is closed.
  const run = state.run;
  const working = run && (run.status === 'queued' || run.status === 'running') && run.activity.length;
  $('browser-button').hidden = !(working && $('browser').hidden);
  const mobileBrowser = !desktop.matches && !$('browser').hidden && !$('layout').hidden;
  $('chat').inert = mobileBrowser;
  document.querySelector('.bar').inert = mobileBrowser;
  if (mobileBrowser) $('drawer').inert = true; else syncDrawer();
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
    input.setAttribute('aria-label', 'Your answer to Monty');
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
  await api(`/api/asks/${ask.id}`, { method: 'POST', body });
  $('ask').hidden = true;
  $('ask').dataset.id = '';
  schedulePoll();
  await refresh();
}

async function takeOver(ask) {
  const link = await api(`/api/runs/${state.run.id}/live`, { method: 'POST', body: {} });
  state.liveAsk = ask.id;
  stopWatching();
  $('browser-label').textContent = 'You have the browser. Give it back when you are done.';
  $('screen').hidden = true;
  $('live').hidden = false;
  $('live').src = link.url;
  $('browser').hidden = false;
  updateBrowserButton();
  $('close-browser').focus();
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
  updateBrowserButton();
}

$('browser-button').addEventListener('click', () => { showScreen(); $('close-browser').focus(); });
$('close-browser').addEventListener('click', () => {
  $('browser').hidden = true;
  updateBrowserButton();
  if (!$('browser-button').hidden) $('browser-button').focus(); else $('message').focus();
});

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
  }) : [Object.assign(document.createElement('li'), { textContent: 'No saved sign-ins yet. Sign in through browser takeover when Monty asks.' })]));
  $('signins').hidden = false;
  $('signins-title').focus();
}

async function openSchedules() {
  const list = $('schedule-list');
  let schedules = [];
  try { schedules = await api('/api/schedules'); } catch (error) { if (error.status !== 404) throw error; }
  list.replaceChildren(...(schedules.length ? schedules.map((s) => {
    const item = document.createElement('li');
    const name = document.createElement('span');
    const title = document.createElement('strong');
    title.textContent = s.name;
    const detail = document.createElement('span');
    detail.className = 'list-detail';
    detail.textContent = `${s.when}${s.paused ? ' (paused)' : ''}`;
    name.append(title, detail);
    const actions = document.createElement('span');
    actions.className = 'list-actions';
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
  }) : [Object.assign(document.createElement('li'), { textContent: 'No scheduled tasks yet. Tell Monty what to do and when in a chat.' })]));
  $('schedules').hidden = false;
  $('schedules-title').focus();
}

$('open-signins').addEventListener('click', () => { location.hash = '#/sign-ins'; closeDrawer(); });
$('open-schedules').addEventListener('click', () => { location.hash = '#/schedules'; closeDrawer(); });
for (const back of document.querySelectorAll('.page .back')) {
  back.addEventListener('click', () => { location.hash = state.thread ? `#/t/${state.thread}` : '#/new'; });
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
  const isPage = hash === '#/sign-ins' || hash === '#/schedules';
  $('layout').hidden = isPage;
  updateBrowserButton();
  for (const [id, path] of [['open-signins', '#/sign-ins'], ['open-schedules', '#/schedules']]) {
    if (hash === path) $(id).setAttribute('aria-current', 'page'); else $(id).removeAttribute('aria-current');
  }
  if (isPage) $('title').textContent = hash === '#/sign-ins' ? 'Saved sign-ins' : 'Schedules';
  if (isPage) {
    await loadThreads();
    return hash === '#/sign-ins' ? openSignins() : openSchedules();
  }
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
