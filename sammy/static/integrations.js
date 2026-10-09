function connectCard(ask) {
  // Sammy needs a service connected. The server answers the ask by itself once the sign-in completes; "I've connected
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
  let form = null;
  if (offered.provider === 'mcp' && offered.url && offered.auth === 'token') {
    form = tokenForm(offered, () => {
      form.hidden = true;
      started(`${name} is connected.`);
    });
  } else if (offered.provider === 'mcp' && offered.url) {
    row.append(button(`Connect ${name}`, 'good', async () => {
      const signingIn = await connectPreset(offered);
      started(signingIn ? `Finish signing in to ${name} in the window that opened. Sammy carries on once you have.`
        : `${name} is connected.`);
    }));
  } else if (offered.provider === 'composio') {
    row.append(button(`Connect ${name}`, 'good', async () => {
      await openSignIn('connect app', { app: offered.key }, () => (
        api(`/api/integrations/apps/${encodeURIComponent(offered.key)}/connect`, { method: 'POST', body: {} })));
      started(`Finish signing in to ${name} in the window that opened. Sammy carries on once you have.`);
    }));
  } else if (offered.server_id) {
    row.append(button(`Sign in to ${name}`, 'good', async () => {
      await openSignIn('sign in to mcp server', {}, () => (
        api(`/api/integrations/servers/${offered.server_id}/sign-in`, { method: 'POST', body: {} })));
      started(`Finish signing in to ${name} in the window that opened. Sammy carries on once you have.`);
    }));
  } else {
    note.textContent = `${name} isn't one of the apps Sammy connects in one click. If it has an MCP server, add it in Integrations.`;
    row.append(button('Add an MCP server', 'good', () => {
      state.serverName = name;  // the form starts with it
      location.hash = '#/integrations';
    }));
  }
  row.append(done, notNow);
  return [head, element('p', ask.prompt), ...(form ? [form] : []), note, row];
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
    throw new Error('Sammy sent a sign-in address this page cannot open.');
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

let tokenForms = 0;

function tokenForm(listed, connected) {
  // A listed MCP server that signs in with a token the user pastes (`auth: 'token'`, GitHub's): added under its own
  // name with the token, sent as `Bearer <token>` as pydantic-ai-harness sends it. Nothing opens; the token is not
  // traced, and is not left in the field.
  const form = element('form', '', 'token-form');
  const input = element('input');
  input.id = `token-${tokenForms += 1}`;
  input.type = 'password';
  input.autocomplete = 'off';
  input.maxLength = 4000;
  input.required = true;
  input.placeholder = 'Paste it here';
  const label = element('label', listed.token_hint || `A token for ${listed.name}`);
  label.htmlFor = input.id;
  const submit = element('button', 'Connect', 'good');
  submit.type = 'submit';
  submit.setAttribute('aria-label', `Connect ${listed.name} with this token`);
  const error = element('p', '', 'error');
  error.setAttribute('role', 'alert');
  const fields = element('div', '', 'token-fields');
  fields.append(input, submit);
  form.append(label, fields, error);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    report((async () => {
      error.textContent = '';
      const token = input.value.trim();
      if (!token) {  // spaces pass `required`
        input.focus();
        return;
      }
      submit.disabled = true;
      const headers = { [listed.token_header || 'Authorization']: `Bearer ${token}` };
      try {
        await telemetry.span('connect mcp server', { app: listed.key, with_token: true }, () => (
          api('/api/integrations/servers', { method: 'POST', body: { name: listed.name, url: listed.url, headers } })));
      } catch (failed) {
        error.textContent = failed.message;
        return;
      } finally {
        submit.disabled = false;
      }
      input.value = '';
      await connected();
    })());
  });
  return form;
}

async function signInsChanged() {
  // What is connected changed, and a waiting chat may carry on.
  if (location.hash === '#/integrations') await loadIntegrations();
  else if (!$('layout').hidden && state.threadId) await loadChat();
}

if ('BroadcastChannel' in window) {
  // A sign-in finished in another window.
  new BroadcastChannel('sammy-integrations').addEventListener('message', () => reportUnlessOffline(signInsChanged()));
}

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
  if (!connection) add('Connect', 'secondary', () => connectEntry(entry, actions));
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

async function connectEntry(entry, actions) {
  if (entry.provider !== 'mcp') {
    await connectApp(entry.key);
    return;
  }
  if (entry.auth === 'token') {
    toggleTokenForm(entry, actions);
    return;
  }
  const signingIn = await connectPreset(entry);
  if (signingIn) await loadIntegrations();  // listed as needing a sign-in until the window says it is done
  $('integrations-status').textContent = signingIn ? SIGNING_IN : `${entry.name} is connected.`;
}

function toggleTokenForm(entry, actions) {
  // Its Connect opens the field for the token under the row, and closes it again.
  const item = actions.closest('li');
  const opener = actions.querySelector('button');
  const open = item.querySelector('.token-form');
  if (open) open.remove();
  item.classList.toggle('with-form', !open);
  opener.setAttribute('aria-expanded', String(!open));
  if (open) return;
  const form = tokenForm(entry, async () => {
    await loadIntegrations();
    $('integrations-status').textContent = `${entry.name} is connected.`;
  });
  item.append(form);
  form.querySelector('input').focus();
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
  if (!confirm(`${question} Sammy will no longer be able to use it.`)) return;
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

