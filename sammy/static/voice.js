// Voice (#137): dictating a message, and hearing Sammy's replies.
//
// Dictation uses the browser's own speech recognition where there is one (Safari on iPhone and Mac, Chrome).
// Elsewhere it records, and the server turns the recording into text if it has a speech provider (`GET /api/voice`).
// The words land in the message box, to read over and send. Replies are read in the server's voice when it has one,
// and in the device's own otherwise; "Read replies aloud" reads each new reply once its task is done, and each
// question Sammy asks while the user watches.
//
// iPhones make sound only from a tap. So the sound is started, silently, in a tap first (`primeSound`), and later
// replies play when they come. A recording goes to the server once and is not kept.
//
// Loaded before app.js; uses its helpers ($, element, report, showNotice, signedOut, telemetry) only once it runs.
'use strict';

const byId = (id) => document.getElementById(id);  // app.js's `$` is not there yet while this file loads

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const SILENCE = 'data:audio/wav;base64,UklGRjQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YRAAAACAgICAgICAgICAgICAgICA';
const MAX_SPEECH_CHARS = 20000;
const MIC_BLOCKED = 'Sammy cannot use your microphone. Allow it in your browser settings, then try again.';
const AUTO_READ_KEY = 'sammy.autoRead';

const voice = {
  server: { transcribe: false, speak: false },  // what the server can do (`GET /api/voice`)
  autoRead: false,  // "Read replies aloud", kept in this browser
  listening: null,  // while dictating: { stop, cancel }
  audio: null,  // the one <audio> the server's voice plays in
  primed: false,  // sound has started in a tap, so it may play later
  reading: null,  // what is being read: { button }, the reply's read-aloud button, or null for a question
};

function canDictate() {
  return Boolean(Recognition) || (voice.server.transcribe && Boolean(window.MediaRecorder && navigator.mediaDevices));
}

function canSpeak() {
  return voice.server.speak || 'speechSynthesis' in window;
}

async function startVoice() {
  // In the background, and never failing: without it, the app is the same, only without voice.
  try {
    voice.autoRead = localStorage.getItem(AUTO_READ_KEY) === 'on';
  } catch { /* storage is off in this browser */ }
  try {
    const response = await fetch('/api/voice', { credentials: 'same-origin' });  // not tied to the page on screen
    if (response.ok) voice.server = await response.json();
  } catch (error) {
    console.warn('Voice from the server is off:', error);
  }
  voice.primed = false;  // primed for the device's voice before the server's was known
  $('dictate').hidden = !canDictate();
  $('auto-read').hidden = !canSpeak();
  renderAutoRead();
  addReadButtons($('messages'));  // the chat may have loaded first
}

// --- dictation ---

byId('dictate').addEventListener('click', () => {
  if (voice.listening) { voice.listening.stop(); return; }
  if (Recognition) dictateInBrowser(); else report(dictateWithServer());
});

function listening(controls, how) {
  voice.listening = controls;
  telemetry.log('dictate', { how });  // never the words
  renderDictate();
}

function listeningEnded() {
  voice.listening = null;
  renderDictate();
  $('message').focus();
}

function renderDictate() {
  const on = Boolean(voice.listening);
  $('dictate').classList.toggle('listening', on);
  $('dictate').setAttribute('aria-pressed', String(on));
  $('dictate').setAttribute('aria-label', on ? 'Stop dictating' : 'Dictate');
  $('dictate').title = on ? 'Stop dictating' : 'Dictate a message';
}

function withWords(before, words) {
  return [before, words.trim()].filter(Boolean).join(' ');
}

function dictateInBrowser() {
  const recognition = new Recognition();
  recognition.lang = navigator.language || 'en-US';
  recognition.continuous = true;
  recognition.interimResults = true;  // the words show as they are heard
  const box = $('message');
  const before = box.value.trimEnd();  // what is typed already: the words go after it
  recognition.onresult = (event) => {
    box.value = withWords(before, Array.from(event.results, (result) => result[0].transcript).join(''));
  };
  recognition.onerror = (event) => {
    if (['not-allowed', 'service-not-allowed'].includes(event.error)) showNotice(MIC_BLOCKED);
    else if (!['aborted', 'no-speech'].includes(event.error)) showNotice('Dictation stopped. Please try again.');
  };
  recognition.onend = listeningEnded;
  recognition.start();
  listening({ stop: () => recognition.stop(), cancel: () => recognition.abort() }, 'browser');
}

async function dictateWithServer() {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    throw new Error(MIC_BLOCKED);
  }
  const recorder = new MediaRecorder(stream);
  const chunks = [];
  let cancelled = false;
  recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
  recorder.onstop = () => {
    for (const track of stream.getTracks()) track.stop();  // the browser's recording light goes off
    listeningEnded();
    if (!cancelled && chunks.length) report(transcribe(new Blob(chunks, { type: recorder.mimeType || chunks[0].type })));
  };
  recorder.start();
  listening({ stop: () => recorder.stop(), cancel: () => { cancelled = true; recorder.stop(); } }, 'server');
}

async function transcribe(recording) {
  const button = $('dictate');
  button.disabled = true;
  button.classList.add('working');
  try {
    await telemetry.span('transcribe', { size: recording.size }, async () => {
      const response = await voiceFetch('/api/voice/transcriptions', recording.type || 'audio/webm', recording);
      const { text } = await response.json();
      $('message').value = withWords($('message').value.trimEnd(), text);
    });
  } finally {
    button.disabled = false;
    button.classList.remove('working');
    $('message').focus();
  }
}

// Sending, or Sammy taking the message, ends dictation: later words must not fill the emptied box again.
byId('composer').addEventListener('submit', () => { if (voice.listening) voice.listening.cancel(); });

