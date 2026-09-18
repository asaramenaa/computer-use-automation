"""In-page helpers: role/name/anchor computation, control enumeration, anchor matching, table extraction.

Roles and names follow the same rules Playwright's get_by_role uses for the
controls we care about, so the ROLE_NAME and ANCHOR strategies agree on what a
control *is*. Kept small on purpose: legacy markup has no ARIA to speak of.
"""

CUC_JS = r"""
(() => {
  if (window.__cuc) return true;
  const INTERACTIVE = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],[role=textbox],[role=combobox],[onclick]';
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'button') return 'button';
    if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (['submit', 'button', 'reset', 'image'].includes(t)) return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'range') return 'slider';
      if (t === 'number') return 'spinbutton';
      return 'textbox';
    }
    if (tag === 'td') return 'cell';
    if (tag === 'th') return 'columnheader';
    if (el.hasAttribute('onclick')) return 'button';
    return 'generic';
  };
  const nameOf = (el) => {
    const al = el.getAttribute('aria-label'); if (al) return norm(al);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(id => { const n = document.getElementById(id); return n ? n.innerText : ''; }).join(' '); if (norm(t)) return norm(t); }
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return norm(l.innerText); }
    const wrap = el.closest('label'); if (wrap) return norm(wrap.innerText);
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (['submit', 'button', 'reset'].includes(t)) return norm(el.value || (t === 'submit' ? 'Submit' : ''));
      if (t === 'image') return norm(el.getAttribute('alt'));
      return norm(el.getAttribute('title') || el.getAttribute('placeholder') || '');
    }
    if (tag === 'select' || tag === 'textarea') return norm(el.getAttribute('title') || '');
    return norm(el.innerText || el.getAttribute('title') || '');
  };
  // Nearest preceding visible text in document order, bounded to the same table/form/body.
  const anchorOf = (el) => {
    const scope = el.closest('table, form, body') || document.body;
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    let last = '';
    let node;
    while ((node = walker.nextNode())) {
      const pos = el.compareDocumentPosition(node);
      if (pos & Node.DOCUMENT_POSITION_FOLLOWING) break;
      if (el.contains(node)) continue;
      const t = norm(node.textContent);
      if (!t) continue;
      const p = node.parentElement;
      if (!p || ['SCRIPT', 'STYLE', 'OPTION'].includes(p.tagName) || !visible(p)) continue;
      last = t;
    }
    return last.length > 60 ? last.slice(0, 60) : last;
  };
  const valueOf = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (['submit', 'button', 'reset', 'image'].includes(t)) return '';
      if (t === 'password') return el.value ? '********' : '';
      if (t === 'checkbox' || t === 'radio') return el.checked ? 'checked' : '';
      return norm(el.value);
    }
    if (tag === 'select') return norm(el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : '');
    if (tag === 'textarea') return norm(el.value);
    return '';
  };
  const controls = () => {
    const out = [];
    for (const el of document.querySelectorAll(INTERACTIVE)) {
      if (!visible(el)) continue;
      const r = el.getBoundingClientRect();
      const name = nameOf(el);
      out.push({ role: roleOf(el), name, anchor: name ? '' : anchorOf(el), value: valueOf(el),
                 enabled: !el.disabled, bbox: [r.x, r.y, r.width, r.height] });
    }
    return out;
  };
  // Mark controls of `role` whose anchor equals `anchor` (case-insensitive); return count.
  const markByAnchor = (role, anchor, token) => {
    const want = norm(anchor).toLowerCase();
    let n = 0;
    for (const el of document.querySelectorAll(INTERACTIVE)) {
      if (!visible(el) || roleOf(el) !== role) continue;
      if (anchorOf(el).toLowerCase() === want) { el.setAttribute('data-cuc-match', token); n++; }
    }
    return n;
  };
  const unmark = () => { for (const el of document.querySelectorAll('[data-cuc-match]')) el.removeAttribute('data-cuc-match'); };
  const cellText = (td) => norm(td.innerText);
  const tableCell = (rowAnchor, colHeader) => {
    const hits = [];
    const wantRow = norm(rowAnchor).toLowerCase(), wantCol = norm(colHeader).toLowerCase();
    for (const table of document.querySelectorAll('table')) {
      const rows = Array.from(table.rows).filter(r => r.closest('table') === table);
      let col = -1;
      for (const r of rows) {
        const cells = Array.from(r.cells);
        const idx = cells.findIndex(c => cellText(c).toLowerCase() === wantCol);
        if (idx >= 0) { col = idx; continue; }
        if (col < 0) continue;
        if (cells.some(c => cellText(c).toLowerCase().includes(wantRow)) && cells[col]) hits.push(cellText(cells[col]));
      }
    }
    return hits;
  };
  const labelValue = (label) => {
    const want = norm(label).toLowerCase();
    const hits = [];
    for (const c of document.querySelectorAll('td, th')) {
      if (cellText(c).toLowerCase() !== want) continue;
      let next = c.nextElementSibling;
      while (next && !['TD', 'TH'].includes(next.tagName)) next = next.nextElementSibling;
      if (next) hits.push(cellText(next));
    }
    return hits;
  };
  window.__cuc = { controls, markByAnchor, unmark, tableCell, labelValue, text: () => document.body ? document.body.innerText : '' };
  return true;
})()
"""
