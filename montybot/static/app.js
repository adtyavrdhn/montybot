// monty-bot web app: chat with the bot, watch its browser, take over when it asks. No build step, no framework.
//
// Staying correct while the user moves around: everything that belongs to what is on screen (its requests, the run's
// event stream, the browser screenshots) is tied to `page.signal`. Opening another chat or page aborts it, which
// cancels those requests and closes the stream, so an old chat can never draw over a new one. The user's own actions
// (sending, answering, stopping) are not tied to it: they always finish.
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
  takeoverAskId: null,  // the hand-off whose live view is open
  browserClosed: false,  // the user closed the browser panel in this chat, so it does not open by itself again
  threadsShown: '',  // the chat list as last drawn, so an unchanged list is not redrawn under the user's focus
  signingUp: false,
};
let page = new AbortController();
let events = null;  // the open run's EventSource
let watching = null;  // the AbortController of the screenshot loop, while it runs

function newPage() {
  page.abort();
  page = new AbortController();
  closeEvents();
  stopWatching();
  closeTakeover();
  hideNotice();
}

async function api(path, { method = 'GET', body } = {}) {
  // Reads belong to the page on screen; actions always finish.
  const init = { method, credentials: 'same-origin', headers: {}, signal: method === 'GET' ? page.signal : undefined };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const data = (response.headers.get('Content-Type') || '').includes('application/json') ? await response.json() : null;
  if (response.status === 401 && !['/api/signin', '/api/me'].includes(path)) signedOut();
  if (!response.ok) {
    const error = new Error(data && typeof data.detail === 'string' ? data.detail : `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function report(promise) {
  // For event handlers: show what went wrong, but not for requests of a page the user has left.
  promise.catch((error) => {
    if (error.name === 'AbortError') return;
    console.error(error);
    showNotice(error.message);
  });
}

function showNotice(text) {
  $('notice-text').textContent = text;
  $('notice').hidden = false;
}
function hideNotice() { $('notice').hidden = true; }
$('close-notice').addEventListener('click', hideNotice);

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

$('signout').addEventListener('click', () => report(signOut()));

async function signOut() {
  signedOut();
  try {
    await stopNotifications();
  } catch (error) {
    console.error(error);  // signing out matters more than the push subscription
  }
  await api('/api/signout', { method: 'POST', body: {} });
  location.hash = '';
  location.reload();
}

// --- the chat list ---

async function loadThreads() {
  const threads = await api('/api/threads');
  const shown = JSON.stringify([state.threadId, threads]);
  if (shown === state.threadsShown) return;
  state.threadsShown = shown;
  const badges = { waiting: 'Needs you', running: 'Working', queued: 'Working' };
  $('threads').replaceChildren(...threads.map((thread) => {
    const open = element('button', '', thread.id === state.threadId ? 'current' : '');
    open.title = thread.title || 'Untitled';
    open.append(element('span', thread.title || 'Untitled', 'thread-title'));
    if (badges[thread.status]) open.append(element('span', badges[thread.status], `badge ${thread.status}`));
    if (thread.id === state.threadId) open.setAttribute('aria-current', 'page');
    open.addEventListener('click', () => { location.hash = `#/t/${thread.id}`; closeDrawer(); });
    const item = element('li');
    item.append(open);
    return item;
  }));
  if (!threads.length) $('threads').append(element('li', 'Your next task starts with a new chat.', 'thread-empty'));
}

setInterval(() => {
  // Another chat may start needing the user at any time.
  if (!$('main').hidden && document.visibilityState === 'visible') report(loadThreads());
}, 15000);

// --- the drawer, on small screens ---

function syncDrawer() {
  const open = !desktop.matches && $('drawer').classList.contains('open');
  $('drawer').inert = !desktop.matches && !open;
  $('drawer-backdrop').hidden = !open;
  $('menu-button').setAttribute('aria-expanded', String(open));
  for (const id of ['layout', 'files', 'signins', 'schedules', 'browser-button']) $(id).inert = open;
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
$('new-chat').addEventListener('click', () => { location.hash = '#/new'; closeDrawer(); });

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
  if (threadId === null) {
    $('title').textContent = 'New chat';
    $('messages').replaceChildren(emptyChat());
    renderRun(null);
    return;
  }
  renderRun(null);
  $('send').disabled = true;  // until the chat has loaded and says whether it is still working
  const mine = page;
  try {
    await loadChat();
  } catch (error) {
    if (page === mine) $('send').disabled = false;  // let the user try again
    throw error;
  }
}

