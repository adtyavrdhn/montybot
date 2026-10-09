// Chat apps (sammy.channels): link Slack, Telegram, WhatsApp or Discord to this account, and choose where Sammy
// pings. Loaded after app.js, whose helpers ($, api, element, button, report, telemetry) it uses. `#/chat-apps` is
// the page; `#/link/<code>` is the same page asking to link the app that sent the code (the link Sammy sent there).
'use strict';

const CHAT_APP_NAMES = { slack: 'Slack', telegram: 'Telegram', whatsapp: 'WhatsApp', discord: 'Discord' };
let chatAppsNotice = '';  // said once the page opens again, after linking

function chatAppName(name) {
  return CHAT_APP_NAMES[name] || name.charAt(0).toUpperCase() + name.slice(1);
}

function linkCode() {
  const match = location.hash.match(/^#\/link\/([A-Za-z2-7]{10})$/);
  return match ? match[1] : null;
}

async function openChatApps() {
  $('chat-apps-title').focus();
  $('link-card').hidden = !linkCode();
  $('chat-apps-status').textContent = chatAppsNotice;
  chatAppsNotice = '';
  await loadChatApps();
}

async function loadChatApps() {
  const data = await api('/api/channels');
  const linked = new Map(data.linked.map((identity) => [identity.channel, identity]));
  const names = [...new Set([...data.enabled, ...linked.keys()])];
  $('chat-app-list').replaceChildren(...(names.length ? names.map((name) => chatAppRow(name, linked.get(name), data.enabled.includes(name)))
    : [element('li', 'No chat apps are set up on this server yet.')]));
}

function chatAppRow(name, identity, enabled) {
  const about = element('span');
  const detail = identity ? (identity.pings ? 'Linked' : 'Linked. Message Sammy directly there to get pings.')
    : 'Message Sammy there and open the link it sends, or get a code to send it.';
  about.append(element('strong', chatAppName(name)), element('span', detail, 'list-detail'));
  const actions = element('span', '', 'list-actions');
  if (identity) {
    const ping = button(identity.notify ? 'Ping me here: on' : 'Ping me here: off', 'secondary', async () => {
      await telemetry.span('chat app pings', { channel: name, notify: !identity.notify }, () => (
        api(`/api/channels/${encodeURIComponent(name)}`, { method: 'PUT', body: { notify: !identity.notify } })));
      await loadChatApps();
    });
    ping.setAttribute('aria-pressed', String(identity.notify));
    actions.append(ping, button('Unlink', 'bad', async () => {
      if (!confirm(`Unlink ${chatAppName(name)}? Sammy will stop answering you there until you link it again.`)) return;
      await telemetry.span('unlink chat app', { channel: name }, () => (
        api(`/api/channels/${encodeURIComponent(name)}`, { method: 'DELETE' })));
      await loadChatApps();
    }));
  } else if (enabled) {
    actions.append(button('Get a code', 'secondary', async () => {
      // The code is never traced: it links whoever sends it.
      const made = await telemetry.span('chat app code', { channel: name }, () => (
        api(`/api/channels/${encodeURIComponent(name)}/code`, { method: 'POST', body: {} })));
      $('chat-apps-status').textContent = `Send /start ${made.code} to Sammy in ${chatAppName(name)}. The code works for ${made.minutes} minutes, once.`;
    }));
  }
  const item = element('li');
  item.append(about, actions);
  return item;
}

$('open-chat-apps').addEventListener('click', () => { location.hash = '#/chat-apps'; closeDrawer(); });
$('link-app').addEventListener('click', () => report((async () => {
  const code = linkCode();
  if (!code) return;
  $('link-app').disabled = true;
  try {
    const linked = await telemetry.span('link chat app', {}, () => (
      api('/api/channels/link', { method: 'POST', body: { code } })));
    // Only the chat account that got the link can send this code, so opening someone else's link links nothing.
    chatAppsNotice = `To finish, send ${linked.code} to Sammy in ${chatAppName(linked.channel)} within ${linked.minutes} minutes.`;
    location.hash = '#/chat-apps';
  } catch (error) {
    $('link-card').hidden = true;
    $('chat-apps-status').textContent = error.status === 404 ? 'That link has expired or was used already. Message Sammy again for a new one.' : error.message;
  } finally {
    $('link-app').disabled = false;
  }
})()));
