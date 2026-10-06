// The agent's view of a page: one walker for every engine (#13). `snapshot.py` explains the format and how to call it.
//
// This file is one function expression. Run it in the tab's top-level document with one argument:
//
//   {op: 'snapshot', budget}                 -> {page, url, title, text}
//   {op: 'locate', ref, page, action, text}  -> {x, y} for a click, {keys} for typing, or {error, detail}
//
// It reads only the DOM and computed styles, never an accessibility tree, so Chromium and Servo give the same text
// for the same page.
(function (request) {
  'use strict';

  const STATE = '__montybotRefs';
  const ATTR = 'data-montybot-ref';
  const NAME_LIMIT = 80;
  const VALUE_LIMIT = 200;
  const OPTION_LIMIT = 20;
  const SKIP = new Set(['script', 'style', 'noscript', 'template', 'head', 'title', 'meta', 'link', 'base']);
  const ROLES = new Set([
    'button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio',
    'option', 'combobox', 'listbox', 'textbox', 'searchbox', 'slider', 'spinbutton', 'treeitem',
  ]);
  const NAMED_BY_CONTENT = new Set([
    'button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio',
    'option', 'treeitem', 'clickable',
  ]);
  // Inputs that take typing as key presses, and inputs whose value is set directly because each engine has its own
  // widget for them.
  const KEY_TYPES = new Set(['text', 'password', 'email', 'tel', 'url', 'search']);
  const VALUE_TYPES = new Set(['number', 'date', 'datetime-local', 'month', 'week', 'time', 'color', 'range']);
  const BUTTON_TYPES = {button: '', submit: 'Submit', reset: 'Reset'};

  const styles = new Map();

  // --- small helpers ---

  const quote = (s) => JSON.stringify(s);
  const normalize = (s) => s.replace(/\s+/g, ' ').trim();
  const clip = (s, n) => (s.length > n ? s.slice(0, n - 3) + '...' : s);
  const attr = (el, name) => (el.getAttribute(name) || '').trim();

  function styleOf(el) {
    let style = styles.get(el);
    if (!style) {
      style = el.ownerDocument.defaultView.getComputedStyle(el);
      styles.set(el, style);
    }
    return style;
  }

  function shown(el) {
    return styleOf(el).display !== 'none';
  }

  function inputType(el) {
    const type = attr(el, 'type').toLowerCase();
    return type === '' ? 'text' : type;
  }

  /** What a node renders: an open shadow root replaces its light children, a slot shows what is assigned, and a
   * closed `<details>` shows only its summary. */
  function childrenOf(node) {
    if (node.shadowRoot) return Array.from(node.shadowRoot.childNodes);
    if (node.localName === 'details' && !node.open) {
      return Array.from(node.children).filter((c) => c.localName === 'summary').slice(0, 1);
    }
    if (node.localName === 'slot' && node.assignedNodes) {
      const assigned = node.assignedNodes({flatten: true});
      if (assigned.length) return Array.from(assigned);
    }
    return Array.from(node.childNodes);
  }

  function isEditableContent(el) {
    const value = el.getAttribute('contenteditable');
    if (value === null || !['', 'true', 'plaintext-only'].includes(value.toLowerCase())) return false;
    const parent = el.parentElement;
    return !(parent && parent.closest('[contenteditable]:not([contenteditable="false"])'));
  }

  /** The role the agent sees, or null for an element it cannot act on. */
  function roleOf(el) {
    const explicit = attr(el, 'role').split(/\s+/)[0].toLowerCase();
    if (ROLES.has(explicit)) return explicit;
    switch (el.localName) {
      case 'a':
        return el.hasAttribute('href') ? 'link' : null;
      case 'button':
      case 'summary':
        return 'button';
      case 'select':
        return el.multiple ? 'listbox' : 'combobox';
      case 'textarea':
        return 'textbox';
      case 'input': {
        const type = inputType(el);
        if (type === 'hidden') return null;
        if (type in BUTTON_TYPES || type === 'image') return 'button';
        if (type === 'checkbox' || type === 'radio') return type;
        if (type === 'range') return 'slider';
        if (type === 'number') return 'spinbutton';
        if (type === 'search') return 'searchbox';
        if (type === 'file') return 'file';
        return 'textbox';
      }
    }
    if (isEditableContent(el)) return 'textbox';
    if (el.hasAttribute('onclick') || (el.hasAttribute('tabindex') && el.tabIndex >= 0)) return 'clickable';
    return null;
  }

  function isControl(el) {
    return ['input', 'select', 'textarea', 'button'].includes(el.localName);
  }

  function byId(root, id) {
    if (root.getElementById) return root.getElementById(id);
    return Array.from(root.querySelectorAll('[id]')).find((el) => el.id === id) || null;
  }

  /** Text for a name: text nodes, `aria-label`s and image `alt`s, skipping what is not displayed. */
  function contentText(node, skipControls) {
    let out = '';
    for (const child of childrenOf(node)) {
      if (child.nodeType === 3) {
        out += child.data;
      } else if (child.nodeType === 1) {
        if (SKIP.has(child.localName) || !shown(child) || (skipControls && isControl(child))) continue;
        const label = attr(child, 'aria-label');
        const inline = styleOf(child).display === 'inline';
        const text = label || (child.localName === 'img' ? attr(child, 'alt') : contentText(child, skipControls));
        out += inline && !label ? text : ' ' + text + ' ';
      }
    }
    return out;
  }

  function labelsOf(el) {
    const root = el.getRootNode();
    const id = el.getAttribute('id');
    const labels = id ? Array.from(root.querySelectorAll('label')).filter((l) => l.getAttribute('for') === id) : [];
    const wrapping = el.parentElement && el.parentElement.closest('label');
    if (wrapping && !labels.includes(wrapping) && (!wrapping.hasAttribute('for') || wrapping.getAttribute('for') === id))
      labels.push(wrapping);
    return labels;
  }

  function labelControl(label) {
    const target = label.getAttribute('for');
    if (target !== null) return byId(label.getRootNode(), target);
    return label.querySelector('input:not([type=hidden]), select, textarea, button');
  }

  function nameOf(el, role) {
    let name = '';
    const ids = attr(el, 'aria-labelledby');
    if (ids) {
      const root = el.getRootNode();
      name = ids.split(/\s+/).map((id) => byId(root, id)).filter(Boolean).map((l) => contentText(l, false)).join(' ');
    }
    name = normalize(name) || attr(el, 'aria-label');
    const tag = el.localName;
    const type = tag === 'input' ? inputType(el) : '';
    if (!name && tag === 'input' && type in BUTTON_TYPES) {
      name = el.hasAttribute('value') ? el.getAttribute('value') : BUTTON_TYPES[type];
    }
    if (!name && tag === 'input' && type === 'image') name = attr(el, 'alt');
    if (!name && ['input', 'select', 'textarea'].includes(tag)) {
      name = labelsOf(el).map((l) => contentText(l, true)).join(' ');
    }
    if (!name && NAMED_BY_CONTENT.has(role) && !isControl(el)) name = contentText(el, false);
    if (!name && tag === 'button') name = contentText(el, false);
    name = normalize(name || '');
    if (!name) name = attr(el, 'placeholder') || attr(el, 'title');
    return clip(normalize(name), NAME_LIMIT);
  }

  function optionLabel(option) {
    return normalize(option.getAttribute('label') || option.textContent || '');
  }

  function isDisabled(el) {
    if (attr(el, 'aria-disabled') === 'true') return true;
    try {
      return el.matches(':disabled');
    } catch (e) {
      return !!el.disabled;
    }
  }

  /** The states and value printed after an element's name. */
  function details(el, role) {
    const parts = [];
    const tag = el.localName;
    const type = tag === 'input' ? inputType(el) : '';
    if (tag === 'input' && role === 'textbox' && type !== 'text') parts.push('type=' + type);
    if (tag === 'select') {
      const options = Array.from(el.querySelectorAll('option'));
      const selected = options.filter((o) => o.selected).map(optionLabel);
      if (selected.length) parts.push('value=' + quote(clip(selected.join(', '), VALUE_LIMIT)));
      const listed = options.slice(0, OPTION_LIMIT).map((o) => quote(optionLabel(o)));
      if (options.length > OPTION_LIMIT) listed.push('+' + (options.length - OPTION_LIMIT) + ' more');
      parts.push('options=[' + listed.join(', ') + ']');
    } else if (tag === 'textarea' || (tag === 'input' && (KEY_TYPES.has(type) || VALUE_TYPES.has(type)))) {
      const value = el.value || '';
      if (value) parts.push('value=' + (type === 'password' ? '"***"' : quote(clip(value, VALUE_LIMIT))));
    } else if (role === 'textbox' && isEditableContent(el)) {
      const value = normalize(contentText(el, false));
      if (value) parts.push('value=' + quote(clip(value, VALUE_LIMIT)));
    }
    if (tag === 'input' && (type === 'checkbox' || type === 'radio') ? el.checked : attr(el, 'aria-checked') === 'true')
      parts.push('checked');
    if (attr(el, 'aria-pressed') === 'true') parts.push('pressed');
    if (attr(el, 'aria-selected') === 'true') parts.push('selected');
    const summary = tag === 'summary' && el.parentElement && el.parentElement.localName === 'details';
    const expanded = summary ? String(el.parentElement.open) : attr(el, 'aria-expanded');
    if (expanded === 'true') parts.push('expanded');
    if (expanded === 'false') parts.push('collapsed');
    if (isDisabled(el)) parts.push('disabled');
    if (el.readOnly && (tag === 'input' || tag === 'textarea')) parts.push('readonly');
    return parts;
  }

  // --- the walk ---

  /** Every visible line of the page in order: text lines, and elements the agent can act on. */
  function collect() {
    const items = [];
    let buffer = '';
    let heading = '';
    let indent = '';
    let muted = 0;
    let frames = 0;

    const flush = () => {
      const text = normalize(buffer);
      buffer = '';
      if (text) items.push({line: indent + heading + text});
    };

    const walk = (node, frame) => {
      for (const child of childrenOf(node)) visit(child, frame);
    };

    const visit = (node, frame) => {
      if (node.nodeType === 3) {
        const parent = node.parentElement || (node.parentNode && node.parentNode.host);
        if (!muted && parent && styleOf(parent).visibility === 'visible') buffer += node.data;
        return;
      }
      if (node.nodeType !== 1) return;
      const el = node;
      const tag = el.localName;
      if (SKIP.has(tag)) return;
      const style = styleOf(el);
      if (style.display === 'none') return;
      if (tag === 'br') return flush();
      if (tag === 'iframe' || tag === 'frame') {
        flush();
        return visitFrame(el, frame + '/' + ++frames);
      }
      const role = roleOf(el);
      if (role) {
        if (style.visibility !== 'visible') return;
        flush();
        const name = nameOf(el, role);
        const key = [role, name, el.id || attr(el, 'name'), frame].join('\u0000');
        items.push({el, role, name, key, indent, details: details(el, role)});
        if (isControl(el)) return;
        // A link or a custom widget can hold other things to act on. Its own text is already its name.
        const outer = indent;
        indent += '  ';
        muted++;
        walk(el, frame);
        muted--;
        flush();
        indent = outer;
        return;
      }
      const level = /^h([1-6])$/.exec(tag);
      const display = style.display;
      const block = !!level || !(display.startsWith('inline') || display === 'contents' || display === 'table-cell');
      const padded = !block && display !== 'inline' && display !== 'contents';
      if (block) flush();
      else if (padded) buffer += ' ';
      const outer = heading;
      if (level) heading = '#'.repeat(Number(level[1])) + ' ';
      const control = tag === 'label' ? labelControl(el) : null;
      const mute = !!control && roleOf(control) !== null && shown(control);
      if (mute) muted++;
      walk(el, frame);
      if (mute) muted--;
      if (block) flush();
      else if (padded) buffer += ' ';
      heading = outer;
    };

    const visitFrame = (el, frame) => {
      const name = normalize(attr(el, 'title') || attr(el, 'aria-label') || attr(el, 'name'));
      const head = indent + 'iframe' + (name ? ' ' + quote(clip(name, NAME_LIMIT)) : '');
      let doc = null;
      try {
        doc = el.contentDocument;
      } catch (e) {
        doc = null;
      }
      const root = doc && (doc.body || doc.documentElement);
      if (!root) return items.push({line: head + ' (other origin, not shown)'});
      items.push({line: head});
      const outer = indent;
      const outerHeading = heading;
      indent += '  ';
      heading = '';
      walk(root, frame);
      flush();
      indent = outer;
      heading = outerHeading;
    };

    walk(document.body || document.documentElement, '');
    flush();
    return items;
  }

  // --- refs ---

  function refState(create) {
    let state = window[STATE];
    if (!state && create) {
      state = {
        page: Math.random().toString(36).slice(2) + Date.now().toString(36),
        next: 1,
        entries: new Map(),
        refOf: new WeakMap(),
      };
      Object.defineProperty(window, STATE, {value: state});
    }
    return state;
  }

  function alive(el) {
    return !!el && el.isConnected && !!el.ownerDocument.defaultView;
  }

  function holdsRef(state, el) {
    const ref = state.refOf.get(el);
    const entry = ref === undefined ? undefined : state.entries.get(ref);
    return entry && entry.el === el ? ref : undefined;
  }

  function bind(state, ref, el, key) {
    state.entries.set(ref, {el, key});
    state.refOf.set(el, ref);
    if (el.getAttribute(ATTR) !== String(ref)) el.setAttribute(ATTR, String(ref));
  }

  function count(values) {
    const counts = new Map();
    for (const value of values) counts.set(value, (counts.get(value) || 0) + 1);
    return counts;
  }

  /** Give every element a ref. An element keeps its ref while it lives. A new element takes over the ref of a removed
   * one only when it is the one new element, and that the one removed element, with the same role, name, id and
   * frame: a re-render that replaced it. Anything else gets a new number. */
  function assignRefs(state, items) {
    const fresh = [];
    for (const item of items) {
      if (!item.el) continue;
      const ref = holdsRef(state, item.el);
      if (ref === undefined) fresh.push(item);
      else bind(state, (item.ref = ref), item.el, item.key);
    }
    const gone = [];
    for (const [ref, entry] of state.entries) {
      if (alive(entry.el)) continue;
      gone.push([ref, entry.key]);
      entry.el = null; // let the removed element be collected; the ref now reports that it is gone
    }
    const goneKeys = count(gone.map(([, key]) => key));
    const freshKeys = count(fresh.map((item) => item.key));
    for (const item of fresh) {
      const replaced = goneKeys.get(item.key) === 1 && freshKeys.get(item.key) === 1;
      item.ref = replaced ? gone.find(([, key]) => key === item.key)[0] : state.next++;
      bind(state, item.ref, item.el, item.key);
    }
  }

  function render(items, budget) {
    const lines = items.map((item) => {
      if (!item.el) return item.line;
      const name = item.name ? ' ' + quote(item.name) : '';
      const extra = item.details.length ? ' ' + item.details.join(' ') : '';
      return item.indent + '[' + item.ref + '] ' + item.role + name + extra;
    });
    const text = lines.join('\n');
    if (text.length <= budget) return text;
    let size = 0;
    let kept = 0;
    const reserve = 80;
    while (kept < lines.length && size + lines[kept].length + 1 <= budget - reserve) size += lines[kept++].length + 1;
    const refs = items.slice(kept).filter((item) => item.el).length;
    const prefix = lines.slice(0, kept);
    // Preserve useful evidence when the first prose line alone exceeds the budget. Never expose a partial
    // control line: its ref/name/states must stay together. Reserve room for the notice, as above.
    const partial = kept === 0 && lines.length > 0 && !items[0].el;
    const note = '[cut at ' + budget + ' characters: ' + (partial ? 'first line truncated; ' : '') +
      (lines.length - kept - (partial ? 1 : 0)) + ' more lines, ' + refs + ' more refs]';
    if (partial) prefix.push(lines[0].slice(0, Math.max(0, budget - Math.max(reserve, note.length + 1))));
    return prefix.concat([note]).join('\n');
  }

  function snapshot(budget) {
    const state = refState(true);
    const items = collect();
    assignRefs(state, items);
    return {page: state.page, url: location.href, title: document.title, text: render(items, budget)};
  }

  // --- acting on a ref ---

  const fail = (error, detail) => ({error, detail: detail || ''});

  function describe(el) {
    const id = el.id ? '#' + el.id : '';
    const cls = typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/)[0] : '';
    return '<' + el.localName + id + cls + '>';
  }

  /** A removed element's ref moves to its replacement: the one element still stamped with the ref (a framework that
   * copied the element), or else the same rule as `assignRefs`. */
  function readopt(state, ref, entry) {
    const items = collect().filter((item) => item.el && holdsRef(state, item.el) === undefined);
    const stamped = items.filter((item) => item.el.getAttribute(ATTR) === String(ref) && item.key === entry.key);
    const gone = Array.from(state.entries.values()).filter((e) => e.key === entry.key && !alive(e.el));
    const same = items.filter((item) => item.key === entry.key);
    const found = stamped.length === 1 ? stamped[0] : gone.length === 1 && same.length === 1 ? same[0] : null;
    if (!found) return null;
    bind(state, ref, found.el, entry.key);
    return found.el;
  }

  function frameOffset(frameEl) {
    const rect = frameEl.getBoundingClientRect();
    const style = styleOf(frameEl);
    return [
      rect.left + frameEl.clientLeft + parseFloat(style.paddingLeft),
      rect.top + frameEl.clientTop + parseFloat(style.paddingTop),
    ];
  }

  /** The element at a top-level viewport point, looking inside open shadow roots and same-origin frames. */
  function deepHit(x, y) {
    let hit = document.elementFromPoint(x, y);
    for (let depth = 0; hit && depth < 64; depth++) {
      if (hit.shadowRoot && hit.shadowRoot.elementFromPoint) {
        const inner = hit.shadowRoot.elementFromPoint(x, y);
        if (inner && inner !== hit) {
          hit = inner;
          continue;
        }
      }
      if (hit.localName === 'iframe' || hit.localName === 'frame') {
        let doc = null;
        try {
          doc = hit.contentDocument;
        } catch (e) {
          doc = null;
        }
        if (doc) {
          const [dx, dy] = frameOffset(hit);
          x -= dx;
          y -= dy;
          const inner = doc.elementFromPoint(x, y);
          if (inner) {
            hit = inner;
            continue;
          }
        }
      }
      break;
    }
    return hit;
  }

  function composedContains(el, node) {
    for (let n = node; n; n = n.parentNode || n.host) if (n === el) return true;
    return false;
  }

  function clickPoint(el) {
    el.scrollIntoView({block: 'center', inline: 'center'});
    for (let win = el.ownerDocument.defaultView; win.frameElement; win = win.parent)
      win.frameElement.scrollIntoView({block: 'nearest', inline: 'nearest'});
    const rect = Array.from(el.getClientRects()).find((r) => r.width > 0 && r.height > 0) || el.getBoundingClientRect();
    let x = rect.left + rect.width / 2;
    let y = rect.top + rect.height / 2;
    for (let win = el.ownerDocument.defaultView; win.frameElement; win = win.parent) {
      const [dx, dy] = frameOffset(win.frameElement);
      x += dx;
      y += dy;
    }
    if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return fail('offscreen');
    const hit = deepHit(x, y);
    const label = hit && hit.closest && hit.closest('label');
    if (!hit || !(composedContains(el, hit) || (label && labelControl(label) === el))) {
      return fail('covered', hit ? describe(hit) : 'nothing');
    }
    return {x, y};
  }

  function setValue(el, value) {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value').set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', {bubbles: true}));
  }

  function prepareTyping(el, text) {
    const tag = el.localName;
    if (tag === 'select') {
      const options = Array.from(el.querySelectorAll('option'));
      const wanted = normalize(text);
      const match = options.find((o) => optionLabel(o) === wanted) || options.find((o) => o.value === text);
      if (!match) return fail('no-option', options.slice(0, OPTION_LIMIT).map((o) => quote(optionLabel(o))).join(', '));
      el.focus();
      if (!match.selected) {
        match.selected = true;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      }
      return {keys: false};
    }
    const type = tag === 'input' ? inputType(el) : '';
    if (VALUE_TYPES.has(type)) {
      if (el.readOnly) return fail('readonly');
      el.focus();
      setValue(el, text);
      el.dispatchEvent(new Event('change', {bubbles: true}));
      return {keys: false};
    }
    const field = tag === 'textarea' || KEY_TYPES.has(type);
    if (!field && !isEditableContent(el)) return fail('not-editable', roleOf(el) || tag);
    if (field && el.readOnly) return fail('readonly');
    el.focus();
    const root = el.getRootNode();
    if ((root.activeElement || el.ownerDocument.activeElement) !== el) return fail('not-focusable');
    if (field) {
      if (text === '') {
        setValue(el, '');
        return {keys: false};
      }
      el.select();
      return {keys: true};
    }
    const doc = el.ownerDocument;
    if (text === '') {
      el.textContent = '';
      el.dispatchEvent(new Event('input', {bubbles: true}));
      return {keys: false};
    }
    const range = doc.createRange();
    range.selectNodeContents(el);
    const selection = doc.defaultView.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return {keys: true};
  }

  function locate(ref, page, action, text) {
    const state = refState(false);
    if (!page) return fail('no-snapshot');
    if (!state || state.page !== page) return fail('page-changed');
    const entry = /^[0-9]+$/.test(ref) ? state.entries.get(Number(ref)) : undefined;
    if (!entry) return fail('unknown');
    const el = alive(entry.el) ? entry.el : readopt(state, Number(ref), entry);
    if (!el) return fail('stale');
    const style = styleOf(el);
    if (style.visibility !== 'visible' || !el.getClientRects().length) return fail('hidden');
    if (isDisabled(el)) return fail('disabled');
    return action === 'type' ? prepareTyping(el, text) : clickPoint(el);
  }

  if (request.op === 'snapshot') return snapshot(request.budget);
  if (request.op === 'locate') return locate(String(request.ref), request.page, request.action, request.text || '');
  throw new Error('snapshot.js: unknown op ' + request.op);
})
