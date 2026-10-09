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
  }) : [element('li', 'No saved browser data yet. Sign in through browser takeover when Sammy asks.')]));
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
        if (!confirm(`Delete "${s.name}"? Sammy will stop running it.`)) return;
        await telemetry.span('delete schedule', { schedule_id: s.id, thread_id: s.thread_id }, () => (
          api(`/api/schedules/${s.id}`, { method: 'DELETE' })));
        await Promise.all([openSchedules(), loadThreads()]);  // its chat goes too if it never ran
      }),
    );
    const item = element('li');
    item.append(name, actions);
    return item;
  }) : [element('li', 'No scheduled tasks yet. Tell Sammy what to do and when in a chat.')]));
}

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
  $('notifications-label').textContent = 'Notify me when Sammy needs me';
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

const PAGES = { '#/settings': ['settings', 'Settings', openSettings], '#/sign-ins': ['signins', 'Saved browser data', openSignins],
  '#/integrations': ['integrations', 'Integrations', openIntegrations], '#/schedules': ['schedules', 'Schedules', openSchedules] };
const PAGE_BUTTONS = { '#/settings': 'open-settings', '#/sign-ins': 'open-signins', '#/integrations': 'open-integrations', '#/schedules': 'open-schedules' };

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
    report(loadModelPreferences());
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
