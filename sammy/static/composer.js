// --- sending ---

$('composer').addEventListener('submit', (event) => {
  event.preventDefault();
  if (modelPicker.saving) {
    showNotice('Wait for your model choice to finish saving before sending.');
    return;
  }
  if ($('send').hidden && !$('ask').hidden) {  // Sammy waits for an answer: take the user there
    $('ask').querySelector('textarea, button').focus();
    return;
  }
  if ($('send').disabled || $('send').hidden) return;  // Enter obeys the same guard as the button
  const text = $('message').value.trim();
  if (state.pending.some((file) => file.status === 'uploading')) {
    showNotice('Your files are still uploading. Send once they are ready.');
    return;
  }
  const files = state.pending.filter((file) => file.status === 'ready');
  if (!text && !files.length) { $('message').focus(); return; }
  $('send').disabled = true;  // the chat enables it again once it has loaded the new message's run
  report(send(text, files).catch((error) => { $('send').disabled = false; throw error; }));
});

function send(text, files) {
  const threadId = state.threadId;
  const attributes = { thread_id: threadId, new_chat: threadId === null, text, files: files.length };
  return telemetry.span('send message', attributes, (note) => sendMessage(text, files, note));
}

async function sendMessage(text, files, note) {
  const before = page;
  const threadId = state.threadId;
  const path = threadId === null ? '/api/threads' : `/api/threads/${threadId}/messages`;
  let created;
  try {
    const body = { text, timezone: TIMEZONE };
    if (files.length) body.attachments = files.map((file) => file.id);
    created = await api(path, { method: 'POST', body });
  } catch (error) {
    if (page !== before) throw error;
    if (error.status === 409) await loadChat();  // Sammy started on this chat elsewhere (a schedule, another tab)
    if (error.status !== 404 || threadId === null) throw error;
    // The chat was deleted with its schedule; the message is still in the box. Show the notice once the new
    // chat has opened, as opening a page clears notices.
    location.hash = '#/new';
    await new Promise((resolve) => window.addEventListener('hashchange', resolve, { once: true }));
    throw new Error('That chat was deleted. Send your message again to start a new chat.');
  }
  note({ thread_id: created.thread_id, run_id: created.run_id });
  if ($('message').value.trim() === text) $('message').value = '';
  for (const file of files) removeFile(file);  // sent: they are in the chat now
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
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && desktop.matches) {  // not mid-word in an IME
    event.preventDefault();
    $('composer').requestSubmit();
  }
});

// --- files for the next message: dropped, pasted or picked ---
//
// Each file uploads as soon as it is added, so it is ready by the time the user has typed; the message names the
// uploads it goes with. Upload progress needs XMLHttpRequest: fetch cannot report it.

const MAX_FILES = 10;
const MAX_FILE_BYTES = 20 * 1024 * 1024;
const INLINE_IMAGES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];
let fileKeys = 0;

function addFiles(files, how) {
  const room = MAX_FILES - state.pending.length;
  const added = [...files];
  if (added.length > room) showNotice(`You can send up to ${MAX_FILES} files with one message.`);
  for (const file of added.slice(0, Math.max(room, 0))) {
    const item = { key: ++fileKeys, file, name: file.name || 'file', size: file.size, type: file.type };
    if (file.size > MAX_FILE_BYTES) {
      item.status = 'failed';
      item.error = 'Over the 20 MB limit';
    } else if (!file.size) {
      item.status = 'failed';
      item.error = 'This file is empty';
    } else {
      item.status = 'uploading';
      item.progress = 0;
      uploadFile(item, how);
    }
    if (INLINE_IMAGES.includes(file.type)) item.preview = URL.createObjectURL(file);
    state.pending.push(item);
  }
  renderPending();
}

function uploadFile(item, how) {
  const request = new XMLHttpRequest();
  item.request = request;
  const end = telemetry.begin('attach file', { size: item.size, how });  // the size only: names stay out of telemetry
  request.open('POST', '/api/attachments');
  request.setRequestHeader('X-Filename', encodeURIComponent(item.name));
  request.setRequestHeader('Content-Type', item.type || 'application/octet-stream');
  request.responseType = 'json';
  request.upload.onprogress = (event) => {
    if (!event.lengthComputable) return;
    item.progress = event.loaded / event.total;
    renderPending();
  };
  request.onload = () => {
    end({ 'http.response.status_code': request.status });
    if (request.status === 401) { signedOut(); return; }
    const data = request.response || {};
    if (request.status === 201) {
      Object.assign(item, { status: 'ready', id: data.id, kind: data.kind });
    } else {
      item.status = 'failed';
      item.error = request.status === 413 ? 'Over the 20 MB limit' : (data.detail || 'Upload failed');
    }
    renderPending();
  };
  request.onerror = () => {
    end({ outcome: 'offline' });
    item.status = 'failed';
    item.error = 'Upload failed: check your connection';
    renderPending();
  };
  request.send(item.file);
}

