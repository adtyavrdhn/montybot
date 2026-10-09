// --- a chat ---

function messageBubble(role, text, files = []) {
  // role: 'user', 'assistant', or 'event' (a line recording an approval or a hand-off). `files`: the user's attached
  // files on their message, or those Sammy shared on its reply.
  const bubble = element('div', '', `msg ${role}`);
  if (role === 'assistant') bubble.append(renderMarkdown(text));
  else if (files.length) bubble.append(...(text ? [element('div', text, 'msg-text')] : []));
  else bubble.textContent = text;
  if (files.length) bubble.append(fileList(files));
  if (role === 'user' && files.length && !text) bubble.classList.add('files-only');
  return bubble;
}

function fileList(files) {
  // Images as pictures, which open full size; any other file as a card that downloads it.
  const list = element('div', '', 'msg-files');
  for (const file of files) {
    const url = `/api/attachments/${file.id}`;
    const picture = file.kind === 'image' && INLINE_IMAGES.includes(file.media_type);  // the server checked its bytes
    const link = element('a', '', picture ? 'file-image' : 'file-card');
    link.title = `${file.name} (${shownSize(file.size)})`;
    if (picture) {
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener';
      const image = element('img');
      image.src = url;
      image.alt = file.name;
      image.loading = 'lazy';
      link.append(image);
    } else {
      link.href = `${url}?download`;
      link.download = file.name;
      link.append(fileBadge(file.name), fileLabel(file.name, shownSize(file.size)));
    }
    link.addEventListener('click', () => telemetry.log('open file', { size: file.size, media_type: file.media_type }));
    list.append(link);
  }
  return list;
}

function fileBadge(name) {
  // The file's extension, as a little document: PDF, CSV, XLSX...
  const extension = name.includes('.') ? name.split('.').pop().slice(0, 4).toUpperCase() : 'FILE';
  const badge = element('span', extension, 'file-badge');
  badge.setAttribute('aria-hidden', 'true');
  return badge;
}

function fileLabel(name, detail) {
  const label = element('span', '', 'file-label');
  label.append(element('strong', name), element('small', detail));
  return label;
}

function emptyChat() {
  const empty = element('div', '', 'empty');
  empty.innerHTML = '<span class="sammy-mark" aria-hidden="true">s<span>•</span></span>' +
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
  $('title').textContent = thread.title || 'Sammy';
  const box = $('messages');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.replaceChildren(...thread.messages.map((m) => messageBubble(m.role, m.text, m.files)));
  if (!thread.messages.length) {  // a scheduled task's chat, before its first run
    box.append(element('p', 'Nothing here yet. Each time this scheduled task runs, what Sammy did shows up here.', 'empty-chat'));
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
  $('message').placeholder = !active ? 'What would you like Sammy to do?'
    : run.status === 'waiting' ? 'Sammy is waiting for you: answer above.' : 'Sammy is on it. Stop it, or wait to send your next message.';
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
    input.setAttribute('aria-label', 'Your answer to Sammy');
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