// --- reading replies aloud ---

function addReadButtons(box) {
  if (!canSpeak()) return;
  for (const bubble of box.querySelectorAll('.msg.assistant:not(.draft)')) {
    if (!bubble.querySelector('.read-aloud')) bubble.append(readButton(bubble));
  }
}

function readButton(bubble) {
  const made = element('button', '', 'icon read-aloud');
  made.type = 'button';
  made.setAttribute('aria-pressed', 'false');
  made.setAttribute('aria-label', 'Read aloud');
  made.title = 'Read aloud';
  made.innerHTML = '<svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9Z"/>' +
    '<path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/></svg>';
  made.addEventListener('click', () => {
    if (voice.reading && voice.reading.button === made) stopReading();
    else report(readAloud(spokenText(bubble), made, 'button'));
  });
  return made;
}

function spokenText(bubble) {
  // The reply as it reads on screen, without Markdown's marks, its files or its buttons.
  return Array.from(bubble.children)
    .filter((child) => !child.matches('.read-aloud, .msg-files, small'))
    .map((child) => child.innerText.trim())
    .filter(Boolean)
    .join('\n')
    .slice(0, MAX_SPEECH_CHARS);
}

function primeSound() {
  // Call in a tap: an iPhone then lets Sammy speak later, when a reply comes.
  if (voice.primed) return;
  voice.primed = true;
  if (voice.server.speak) {
    if (!voice.audio) voice.audio = new Audio();
    voice.audio.src = SILENCE;
    voice.audio.play().catch(() => { voice.primed = false; });
  } else if ('speechSynthesis' in window) {
    speechSynthesis.speak(new SpeechSynthesisUtterance(''));
  }
}

async function readAloud(text, button, how) {
  // `button`: the reply's read-aloud button, which says Stop reading meanwhile; null for a question.
  stopReading();
  if (!text) return;
  primeSound();  // before anything is awaited: still in the tap, if there was one
  const audio = voice.server.speak ? voice.audio : null;
  const reading = { button };
  voice.reading = reading;
  renderReading();
  telemetry.log('read aloud', { how, chars: text.length, voice: audio ? 'server' : 'device' });  // never the words
  try {
    if (audio) await playFromServer(audio, text, reading); else speakOnDevice(text, reading);
  } catch (error) {
    if (voice.reading === reading) stopReading();
    throw error;
  }
}

async function playFromServer(audio, text, reading) {
  const response = await voiceFetch('/api/voice/speech', 'application/json', JSON.stringify({ text }));
  const speech = await response.blob();
  if (voice.reading !== reading) return;  // stopped, or something else started, meanwhile
  if (audio.src.startsWith('blob:')) URL.revokeObjectURL(audio.src);
  audio.src = URL.createObjectURL(speech);
  audio.onended = () => { if (voice.reading === reading) stopReading(); };
  await audio.play();
}

function speakOnDevice(text, reading) {
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = navigator.language || 'en-US';
  utterance.onend = () => { if (voice.reading === reading) stopReading(); };
  utterance.onerror = utterance.onend;
  speechSynthesis.speak(utterance);
}

function stopReading() {
  if (voice.audio) voice.audio.pause();
  if (voice.reading && 'speechSynthesis' in window) speechSynthesis.cancel();
  voice.reading = null;
  renderReading();
}

function renderReading() {
  for (const each of document.querySelectorAll('.read-aloud')) {
    const on = Boolean(voice.reading) && each === voice.reading.button;
    each.classList.toggle('reading', on);
    each.setAttribute('aria-pressed', String(on));
    each.setAttribute('aria-label', on ? 'Stop reading' : 'Read aloud');
    each.title = on ? 'Stop reading' : 'Read aloud';
  }
}

function readLatestReply() {
  // A task the user watched is done: read its reply, if they asked for that.
  if (!voice.autoRead || !canSpeak()) return;
  const replies = $('messages').querySelectorAll('.msg.assistant:not(.draft)');
  const latest = replies[replies.length - 1];
  const button = latest && latest.querySelector('.read-aloud');
  if (button) report(readAloud(spokenText(latest), button, 'auto'));
}

function readQuestion(ask) {
  // Sammy asked a question in a task the user is watching: read it as it reads a reply, if they asked for that.
  if (!voice.autoRead || !canSpeak()) return;
  report(readAloud(ask.prompt.trim().slice(0, MAX_SPEECH_CHARS), null, 'question'));
}

byId('auto-read').addEventListener('click', () => {
  voice.autoRead = !voice.autoRead;
  try {
    localStorage.setItem(AUTO_READ_KEY, voice.autoRead ? 'on' : 'off');
  } catch { /* kept for this visit only */ }
  if (voice.autoRead) primeSound();
  else stopReading();
  telemetry.log('auto read', { on: voice.autoRead });
  renderAutoRead();
});

function renderAutoRead() {
  $('auto-read').setAttribute('aria-pressed', String(voice.autoRead));
  $('auto-read-label').textContent = voice.autoRead ? 'Reading replies aloud' : 'Read replies aloud';
}

// Any tap readies the sound for the next reply, while "Read replies aloud" is on.
for (const kind of ['click', 'keydown']) {
  document.addEventListener(kind, () => { if (voice.autoRead) primeSound(); }, true);
}

async function voiceFetch(path, contentType, body) {
  // As `api()`, for a body or an answer that is not JSON.
  let response;
  try {
    response = await fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': contentType }, body });
  } catch (error) {
    throw offlineError(error);
  }
  if (response.status === 401) signedOut();
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error((data && typeof data.detail === 'string' && data.detail) || 'Speech is not working right now. Please try again.');
  }
  return response;
}