function removeFile(item) {
  if (item.request && item.status === 'uploading') item.request.abort();
  if (item.preview) URL.revokeObjectURL(item.preview);
  state.pending = state.pending.filter((each) => each !== item);
  renderPending();
}

const KIND_NOTES = { image: 'Sammy sees it', pdf: 'Sammy reads it', text: 'Sammy reads it', file: 'Sammy opens it with code' };

function renderPending() {
  const list = $('attachments');
  list.hidden = !state.pending.length;
  list.replaceChildren(...state.pending.map((item) => {
    const chip = element('li', '', `attachment ${item.status}`);
    chip.append(item.preview ? Object.assign(element('img'), { src: item.preview, alt: '' }) : fileBadge(item.name));
    const detail = item.status === 'uploading' ? `Uploading… ${Math.round((item.progress || 0) * 100)}%`
      : item.status === 'failed' ? item.error : `${shownSize(item.size)} · ${KIND_NOTES[item.kind] || ''}`;
    chip.append(fileLabel(item.name, detail));
    if (item.status === 'uploading') {
      const bar = element('span', '', 'progress');
      bar.style.setProperty('--done', `${Math.round((item.progress || 0) * 100)}%`);
      chip.append(bar);
    }
    const remove = element('button', '✕', 'icon');
    remove.type = 'button';
    remove.setAttribute('aria-label', `Remove ${item.name}`);
    remove.addEventListener('click', () => { removeFile(item); $('message').focus(); });
    chip.append(remove);
    return chip;
  }));
  const uploading = state.pending.some((item) => item.status === 'uploading');
  $('send').title = uploading ? 'Waiting for your files to upload' : '';
}

function shownSize(bytes) {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

$('attach').addEventListener('click', () => $('file-input').click());
$('file-input').addEventListener('change', () => {
  addFiles($('file-input').files, 'picked');
  $('file-input').value = '';  // the same file can be picked again
  $('message').focus();
});

$('message').addEventListener('paste', (event) => {
  // A screenshot or a copied file; pasted text stays text.
  const files = [...(event.clipboardData ? event.clipboardData.files : [])];
  if (!files.length) return;
  event.preventDefault();
  const stamp = new Date().toTimeString().slice(0, 8).replaceAll(':', '.');
  addFiles(files.map((file) => (file.name && file.name !== 'image.png' ? file
    : new File([file], `Pasted image ${stamp}.${(file.type.split('/')[1] || 'png').replace('jpeg', 'jpg')}`, { type: file.type }))), 'pasted');
});

// Dropping files anywhere on the chat attaches them; elsewhere it does nothing (rather than the browser opening the
// file in place of the app). `dragDepth` counts the elements the drag is over, as each fires its own enter and leave.
let dragDepth = 0;
const draggingFiles = (event) => Boolean(event.dataTransfer && [...event.dataTransfer.types].includes('Files'));
const canDrop = () => !$('main').hidden && !$('layout').hidden && !$('takeover').open;

window.addEventListener('dragenter', (event) => {
  if (!draggingFiles(event)) return;
  event.preventDefault();
  dragDepth += 1;
  $('drop-zone').hidden = !canDrop();
});
window.addEventListener('dragleave', (event) => {
  if (!draggingFiles(event)) return;
  dragDepth = Math.max(dragDepth - 1, 0);
  if (!dragDepth) $('drop-zone').hidden = true;
});
window.addEventListener('dragover', (event) => {
  if (!draggingFiles(event)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = canDrop() ? 'copy' : 'none';
});
window.addEventListener('drop', (event) => {
  if (!draggingFiles(event)) return;
  event.preventDefault();
  dragDepth = 0;
  $('drop-zone').hidden = true;
  if (!canDrop()) return;
  addFiles(event.dataTransfer.files, 'dropped');
  $('message').focus();
});

