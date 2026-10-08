// The page as a screen reader reads it, for the user driving the live view with VoiceOver: what is on screen in
// reading order (headings, text, links, buttons and fields) with each item's role, name, value, state and box in the
// viewport's CSS pixels. One function expression, run in the active tab's top-level document; it returns JSON.
//
// It is for the user's own screen, never the agent's: passwords are never read, only how many characters they have.
(function () {
  const LIMIT = 400;
  const CONTROL_ROLES = new Set([
    'button', 'link', 'checkbox', 'radio', 'switch', 'textbox', 'searchbox', 'combobox', 'listbox', 'option', 'tab',
    'menuitem', 'slider', 'spinbutton',
  ]);
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK', 'SVG']);
  const items = [];

  function visible(el) {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0 && box.bottom > 0 && box.right > 0 &&
      box.top < innerHeight && box.left < innerWidth;
  }

  function roleOf(el) {
    const explicit = (el.getAttribute('role') || '').split(/\s+/)[0].toLowerCase();
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === 'A' && el.hasAttribute('href')) return 'link';
    if (tag === 'BUTTON' || tag === 'SUMMARY') return 'button';
    if (tag === 'SELECT') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'INPUT') {
      const type = (el.type || 'text').toLowerCase();
      if (type === 'hidden') return null;
      if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'range') return 'slider';
      if (type === 'number') return 'spinbutton';
      if (type === 'search') return 'searchbox';
      return 'textbox';
    }
    if (el.isContentEditable && el.parentElement && !el.parentElement.isContentEditable) return 'textbox';
    if (/^H[1-6]$/.test(tag)) return 'heading';
    if (tag === 'IMG' && (el.getAttribute('alt') || '').trim()) return 'image';
    return null;
  }

  function text(node) {
    return (node.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function nameOf(el, role) {
    const label = el.getAttribute('aria-label');
    if (label && label.trim()) return label.trim();
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const named = by.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean).map(text).join(' ');
      if (named) return named;
    }
    if (el.labels && el.labels.length) return Array.from(el.labels).map(text).join(' ');
    if (role === 'image') return el.getAttribute('alt').trim();
    if (el.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes((el.type || '').toLowerCase())) {
      return el.value || (el.type === 'submit' ? 'Submit' : '');
    }
    if (['textbox', 'searchbox', 'combobox', 'spinbutton', 'slider'].includes(role)) {
      return (el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '').trim();
    }
    return text(el) || (el.getAttribute('title') || '').trim();
  }

  function valueOf(el, role) {
    if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'password') {
      return el.value ? `${el.value.length} characters` : '';
    }
    if (el.tagName === 'SELECT') return Array.from(el.selectedOptions).map((o) => o.label || o.text).join(', ');
    if (['textbox', 'searchbox', 'combobox', 'spinbutton', 'slider'].includes(role)) {
      return el.isContentEditable ? text(el) : (el.value || '');
    }
    return '';
  }

  function plainText(el) {
    for (const child of el.querySelectorAll('*')) {
      if (!getComputedStyle(child).display.startsWith('inline') || roleOf(child)) return false;
    }
    return true;
  }

  function push(el, role, name, extra) {
    if (items.length >= LIMIT) return;
    const box = el.getBoundingClientRect();
    items.push(Object.assign({
      role, name: name.slice(0, 300),
      x: Math.round(box.left), y: Math.round(box.top), width: Math.round(box.width), height: Math.round(box.height),
    }, extra));
  }

  function walk(el) {
    if (items.length >= LIMIT || SKIP.has(el.tagName.toUpperCase()) || !visible(el)) return;
    const role = roleOf(el);
    if (role === 'heading') {
      push(el, 'heading', text(el), {level: Number(el.tagName[1]) || 2});
      return;
    }
    if (role === 'image') {
      push(el, 'image', nameOf(el, role), {});
      return;
    }
    if (role && CONTROL_ROLES.has(role)) {
      const extra = {
        value: valueOf(el, role),
        disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
        focused: document.activeElement === el,
      };
      if (role === 'checkbox' || role === 'radio' || role === 'switch') {
        extra.checked = el.checked === true || el.getAttribute('aria-checked') === 'true';
      }
      if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'password') extra.secure = true;
      push(el, role, nameOf(el, role), extra);
      if (role === 'link' || role === 'button') return;  // their text is their name
    }
    // A block of plain text (inline elements only, no controls) is read as one line, as a screen reader reads a
    // paragraph: "In cart: eggs", not "In cart:" then "eggs".
    if (!role && plainText(el)) {
      const line = text(el);
      if (line) push(el, 'text', line, {});
      return;
    }
    // Otherwise text directly in this element (not in its children) is read as its own line.
    let own = '';
    for (const child of el.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) own += child.textContent;
    }
    own = own.replace(/\s+/g, ' ').trim();
    if (own && !(role && CONTROL_ROLES.has(role)) && el.tagName !== 'LABEL' && el.tagName !== 'OPTION') {
      push(el, 'text', own, {});
    }
    for (const child of el.children) walk(child);
  }

  if (document.body) walk(document.body);
  return {title: document.title, items};
})
