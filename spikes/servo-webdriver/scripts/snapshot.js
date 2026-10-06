// Agent page snapshot built from the DOM, the way browser-use-style agents do it.
// Run through WebDriver Execute Script; returns {title, url, lines, count}. Each interactive element gets a stable
// `data-mb-ref` attribute so the agent can act on it with `[data-mb-ref="eN"]` (WebDriver Find Element).
// Walks open shadow roots and same-origin iframes are not entered (kept simple for the spike).
const MAX = arguments[0] || 400;
const IMPLICIT = {
  A: el => (el.hasAttribute('href') ? 'link' : null),
  BUTTON: () => 'button',
  SELECT: el => (el.multiple || el.size > 1 ? 'listbox' : 'combobox'),
  TEXTAREA: () => 'textbox',
  SUMMARY: () => 'button',
  OPTION: () => 'option',
  H1: () => 'heading', H2: () => 'heading', H3: () => 'heading', H4: () => 'heading', H5: () => 'heading', H6: () => 'heading',
  IMG: el => (el.getAttribute('alt') === '' ? null : 'img'),
  INPUT: el => {
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    return ({
      button: 'button', submit: 'button', reset: 'button', image: 'button', checkbox: 'checkbox', radio: 'radio',
      range: 'slider', number: 'spinbutton', search: el.hasAttribute('list') ? 'combobox' : 'searchbox',
      email: 'textbox', tel: 'textbox', text: 'textbox', url: 'textbox', password: 'textbox', hidden: null,
      file: 'button', color: 'button', date: 'textbox', 'datetime-local': 'textbox', month: 'textbox', time: 'textbox', week: 'textbox',
    })[t] ?? 'textbox';
  },
};
const INTERACTIVE_ROLES = new Set(['button', 'link', 'textbox', 'searchbox', 'combobox', 'listbox', 'checkbox', 'radio',
  'slider', 'spinbutton', 'switch', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'treeitem']);
const clean = s => (s || '').replace(/\s+/g, ' ').trim();

function role(el) {
  const r = el.getAttribute('role');
  if (r) return r.split(/\s+/)[0];
  const f = IMPLICIT[el.tagName];
  return f ? f(el) : null;
}
function textOf(el) {
  // innerText respects CSS visibility; fall back to textContent
  return clean(el.innerText !== undefined ? el.innerText : el.textContent);
}
function byIds(ids) {
  return clean(ids.split(/\s+/).map(id => { const n = document.getElementById(id); return n ? textOf(n) : ''; }).join(' '));
}
function name(el, r) {
  const lb = el.getAttribute('aria-labelledby');
  if (lb) { const t = byIds(lb); if (t) return t; }
  const al = clean(el.getAttribute('aria-label'));
  if (al) return al;
  if (el.labels && el.labels.length) {
    const t = clean(Array.from(el.labels).map(textOf).join(' '));
    if (t) return t;
  }
  if (el.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes((el.type || '').toLowerCase()))
    return clean(el.value) || (el.type === 'submit' ? 'Submit' : el.type === 'reset' ? 'Reset' : '');
  if (el.tagName === 'IMG' || (el.tagName === 'INPUT' && el.type === 'image')) { const a = clean(el.getAttribute('alt')); if (a) return a; }
  if (['button', 'link', 'tab', 'menuitem', 'option', 'heading', 'checkbox', 'radio', 'switch', 'treeitem'].includes(r)) {
    let t = textOf(el);
    if (!t) { const img = el.querySelector('img[alt],svg[aria-label],[aria-label]'); if (img) t = clean(img.getAttribute('alt') || img.getAttribute('aria-label')); }
    if (t) return t.slice(0, 120);
  }
  return clean(el.getAttribute('placeholder')) || clean(el.getAttribute('title')) || '';
}
function visible(el) {
  const rects = el.getClientRects();
  if (!rects.length) return false;
  const st = getComputedStyle(el);
  if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) === 0) return false;
  const b = el.getBoundingClientRect();
  return b.width > 0 && b.height > 0;
}
function interactive(el, r) {
  if (r && INTERACTIVE_ROLES.has(r)) return true;
  if (el.isContentEditable) return true;
  if (el.hasAttribute('onclick')) return true;
  const ti = el.getAttribute('tabindex');
  return ti !== null && Number(ti) >= 0;
}

let n = 0;
const lines = [];
const seen = new Set();
function walk(root, depth) {
  const all = root.querySelectorAll('*');
  for (const el of all) {
    if (lines.length >= MAX) return;
    if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
    const r = role(el);
    const isHeading = r === 'heading';
    if (!(isHeading || interactive(el, r)) || seen.has(el) || !visible(el)) continue;
    seen.add(el);
    let ref = el.getAttribute('data-mb-ref');
    if (!ref) { ref = 'e' + (++n + Number(document.documentElement.getAttribute('data-mb-n') || 0)); el.setAttribute('data-mb-ref', ref); }
    const nm = name(el, r);
    let line = `${r || el.tagName.toLowerCase()}${nm ? ` "${nm.replace(/"/g, "'")}"` : ''} [ref=${ref}]`;
    if (isHeading) line = `heading "${textOf(el).slice(0, 120)}" [level=${el.tagName[1] || el.getAttribute('aria-level') || '?'}]`;
    if ('value' in el && ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) && !['button', 'submit', 'reset', 'checkbox', 'radio'].includes(el.type)) {
      const v = el.type === 'password' ? '*'.repeat(el.value.length) : el.value;
      if (v) line += ` value="${v.slice(0, 80)}"`;
    }
    if (el.type === 'checkbox' || el.type === 'radio' || el.getAttribute('aria-checked')) line += ` checked=${el.checked ?? el.getAttribute('aria-checked')}`;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') line += ' disabled';
    if (el.required) line += ' required';
    if (el.getAttribute('aria-expanded')) line += ` expanded=${el.getAttribute('aria-expanded')}`;
    if (el.tagName === 'A' && el.href) line += ` href=${el.getAttribute('href').slice(0, 80)}`;
    lines.push(line);
  }
}
const t0 = performance.now();
walk(document, 0);
document.documentElement.setAttribute('data-mb-n', String(Number(document.documentElement.getAttribute('data-mb-n') || 0) + n));
return { title: document.title, url: location.href, count: lines.length, ms: Math.round(performance.now() - t0), lines };