async function loadChat() {
  const load = ++state.chatLoads;
  let thread;
  try {
    thread = await api(`/api/threads/${state.threadId}`);
  } catch (error) {
    if (error.status === 404) { location.hash = '#/new'; return; }
    throw error;
  }
  if (load !== state.chatLoads) return;  // a later load is drawing this chat
  $('title').textContent = thread.title || 'Monty';
  const box = $('messages');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.replaceChildren(...thread.messages.map((m) => messageBubble(m.role, m.text)));
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
  $('status').textContent = state.draftLost ? `${now} · Live preview unavailable; checking for updates…` : now;
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
  if (events && events.runId === run.id) return;
  closeEvents();
  const source = new EventSource(`/api/runs/${run.id}/events`);
  source.runId = run.id;
  events = source;
  const live = () => events === source;  // events may still arrive after it closed
  source.onopen = () => {
    if (!live()) return;
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
    renderRun(status);
    report(loadThreads());
    if (!ACTIVE.includes(status.status)) {
      closeEvents();  // and the draft: the saved reply replaces it
      report(loadChat());
    }
  });
  source.onerror = () => {
    if (!live()) return;
    // EventSource reconnects by itself. A reload of the chat tells us if the run ended or the user was signed out.
    state.draftLost = true;
    renderStatus();
    renderDraft();
    setTimeout(() => { if (live()) report(loadChat()); }, 2000);
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
  report(api(`/api/runs/${run.id}/stop`, { method: 'POST', body: {} })
    .catch((error) => { if (error.status !== 409) throw error; })  // it finished meanwhile
    .then(() => { if (page === before) return Promise.all([loadChat(), loadThreads()]); })
    .finally(() => { $('stop').disabled = false; }));
});

// --- what the bot asks ---

function renderAsk(ask) {
  const box = $('ask');
  if (!ask || ask.id !== state.takeoverAskId) closeTakeover();  // that hand-off is over
  if (ask === null) {
    box.hidden = true;
    box.dataset.id = '';
    return;
  }
  if (box.dataset.id === ask.id) return;  // already shown: keep what the user is typing
  box.dataset.id = ask.id;
  box.hidden = false;
  const row = element('div', '', 'row');
  if (ask.kind === 'question') {
    const input = element('textarea');
    input.rows = 2;
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
  } else {
    row.append(button('Take over the browser', '', () => takeOver(ask)));
  }
  box.replaceChildren(element('p', ask.prompt), row);
  notify(ask);
}

async function answer(ask, body) {
  const before = page;
  await api(`/api/asks/${ask.id}`, { method: 'POST', body });
  if (page !== before) return;  // the user went elsewhere meanwhile
  if (state.run && state.run.ask && state.run.ask.id === ask.id) {
    $('ask').hidden = true;
    $('ask').dataset.id = '';
  }
  await loadChat();
}

async function takeOver(ask) {
  const link = await api(`/api/runs/${state.run.id}/live`, { method: 'POST', body: {} });
  if (!state.run || !state.run.ask || state.run.ask.id !== ask.id) return;  // the ask ended meanwhile
  stopWatching();
  state.takeoverAskId = ask.id;
  $('live').src = link.url;
  // A modal dialog: the page behind cannot be reached and Escape closes it.
  $('takeover').showModal();
  $('live').focus();
}

function closeTakeover() {
  // The hand-off goes on until the user gives the browser back; "Take over" opens it again.
  if ($('takeover').open) $('takeover').close();
}
window.addEventListener('message', (event) => {
  // The live view's own "Back to chat" button.
  if (event.origin === location.origin && event.data && event.data.kind === 'close-takeover') closeTakeover();
});
$('takeover').addEventListener('close', () => {  // also after Escape
  state.takeoverAskId = null;
  $('live').src = 'about:blank';
  const takeOverAgain = $('ask').hidden ? null : $('ask').querySelector('button');
  (takeOverAgain || $('message')).focus();
});

// --- the bot's browser, while it works ---

function startWatching() {
  // A screenshot a second while the browser panel is open.
  if (watching) return;
  watching = new AbortController();
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
  if (old.startsWith('blob:')) URL.revokeObjectURL(old);
}

