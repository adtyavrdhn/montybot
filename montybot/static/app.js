// The Monty web app: chat with Monty, watch its browser, take over when it asks. No build step, no framework.
//
// Staying correct while the user moves around: `newPage()` tears down everything that belongs to what is on screen.
// It aborts `page`, whose signal every read (`api()` GET) uses, and closes the run's event stream and the browser
// screenshots, so an old chat can never draw over a new one. The user's own actions (sending, answering,
// stopping) are not tied to it: they always finish.
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
  let response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('Could not reach Monty. Check your connection, and try again.');  // not the browser's words
  }
  const data = (response.headers.get('Content-Type') || '').includes('application/json') ? await response.json() : null;
  if (response.status === 401 && !['/api/signin', '/api/me'].includes(path)) signedOut();
  if (!response.ok) {
    const error = new Error(problem(response.status, data && typeof data.detail === 'string' ? data.detail : null));
    error.status = response.status;
    throw error;
  }
  return data;
}

function problem(status, detail) {
  // The server's words where they are meant for the user; plain ones where they are not.
  if (status === 404) return 'That is no longer here. It may have been deleted, or the task may have finished.';
  if (status === 422) return 'Please check what you typed, and try again.';
  return detail || `Something went wrong (${status}). Please try again.`;
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
  const load = ++state.threadLoads;
  const threads = await api('/api/threads');
  if (load !== state.threadLoads) return;  // a later load is drawing the list
  const open = $('layout').hidden ? null : threads.find((thread) => thread.id === state.threadId);
  if (open && open.status && !(state.run && ACTIVE.includes(state.run.status))) {
    report(loadChat());  // a run started in the open chat elsewhere (a schedule, another tab): show it
  }
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
  if (events && events.runId === run.id && events.readyState !== EventSource.CLOSED) return;
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
    // EventSource reconnects by itself, and the server ends each stream every few minutes on purpose. Only if it is
    // still not back after a moment: warn, and reload the chat (which says whether the run ended or the user was
    // signed out).
    setTimeout(() => {
      if (!live() || source.readyState === EventSource.OPEN) return;
      state.draftLost = true;
      renderStatus();
      renderDraft();
      report(loadChat());
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
  } else {
    row.append(button('Take over the browser', '', () => takeOver(ask)));
  }
  box.replaceChildren(element('p', ask.prompt), row);  // the server's push tells the user if they are away
}

async function answer(ask, body) {
  const before = page;
  const buttons = [...$('ask').querySelectorAll('button')];
  for (const each of buttons) each.disabled = true;  // Approve and Deny together: one answer only
  try {
    await api(`/api/asks/${ask.id}`, { method: 'POST', body });
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
  const before = page;
  let link;
  try {
    link = await api(`/api/runs/${state.run.id}/live`, { method: 'POST', body: {} });
  } catch (error) {
    if (error.status !== 404) throw error;
    if (page === before) await loadChat();  // the hand-off ended meanwhile: show where the run is now
    return;
  }
  if (page !== before || !state.run || !state.run.ask || state.run.ask.id !== ask.id) return;  // left, or it ended
  state.takeoverAskId = ask.id;
  $('live').src = link.url;
  // A modal dialog: the page behind cannot be reached. The live view's own "Back to chat" closes it.
  $('takeover').showModal();
  $('live').focus();
}

function closeTakeover() {
  // The hand-off goes on until the user gives the browser back; "Take over" opens it again.
  if ($('takeover').open) $('takeover').close();
}
window.addEventListener('message', (event) => {
  // The live view's own "Back to chat" button.
  if (event.source !== $('live').contentWindow || event.origin !== location.origin) return;
  if (event.data && event.data.kind === 'close-takeover') closeTakeover();
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

async function send(text) {
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
    (data.files.length ? '' : 'No files yet. Ask Monty to download or make a file.');
  $('file-list').replaceChildren(...data.files.map((file) => {
    const tooLarge = file.size > data.max_download_bytes;
    const download = button('Download', 'secondary', () => downloadFile(file.path));
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
    $('files-status').textContent = response.status === 413 ? 'This file is over the 20 MB download limit.' :
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
      button('Open chat', 'secondary', () => { location.hash = `#/t/${s.thread_id}`; }),
      button(s.paused ? 'Resume' : 'Pause', 'secondary', async () => {
        await api(`/api/schedules/${s.id}/${s.paused ? 'resume' : 'pause'}`, { method: 'POST', body: {} });
        await openSchedules();
      }),
      button('Delete', 'bad', async () => {
        if (!confirm(`Delete "${s.name}"? Monty will stop running it.`)) return;
        await api(`/api/schedules/${s.id}`, { method: 'DELETE' });
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

async function enableNotifications() {
  if (await Notification.requestPermission() !== 'granted') {
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

$('enable-notifications').addEventListener('click', () => report(enableNotifications()));

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
  try {
    await api('/api/me');
  } catch (error) {
    show('signin');
    return;
  }
  show('main');
  report(showNotificationButton());
  await route();
}

window.addEventListener('hashchange', () => { if (!$('main').hidden) report(route()); });
report(start());
