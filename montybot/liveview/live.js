// The live view page: draws frames from the hand-off's WebSocket and sends the user's input back.
// The protocol is in wire.py.
'use strict';

const view = document.getElementById('view');
const context = view.getContext('2d');
const keys = document.getElementById('keys');
const reason = document.getElementById('reason');
const giveBack = document.getElementById('give-back');
const tabs = document.getElementById('tabs');
const url = document.getElementById('url');
const status = document.getElementById('status');
const useHere = document.getElementById('use-here');

const BUTTONS = ['left', 'middle', 'right'];
const MODIFIERS = ['Alt', 'Control', 'Meta', 'Shift'];
const NAMED_KEYS = new Set([
  'Enter', 'Tab', 'Backspace', 'Escape', 'Delete', 'Insert', 'ArrowLeft', 'ArrowUp', 'ArrowRight', 'ArrowDown',
  'Home', 'End', 'PageUp', 'PageDown', 'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12',
]);

let socket = null;
let size = { width: 0, height: 0 };
let shown = 0;
let retries = 0;
let finished = false;
let pendingMove = null;

function socketUrl() {
  const address = new URL(location.href);
  address.protocol = address.protocol === 'https:' ? 'wss:' : 'ws:';
  address.pathname = address.pathname.replace(/\/$/, '') + '/ws';
  address.search = '';
  address.hash = '';
  return address.href;
}

function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

function connect() {
  useHere.hidden = true;
  socket = new WebSocket(socketUrl());
  socket.binaryType = 'arraybuffer';
  socket.onopen = () => { retries = 0; status.textContent = 'You are driving the browser.'; };
  socket.onmessage = (event) => {
    if (typeof event.data === 'string') onMessage(JSON.parse(event.data));
    else onFrame(event.data);
  };
  socket.onclose = (event) => onClose(event.code);
}

function onMessage(message) {
  if (message.kind === 'hello') {
    reason.textContent = 'montybot needs you: ' + message.reason;
    giveBack.disabled = false;
  } else if (message.kind === 'tabs') {
    tabs.replaceChildren(...message.tabs.map((tab) => new Option(tab.title || tab.url, tab.tab_id, false, tab.active)));
    tabs.hidden = message.tabs.length < 2;
    const active = message.tabs.find((tab) => tab.active);
    url.textContent = active ? active.url : '';
  } else if (message.kind === 'error') {
    status.textContent = message.message;
  } else if (message.kind === 'ended') {
    finish(message.given_back ? 'Thanks. The bot has the browser again; you can close this page.'
                              : 'This hand-off is over.');
  }
}

async function onFrame(buffer) {
  const length = new DataView(buffer).getUint32(0);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 4, length)));
  const bitmap = await createImageBitmap(new Blob([new Uint8Array(buffer, 4 + length)], { type: header.mime }));
  if (header.seq < shown) { bitmap.close(); return; }  // decoded out of order; a newer frame is already up
  shown = header.seq;
  size = { width: header.width, height: header.height };
  if (view.width !== bitmap.width || view.height !== bitmap.height) {
    view.width = bitmap.width;
    view.height = bitmap.height;
  }
  context.drawImage(bitmap, 0, 0);
  bitmap.close();
}

function onClose(code) {
  socket = null;
  if (finished) return;
  if (code === 4410) return finish('This hand-off is over.');
  if (code === 4404) return finish('There is no such hand-off for you.');
  if (code === 4401) return finish('Sign in, then open this link again.');
  if (code === 4409) {
    status.textContent = 'You opened this hand-off somewhere else.';
    useHere.hidden = false;
    return;
  }
  status.textContent = 'Connection lost. Reconnecting...';
  setTimeout(connect, Math.min(500 * 2 ** retries++, 10000));
}

function finish(text) {
  finished = true;
  status.textContent = text;
  giveBack.disabled = true;
  if (socket) socket.close();
}

// --- input ---

function point(event) {
  const box = view.getBoundingClientRect();
  return {
    x: (event.clientX - box.left) * size.width / box.width,
    y: (event.clientY - box.top) * size.height / box.height,
  };
}

function flushMove() {
  if (pendingMove) send({ kind: 'mouse_move', ...pendingMove });
  pendingMove = null;
}

view.addEventListener('pointerdown', (event) => {
  event.preventDefault();
  view.setPointerCapture(event.pointerId);
  keys.focus({ preventScroll: true });
  flushMove();
  send({ kind: 'mouse_down', ...point(event), button: BUTTONS[event.button] || 'left' });
});
view.addEventListener('pointermove', (event) => {
  if (!pendingMove) requestAnimationFrame(flushMove);
  pendingMove = point(event);
});
view.addEventListener('pointerup', (event) => {
  flushMove();
  send({ kind: 'mouse_up', ...point(event), button: BUTTONS[event.button] || 'left' });
});
view.addEventListener('wheel', (event) => {
  event.preventDefault();
  const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? size.height : 1;
  send({ kind: 'scroll', ...point(event), delta_x: event.deltaX * scale, delta_y: event.deltaY * scale });
}, { passive: false });
view.addEventListener('contextmenu', (event) => event.preventDefault());

keys.addEventListener('keydown', (event) => {
  if (event.isComposing) return;
  const command = event.ctrlKey || event.metaKey || event.altKey;
  if (NAMED_KEYS.has(event.key) || (event.key.length === 1 && command)) {
    event.preventDefault();
    send({ kind: 'press', key: event.key, modifiers: MODIFIERS.filter((m) => event.getModifierState(m)) });
  }
});
keys.addEventListener('input', () => {
  if (keys.value) send({ kind: 'type', text: keys.value });
  keys.value = '';
});

document.getElementById('keyboard').addEventListener('click', () => keys.focus());
tabs.addEventListener('change', () => send({ kind: 'switch_tab', tab_id: tabs.value }));
giveBack.addEventListener('click', () => { giveBack.disabled = true; send({ kind: 'give_back' }); });
useHere.addEventListener('click', connect);

connect();
