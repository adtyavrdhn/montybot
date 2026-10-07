// The bot's replies as formatted text: a small part of Markdown, built as DOM nodes and never as HTML, so a reply
// cannot inject markup. Blocks: paragraphs, headings, bullet and numbered lists, tables and ``` code blocks.
// Inline: **bold**, *italic*, `code`, [text](https://...) links and bare https:// links. Anything else stays text.
'use strict';

const LIST_ITEM = /^\s*(?:[-*+]|\d+[.)])\s+(.*)$/;
const NUMBERED_ITEM = /^\s*\d+[.)]\s/;
const HEADING = /^(#{1,6})\s+(.*)$/;
// An address may hold one level of parentheses, as Wikipedia's do: /wiki/Python_(programming_language).
const ADDRESS = String.raw`https?:\/\/(?:[^\s()<>]|\([^\s()<>]*\))+`;
const INLINE = new RegExp(String.raw`\*\*([^*]+)\*\*|\x60([^\x60]+)\x60|\[([^\][]+)\]\((${ADDRESS})\)|(${ADDRESS})|\*([^*\s][^*]*)\*`);

function renderMarkdown(text) {
  const blocks = document.createDocumentFragment();
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
    } else if (line.trimStart().startsWith('```')) {
      const fence = line.trim().match(/^`+/)[0];
      const closes = (next) => /^`+$/.test(next.trim()) && next.trim().length >= fence.length;
      const code = [];
      for (i++; i < lines.length && !closes(lines[i]); i++) code.push(lines[i]);
      i++;  // past the closing fence
      blocks.append(node('pre', [node('code', [code.join('\n')])]));
    } else if (HEADING.test(line)) {
      const [, hashes, title] = line.match(HEADING);
      blocks.append(node(`h${Math.min(hashes.length + 2, 6)}`, inline(title)));
      i++;
    } else if (line.includes('|') && isDivider(lines[i + 1])) {
      const rows = [cells(line)];
      for (i += 2; i < lines.length && lines[i].includes('|'); i++) rows.push(cells(lines[i]));
      blocks.append(table(rows));
    } else if (LIST_ITEM.test(line)) {
      const list = node(NUMBERED_ITEM.test(line) ? 'ol' : 'ul');
      if (list.tagName === 'OL') list.start = parseInt(line, 10);
      for (; i < lines.length && LIST_ITEM.test(lines[i]); i++) list.append(node('li', inline(lines[i].match(LIST_ITEM)[1])));
      blocks.append(list);
    } else {
      const paragraph = node('p');
      for (; i < lines.length && lines[i].trim() && !startsBlock(lines, i); i++) {
        if (paragraph.childNodes.length) paragraph.append(node('br'));
        paragraph.append(...inline(lines[i]));
      }
      blocks.append(paragraph);
    }
  }
  return blocks;
}

function startsBlock(lines, i) {
  const line = lines[i];
  return line.trimStart().startsWith('```') || HEADING.test(line) || LIST_ITEM.test(line) ||
    (line.includes('|') && isDivider(lines[i + 1]));
}

function isDivider(line) {
  // The line under a table's header: | --- | :---: |. Checked cell by cell, as one regex would backtrack badly.
  return line !== undefined && line.includes('-') && cells(line).every((cell) => /^:?-{3,}:?$/.test(cell));
}

function cells(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
}

function table([header, ...rows]) {
  const head = node('thead', [node('tr', header.map((cell) => node('th', inline(cell))))]);
  const body = node('tbody', rows.map((row) => node('tr', row.map((cell) => node('td', inline(cell))))));
  return node('div', [node('table', [head, body])], 'table-scroll');
}

function inline(text) {
  // The text as nodes, formatting the first match of INLINE and then the rest of the text after it.
  const nodes = [];
  let rest = text;
  for (let match = rest.match(INLINE); match; match = rest.match(INLINE)) {
    if (match.index > 0) nodes.push(rest.slice(0, match.index));
    let used = match[0];
    const [, bold, code, label, href, url, italic] = match;
    if (bold !== undefined) nodes.push(node('strong', inline(bold)));
    else if (code !== undefined) nodes.push(node('code', [code]));
    else if (label !== undefined) nodes.push(link(href, label));
    else if (url !== undefined) {
      used = url.replace(/[.,;:!?]+$/, '');  // the sentence's punctuation is not part of the address
      nodes.push(link(used, used));
    } else nodes.push(node('em', inline(italic)));
    rest = rest.slice(match.index + used.length);
  }
  if (rest) nodes.push(rest);
  return nodes;
}

function link(href, label) {
  const a = node('a', [label]);
  a.href = href;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  return a;
}

function node(tag, children = [], className = '') {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.append(...children);
  return element;
}