function stopWatching() {
  if (watching) watching.abort();
  watching = null;
  $('browser').hidden = true;
  const old = $('screen').src || '';
  $('screen').removeAttribute('src');
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
  if ($('send').disabled || $('send').hidden) return;  // Enter obeys the same guard as the button
  const text = $('message').value.trim();
  if (!text) return;
  $('send').disabled = true;  // the chat enables it again once it has loaded the new message's run
  report(send(text).catch((error) => { $('send').disabled = false; throw error; }));
});

async function send(text) {
  const threadId = state.threadId;
  const path = threadId === null ? '/api/threads' : `/api/threads/${threadId}/messages`;
  const created = await api(path, { method: 'POST', body: { text, timezone: TIMEZONE } });
  if ($('message').value.trim() === text) $('message').value = '';
  if (threadId === null) {
    location.hash = `#/t/${created.thread_id}`;  // opens the new chat
  } else if (state.threadId === threadId) {
    await loadChat();
    await loadThreads();
  }
}

$('message').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && desktop.matches) {
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
    (data.files.length ? '' : 'No files yet. Ask the bot to download or generate a file.');
  $('file-list').replaceChildren(...data.files.map((file) => {
    const tooLarge = file.size > data.max_download_bytes;
    const download = button('Download', 'secondary', () => downloadFile(file.path));
    download.disabled = tooLarge;
    if (tooLarge) download.title = 'Exceeds the 20 MiB download limit';
    const item = element('li');
    item.append(element('span', `${file.path} (${file.size.toLocaleString()} bytes)`), download);
    return item;
  }));
}

async function downloadFile(path) {
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
  if (response.status === 401) { signedOut(); return; }
  if (!response.ok) {
    $('files-status').textContent = response.status === 413 ? 'File exceeds the 20 MiB download limit.' :
      'File unavailable. Refresh and try again.';
    return;
  }
  const url = URL.createObjectURL(await response.blob());
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

// --- saved sign-ins and schedules ---

async function openSignins() {
  $('signins-title').focus();
  const sites = await api('/api/sign-ins');
  $('signin-list').replaceChildren(...(sites.length ? sites.map((s) => {
    const item = element('li');
    item.append(element('span', s.site), button('Forget', 'secondary', async () => {
      await api(`/api/sign-ins/${encodeURIComponent(s.site)}`, { method: 'DELETE' });
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
      button('Open conversation', 'secondary', () => { location.hash = `#/t/${s.thread_id}`; }),
      button(s.paused ? 'Resume' : 'Pause', 'secondary', async () => {
        await api(`/api/schedules/${s.id}/${s.paused ? 'resume' : 'pause'}`, { method: 'POST', body: {} });
        await openSchedules();
      }),
      button('Delete', 'bad', async () => {
        await api(`/api/schedules/${s.id}`, { method: 'DELETE' });
        await openSchedules();
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
for (const back of document.querySelectorAll('.page .back')) {
  back.addEventListener('click', () => { location.hash = state.threadId ? `#/t/${state.threadId}` : '#/new'; });
}

// --- notifications: a push to the phone when the bot needs the user ---

function base64urlBytes(text) {
  const base64 = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4);
  return Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
}

async function enableNotifications() {
  if (!('Notification' in window) || !('serviceWorker' in navigator) || !('PushManager' in window)) {
    throw new Error('This browser cannot show notifications.');
  }
  const key = await api('/api/push/key');
  if (!key.public_key) throw new Error('Notifications are not set up on this server.');
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

$('enable-notifications').addEventListener('click', () => report(enableNotifications()));

function notify(ask) {
  // While the page is open but hidden; a closed page gets the push from the server instead.
  if (document.visibilityState === 'visible' || !('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  const what = { question: 'has a question', approval: 'needs your approval', handoff: 'needs you in its browser' };
  new Notification('Monty', { body: `Monty ${what[ask.kind]}.`, tag: ask.id });
}

// --- routing ---

const PAGES = { '#/files': ['files', 'Files', openFiles], '#/sign-ins': ['signins', 'Saved browser data', openSignins],
  '#/schedules': ['schedules', 'Schedules', openSchedules] };
const PAGE_BUTTONS = { '#/files': 'open-files', '#/sign-ins': 'open-signins', '#/schedules': 'open-schedules' };

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
    updateBrowserButton();
    await Promise.all([found[2](), loadThreads()]);
    return;
  }
  const match = hash.match(/^#\/t\/([0-9a-f-]{36})$/);
  await openChat(match ? match[1] : null);
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
  await route();
}

window.addEventListener('hashchange', () => { if (!$('main').hidden) report(route()); });
report(start());
