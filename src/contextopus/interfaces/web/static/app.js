'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const state = {modules: [], module: '', views: [], mode: 'text', query: '', records: [], selected: null, total: null, hasMore: false};
  const requests = new Map();
  const metadataInputs = new Map();
  let debounce;
  let moduleVersion = 0;
  let listVersion = 0;
  let selectionVersion = 0;
  let detailVersion = 0;
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  };
  async function loadProviders() {
    const container = $('provider-settings');
    if (!container) return;
    try {
      const response = await fetch('/api/providers');
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Cannot load providers');
      container.replaceChildren();
      for (const [module, status] of Object.entries(data)) {
        for (const [source, config] of Object.entries(status.providers || {})) {
          const label = node('label', source + ' ');
          const input = node('input');
          input.type = 'checkbox'; input.checked = config.enabled;
          input.addEventListener('change', async () => {
            input.disabled = true;
            try {
              const result = await fetch('/api/providers', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({module, source, enabled: input.checked})});
              const body = await result.json();
              if (!result.ok) throw new Error(body.error || 'Cannot update provider');
              $('provider-status').textContent = source + (input.checked ? ' enabled' : ' disabled');
            } catch (error) { input.checked = !input.checked; $('provider-status').textContent = error.message; }
            finally { input.disabled = false; }
          });
          label.append(input); container.append(label, node('br'));
        }
      }
    } catch (error) { $('provider-status').textContent = error.message; }
  }
  loadProviders();
  async function managementRequest(path, data) {
    const response = await fetch(path, data ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)} : {});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Request failed');
    return result;
  }
  let managementVersion = 0;
  function sourceBadge(src) {
    if (src === 'context.configuration') return node('span', 'custom', 'src-badge src-custom');
    if (src === 'root.configuration') return node('span', 'global', 'src-badge src-root');
    return node('span', 'default', 'src-badge src-default');
  }
  function renderDiffBox(diffText) {
    const box = node('div', null, 'diff-box');
    const lines = String(diffText || '').split('\n');
    if (!diffText || !diffText.trim()) {
      box.append(node('div', '(пустой diff или бинарное изменение)', 'diff-meta'));
      return box;
    }
    for (const line of lines) {
      let cls = 'diff-line';
      if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ') || line.startsWith('index ')) cls += ' diff-meta';
      else if (line.startsWith('@@')) cls += ' diff-hunk';
      else if (line.startsWith('+')) cls += ' diff-add';
      else if (line.startsWith('-')) cls += ' diff-del';
      box.append(node('div', line || ' ', cls));
    }
    return box;
  }
  async function openSection(section) {
    const version = ++managementVersion;
    document.querySelectorAll('[data-section]').forEach(btn => btn.classList.toggle('active', btn.dataset.section === section));
    const management = $('management');
    document.querySelector('.catalog').hidden = section !== 'library';
    $('detail').hidden = section !== 'library';
    management.hidden = section === 'library';
    if (section === 'library') return;
    const head = node('div', null, 'mgmt-head');
    const titleBox = node('div');
    titleBox.append(
      node('h1', section === 'contexts' ? 'Contexts and rules' : 'Edit history'),
      node('p', section === 'contexts'
        ? 'Настройки применяются сразу на живой корень. Global задаёт базу, custom — переопределение конкретного контекста.'
        : 'История изменений файлов Core и настроек. Можно посмотреть diff или откатить изменение.', 'mgmt-sub')
    );
    head.append(titleBox);
    management.replaceChildren(head);
    try {
      if (section === 'contexts') {
        const data = await managementRequest('/api/contexts');
        if (version !== managementVersion) return;
        const fields = {documents_approval_required: 'Document approval', instructions_approval_required: 'Instruction approval', git_history_enabled: 'Git history'};
        const table = node('table', null, 'context-table');
        const header = node('tr');
        for (const text of ['Context', 'Work directories', ...Object.values(fields)]) header.append(node('th', text));
        table.append(header);
        for (const item of [{id: null, slug: '', title: 'Global settings (корень)', workdirs: [], settings: data.settings, sources: data.sources}, ...data.contexts]) {
          const row = node('tr', null, item.id === null ? 'global-row' : '');
          const nameCell = node('td');
          if (item.id === null) {
            nameCell.append(node('div', item.title, 'ctx-name'));
          } else {
            const primary = item.slug || item.title || item.id;
            nameCell.append(node('div', primary, 'ctx-name'));
            if (item.slug && item.title && item.title !== item.slug && item.title !== 'Project') {
              nameCell.append(node('div', item.title, 'ctx-sub'));
            }
          }
          const dirsCell = node('td');
          if ((item.workdirs || []).length) {
            for (const w of item.workdirs) dirsCell.append(node('span', w, 'dir-pill'));
          } else {
            dirsCell.append(node('span', '—', 'muted'));
          }
          row.append(nameCell, dirsCell);
          for (const field of Object.keys(fields)) {
            const label = node('label', null, 'setting-cell');
            const input = node('input'); input.type = 'checkbox'; input.checked = item.settings[field];
            input.addEventListener('change', async () => {
              input.disabled = true;
              try { await managementRequest('/api/context-settings', {context: item.id, field, value: input.checked}); if (version === managementVersion) await openSection('contexts'); }
              catch (error) { input.checked = !input.checked; alert(error.message); }
              finally { input.disabled = false; }
            });
            label.append(input, sourceBadge(item.sources[field] || 'default'));
            const cell = node('td'); cell.append(label); row.append(cell);
          }
          table.append(row);
        }
        management.append(table);
      } else {
        const entries = await managementRequest('/api/history');
        if (version !== managementVersion) return;
        if (!entries.length) management.append(node('p', 'История изменений пока пуста.', 'muted'));
        for (const entry of entries) {
          const card = node('section', null, 'management-card');
          if (entry.subject === 'Initial Contextopus history') {
            card.append(
              node('h2', 'Базовый снимок базы (точка отсчёта)'),
              node('p', `${entry.timestamp} · ${entry.commit.slice(0, 8)} · файлов в снимке: ${entry.files.length}`, 'commit-meta')
            );
            management.append(card);
            continue;
          }
          card.append(node('h2', entry.subject), node('p', entry.timestamp + ' · ' + entry.commit.slice(0, 8), 'commit-meta'));
          for (const file of entry.files) {
            const row = node('div', null, 'history-row');
            const actCls = 'act-badge act-' + (['A', 'M', 'D'].includes(file.action) ? file.action : 'M');
            const pathWrap = node('span', null, 'history-path');
            pathWrap.append(node('span', file.action, actCls), ' ' + file.path);
            row.append(pathWrap);
            let diffEl = null;
            const diff = node('button', 'View diff', 'btn-sm'); diff.type = 'button';
            diff.addEventListener('click', async () => {
              if (diffEl) { diffEl.remove(); diffEl = null; diff.textContent = 'View diff'; return; }
              diff.disabled = true;
              try {
                const result = await managementRequest('/api/history/diff?' + new URLSearchParams({commit: entry.commit, path: file.path}));
                diffEl = renderDiffBox(result.diff);
                if (typeof row.after === 'function') row.after(diffEl); else card.append(diffEl);
                diff.textContent = 'Hide diff';
              } catch (error) { alert(error.message); }
              finally { diff.disabled = false; }
            });
            const revert = node('button', 'Revert', 'btn-sm btn-danger'); revert.type = 'button';
            revert.addEventListener('click', async () => {
              if (typeof confirm === 'function' && !confirm(`Откатить ${file.path} к состоянию до коммита ${entry.commit.slice(0, 8)}?`)) return;
              revert.disabled = true;
              try { await managementRequest('/api/history/revert', {commit: entry.commit, path: file.path}); if (version === managementVersion) await openSection('history'); }
              catch (error) { alert(error.message); }
              finally { revert.disabled = false; }
            });
            row.append(diff, revert); card.append(row);
          }
          management.append(card);
        }
      }
    } catch (error) { if (version === managementVersion) management.append(node('p', error.message, 'error')); }
  }
  document.querySelector('[data-section="library"]')?.classList.add('active');
  document.querySelectorAll('[data-section]').forEach(button => button.addEventListener('click', () => openSection(button.dataset.section)));

  const keyString = key => JSON.stringify(key);
  const validKey = key => Array.isArray(key) && key.length === 3 && key.every(v => typeof v === 'string');
  const displayValue = value => typeof value === 'object' && value !== null ? JSON.stringify(value, null, 2) : String(value ?? '—');
  const cancelled = error => error.name === 'AbortError';

  function abort(slot) {
    requests.get(slot)?.abort();
    requests.delete(slot);
  }
  async function api(path, params, slot) {
    abort(slot);
    const controller = new AbortController();
    requests.set(slot, controller);
    try {
      const response = await fetch(path + (params ? '?' + new URLSearchParams(params) : ''), {signal: controller.signal, headers: {Accept: 'application/json'}});
      const value = await response.json().catch(() => {throw new Error('The server returned an unreadable response.');});
      if (!response.ok) throw new Error(typeof value.error === 'string' ? value.error : `Request failed (${response.status}).`);
      return value;
    } finally {
      if (requests.get(slot) === controller) requests.delete(slot);
    }
  }
  function message(container, text, isError = false) {
    container.replaceChildren(node('p', text, 'empty' + (isError ? ' error' : '')));
  }
  function baseDetail(label = 'Library') {
    const detail = $('detail');
    detail.replaceChildren();
    const top = node('div', undefined, 'top');
    top.append(node('span', label), node('span', 'Read-only', 'readonly'));
    detail.append(top);
    return detail;
  }
  function clearDetail(text = 'Choose a record from the catalog.') {
    detailVersion++;
    abort('detail');
    state.selected = null;
    baseDetail().append(node('h2', 'Browse your context'), node('p', text, 'muted'));
  }
  function setBusy(busy) {
    $('run-search').disabled = busy || !state.module || state.modules.find(item => item.id === state.module)?.available === false;
    $('run-search').textContent = busy ? 'Searching…' : state.mode === 'semantic' ? 'Search by meaning' : 'Search';
    $('load-more').disabled = busy;
    $('load-more').textContent = busy ? 'Loading…' : 'Load more';
    $('records').setAttribute('aria-busy', String(busy));
  }
  function renderNav() {
    $('nav').replaceChildren();
    for (const item of state.modules) {
      const button = node('button', undefined, item.id === state.module ? 'active' : '');
      button.type = 'button';
      button.setAttribute('aria-current', item.id === state.module ? 'page' : 'false');
      button.append(node('span', item.title || item.id));
      if (item.available === false) {
        button.classList.add('unavailable');
        button.append(node('small', 'Unavailable'));
      }
      button.addEventListener('click', () => selectModule(item.id));
      $('nav').append(button);
    }
  }
  function resetFilters() {
    state.views = [];
    renderViews();
    metadataInputs.clear();
    $('metadata-filters').replaceChildren();
    $('collection').replaceChildren(new Option('All collections', ''));
    $('type').replaceChildren(new Option('All types', ''));
  }
  function renderViews() {
    $('views').replaceChildren();
    $('views').hidden = !state.views.length;
    for (const view of state.views) {
      const active = $('type').value === view.type;
      const button = node('button', view.title, 'mode' + (active ? ' active' : ''));
      button.type = 'button';
      button.setAttribute('aria-pressed', String(active));
      button.addEventListener('click', () => {
        updateTypes([view.type]);
        $('type').value = view.type;
        renderViews();
        loadRecords();
      });
      $('views').append(button);
    }
  }
  async function loadModules() {
    const version = ++moduleVersion;
    listVersion++;
    selectionVersion++;
    abort('list');
    abort('collections');
    clearTimeout(debounce);
    clearDetail();
    $('refresh').disabled = true;
    $('nav-status').textContent = 'Loading modules…';
    setBusy(true);
    try {
      const result = await api('/api/modules', null, 'modules');
      if (version !== moduleVersion) return;
      state.modules = Array.isArray(result.modules) ? result.modules : [];
      $('nav-status').textContent = '';
      const selected = state.modules.find(item => item.id === state.module);
      renderNav();
      if (!state.modules.length) {
        state.module = '';
        $('heading').textContent = 'Library';
        $('hint').textContent = 'No connected modules.';
        $('count').textContent = 'No modules available.';
        message($('records'), 'There are no modules to browse.');
        resetFilters();
        $('load-more').hidden = true;
      } else {
        await selectModule(selected?.id || state.modules.find(item => item.available !== false)?.id || state.modules[0].id, Boolean(selected));
      }
    } catch (error) {
      if (!cancelled(error) && version === moduleVersion) {
        state.module = '';
        $('nav-status').textContent = 'Could not load modules: ' + error.message;
        $('count').textContent = 'Library unavailable. Use Refresh library to retry.';
        message($('records'), error.message, true);
      }
    } finally {
      if (version === moduleVersion) {
        $('refresh').disabled = false;
        setBusy(false);
      }
    }
  }
  async function selectModule(id, preserveFilters = false) {
    const item = state.modules.find(module => module.id === id);
    if (!item) return;
    clearTimeout(debounce);
    state.module = id;
    const previousCollection = preserveFilters ? $('collection').value : '';
    const previousType = preserveFilters ? $('type').value : '';
    listVersion++;
    const version = ++selectionVersion;
    abort('list');
    abort('collections');
    clearDetail();
    state.records = [];
    resetFilters();
    renderNav();
    $('heading').textContent = item.title || item.id;
    $('hint').textContent = item.description || 'Browse records from this module.';
    $('warnings').hidden = true;
    $('load-more').hidden = true;
    if (item.available === false) {
      $('count').textContent = 'Module unavailable';
      message($('records'), item.error || 'This module is currently unavailable.', true);
      setBusy(false);
      return;
    }
    $('count').textContent = 'Loading collections…';
    message($('records'), 'Loading records…');
    setBusy(true);
    try {
      const result = await api('/api/collections', {module: id}, 'collections');
      if (version !== selectionVersion || state.module !== id) return;
      if (result.error) throw new Error(displayValue(result.error));
      for (const filter of result.filters || []) {
        if (!/^[a-z][a-z0-9_]*$/.test(filter.name)) continue;
        const label = node('label');
        const input = node('input');
        input.type = 'text'; input.placeholder = 'Any';
        input.setAttribute('aria-label', filter.label || filter.name);
        input.addEventListener('change', () => loadRecords());
        metadataInputs.set(filter.name, input);
        label.append(node('span', filter.label || filter.name), input);
        $('metadata-filters').append(label);
      }
      for (const collection of result.collections || []) $('collection').add(new Option(collection.title || collection.id, collection.id));
      if ([...$('collection').options].some(option => option.value === previousCollection)) $('collection').value = previousCollection;
      state.views = (result.views || []).filter(view => typeof view.title === 'string' && typeof view.type === 'string');
      const selectedType = preserveFilters ? previousType : state.views[0]?.type || '';
      updateTypes([selectedType, ...state.views.map(view => view.type)]);
      $('type').value = selectedType;
      renderViews();
      await loadRecords();
    } catch (error) {
      if (!cancelled(error) && version === selectionVersion) {
        $('count').textContent = 'Could not load collections';
        message($('records'), error.message, true);
        setBusy(false);
      }
    }
  }
  function updateTypes(types) {
    const previous = $('type').value;
    const values = new Set([previous, ...(types || []).map(item => typeof item === 'string' ? item : item.id), ...state.records.map(record => record.type)].filter(Boolean));
    $('type').replaceChildren(new Option('All types', ''));
    [...values].sort().forEach(type => $('type').add(new Option(type, type)));
    $('type').value = previous;
  }
  function renderRecords() {
    $('records').replaceChildren();
    for (const record of state.records) {
      const active = keyString(record.key) === keyString(state.selected);
      const button = node('button', undefined, 'record' + (active ? ' active' : ''));
      button.type = 'button';
      button.setAttribute('aria-pressed', String(active));
      button.append(node('span', `${record.key?.[1] || state.module} / ${record.type || 'record'}`, 'eyebrow'), node('strong', record.title || record.key?.[2] || 'Untitled record'), node('p', record.description || 'No description supplied.'));
      const badges = [record.source, record.status, ...(Array.isArray(record.tags) ? record.tags : [])].filter(Boolean);
      if (badges.length) {
        const tags = node('div', undefined, 'tags');
        badges.forEach(tag => tags.append(node('span', displayValue(tag), 'badge')));
        button.append(tags);
      }
      button.disabled = !validKey(record.key);
      button.addEventListener('click', () => openRecord(record.key, true));
      $('records').append(button);
    }
    if (!state.records.length) message($('records'), state.query ? 'No matching records. Try another search or filter.' : 'No records in this collection.');
  }
  async function loadRecords(append = false) {
    const item = state.modules.find(module => module.id === state.module);
    if (!item || item.available === false) return;
    const version = ++listVersion;
    const offset = append ? state.records.length : 0;
    if (!append) {
      state.records = [];
      clearDetail();
      message($('records'), 'Loading records…');
    }
    setBusy(true);
    $('warnings').hidden = true;
    $('count').textContent = append ? 'Loading more records…' : state.mode === 'semantic' && state.query ? 'Searching by meaning…' : 'Loading records…';
    $('load-more').hidden = true;
    try {
      const filters = Object.fromEntries([...metadataInputs].map(([name, input]) => [name, input.value.trim()]).filter(([,value]) => value));
      const result = await api('/api/records', {...(Object.keys(filters).length ? {filters: JSON.stringify(filters)} : {}), module: state.module, collection: $('collection').value, query: state.query, mode: state.query ? state.mode : 'text', type: $('type').value, offset, limit: 40}, 'list');
      if (version !== listVersion) return;
      if (result.error) throw new Error(displayValue(result.error));
      state.records = append ? [...state.records, ...(result.records || [])] : result.records || [];
      state.total = result.total;
      state.hasMore = Boolean(result.has_more);
      updateTypes(result.types);
      renderRecords();
      const total = Number.isInteger(state.total) ? ` of ${state.total}` : '';
      $('count').textContent = `${state.records.length}${total} records` + (state.query ? ` · ${state.mode === 'semantic' ? 'semantic search' : 'text search'}` : '');
      const diagnostics = result.diagnostics || {};
      if (state.mode === 'semantic' && state.query) {
        const counts = diagnostics.counts || {};
        const considered = counts.considered_documents ?? counts.considered ?? diagnostics.considered_documents;
        const selected = counts.selected ?? counts.selected_documents ?? diagnostics.selected;
        const summary = [diagnostics.algorithm, Number.isFinite(considered) ? `${considered} considered` : '', Number.isFinite(selected) ? `${selected} selected` : ''].filter(Boolean);
        if (summary.length) $('count').textContent += ' · ' + summary.join(' · ');
      }
      const warnings = Array.isArray(result.warnings) ? result.warnings.map(displayValue) : [];
      const omitted = diagnostics.boundary?.result_cap?.omitted ?? diagnostics.completeness?.boundary?.result_cap?.omitted;
      if (Number.isFinite(omitted) && omitted > 0) warnings.push(`${omitted} records omitted by the configured result cap.`);
      else if (diagnostics.completeness?.result_window_complete === false || diagnostics.truncated) warnings.push('Search shows a limited result window; additional matches may be omitted.');
      $('warnings').textContent = warnings.join('\n');
      $('warnings').hidden = !warnings.length;
      $('load-more').hidden = !state.hasMore;
      if (!append && validKey(state.records[0]?.key)) openRecord(state.records[0].key);
    } catch (error) {
      if (!cancelled(error) && version === listVersion) {
        $('count').textContent = 'Could not load records: ' + error.message;
        if (!append) message($('records'), 'Search or refresh the library to retry.', true);
        $('load-more').hidden = !(append && state.hasMore);
      }
    } finally {
      if (version === listVersion) setBusy(false);
    }
  }

  // Build Markdown elements using text nodes only; HTML in records is never executed.
  function inline(parent, text) {
    const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g;
    let start = 0;
    for (const match of text.matchAll(pattern)) {
      parent.append(document.createTextNode(text.slice(start, match.index)));
      const token = match[0];
      if (token.startsWith('`')) parent.append(node('code', token.slice(1, -1)));
      else if (token.startsWith('**')) parent.append(node('strong', token.slice(2, -2)));
      else {
        const parts = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
        // Record links are resolved through the Connections section. Only web links are clickable here.
        if (/^https?:\/\//i.test(parts[2])) {
          const anchor = node('a', parts[1]);
          anchor.href = parts[2];
          anchor.target = '_blank';
          anchor.rel = 'noopener noreferrer';
          parent.append(anchor);
        } else parent.append(document.createTextNode(token));
      }
      start = match.index + token.length;
    }
    parent.append(document.createTextNode(text.slice(start)));
  }
  function markdown(content) {
    const body = node('div', undefined, 'body');
    const lines = String(content ?? '').replace(/\r\n?/g, '\n').split('\n');
    let paragraph = [], list = null, code = null, fence = '';
    const flush = () => {
      if (paragraph.length) {const p = node('p'); inline(p, paragraph.join('\n')); body.append(p); paragraph = [];}
      list = null;
    };
    for (const line of lines) {
      const marker = /^\s*(`{3,}|~{3,})(.*)$/.exec(line);
      if (code !== null) {
        if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) {
          const pre = node('pre'); pre.append(node('code', code.join('\n'))); body.append(pre); code = null;
        } else code.push(line);
        continue;
      }
      if (marker) {flush(); fence = marker[1]; code = []; continue;}
      if (!line.trim()) {flush(); continue;}
      const heading = /^(#{1,6})\s+(.+)$/.exec(line);
      const item = /^\s*(?:([-+*])|\d+[.)])\s+(.+)$/.exec(line);
      if (heading) {flush(); const h = node('h' + heading[1].length); inline(h, heading[2]); body.append(h);}
      else if (item) {
        if (paragraph.length) flush();
        const kind = item[1] ? 'UL' : 'OL';
        if (!list || list.tagName !== kind) {list = node(kind.toLowerCase()); body.append(list);}
        const li = node('li'); inline(li, item[2]); list.append(li);
      } else if (/^>\s?/.test(line)) {flush(); const quote = node('blockquote'); inline(quote, line.replace(/^>\s?/, '')); body.append(quote);}
      else {list = null; paragraph.push(line);}
    }
    flush();
    if (code !== null) {const pre = node('pre'); pre.append(node('code', code.join('\n'))); body.append(pre);}
    if (!String(content ?? '').trim()) body.append(node('p', 'This record has no body content.', 'muted'));
    return body;
  }
  function renderDetail(record) {
    const key = record.key;
    const detail = baseDetail(`${key[0]} / ${key[1]}`);
    detail.append(node('span', record.type || 'record', 'badge'), node('h2', record.title || key[2]), node('p', record.description || '', 'description'));
    const metadata = node('dl', undefined, 'meta');
    const fields = {Module: key[0], Collection: key[1]};
    for (const [key, label] of [['source','Source / client'],['session_id','Native session ID'],['project','Project'],['source_uri','Original session'],['updated_at','Description updated']]) {
      if (record.metadata?.[key]) fields[label] = record.metadata[key];
    }
    const status = record.status ?? record.metadata?.status;
    if (status) fields.Status = status;
    for (const [name, value] of Object.entries(fields)) {
      const pair = node('div'); pair.append(node('dt', name), node('dd', displayValue(value))); metadata.append(pair);
    }
    detail.append(metadata);
    for (const group of record.link_groups || []) {
      detail.append(node('h3', group.title, 'section-title'));
      if (!group.links?.length) detail.append(node('p', group.empty_message || 'No linked records.', 'muted'));
      for (const link of group.links || []) detail.append(recordLink(link));
    }
    detail.append(markdown(record.content));
    const pagination = record.metadata?.pagination;
    if (pagination && typeof pagination === 'object') {
      const offset = Number.isInteger(pagination.offset) && pagination.offset >= 0 ? pagination.offset : 0;
      const limit = Number.isInteger(pagination.limit) && pagination.limit > 0 ? Math.min(pagination.limit, 100) : 40;
      const returned = Number.isInteger(pagination.returned) && pagination.returned >= 0 ? pagination.returned : 0;
      const total = Number.isInteger(pagination.total) && pagination.total >= 0 ? pagination.total : null;
      const partial = offset > 0 || Boolean(pagination.has_more) || (total !== null && returned < total);
      const paging = node('section', undefined, 'content-paging');
      paging.setAttribute('aria-label', 'Conversation content pages');
      const range = returned ? `${offset + 1}–${offset + returned}` : '0';
      paging.append(node('p', `Conversation blocks ${range}${total !== null ? ` of ${total}` : ''}${partial ? ' · partial content' : ''}`, 'page-status'));
      if (record.metadata.content_source) paging.append(node('p', 'Source: ' + displayValue(record.metadata.content_source), 'source'));
      const controls = node('div', undefined, 'page-controls');
      const previous = node('button', 'Previous blocks', 'back');
      previous.type = 'button';
      previous.disabled = offset === 0;
      previous.addEventListener('click', () => openRecord(key, true, Math.max(0, offset - limit), limit));
      const next = node('button', 'Next blocks', 'back');
      next.type = 'button';
      next.disabled = !pagination.has_more;
      const nextOffset = Number.isInteger(pagination.next_offset) && pagination.next_offset > offset ? pagination.next_offset : offset + Math.max(returned, limit);
      next.addEventListener('click', () => openRecord(key, true, nextOffset, limit));
      controls.append(previous, next);
      paging.append(controls);
      detail.append(paging);
    }
    const fullMetadata = node('details', undefined, 'full-metadata');
    fullMetadata.append(node('summary', 'Record metadata'));
    const values = {ID: key[2], ...(record.metadata && typeof record.metadata === 'object' ? record.metadata : {})};
    if (record.revision !== undefined) values.Revision = record.revision;
    const metadataBody = node('dl', undefined, 'metadata-body');
    for (const [name, value] of Object.entries(values)) {
      const pair = node('div'); pair.append(node('dt', name), node('dd', displayValue(value))); metadataBody.append(pair);
    }
    fullMetadata.append(metadataBody);
    detail.append(fullMetadata);
    detail.append(node('h3', 'Connections', 'section-title'));
    if (!record.links?.length) detail.append(node('p', 'No linked records.', 'muted'));
    for (const link of record.links || []) detail.append(recordLink(link));
    if (record.path) detail.append(node('h3', 'Source', 'section-title'), node('div', record.path, 'source'));
  }
  function recordLink(link) {
    const button = node('button', undefined, 'link');
    button.type = 'button';
    if (link.label) button.append(node('small', link.label, 'eyebrow'));
    button.append(node('span', link.title || link.reference || link.key?.[2] || 'Linked record'));
    if (link.description) button.append(node('small', link.description));
    if (link.unavailable || !validKey(link.key)) {
      button.disabled = true;
      button.append(node('small', (link.unavailable ? 'Unavailable' : 'Reference') + (link.reference ? ' · ' + link.reference : '')));
    } else {
      button.append(node('small', (link.path || link.key.join(' / ')) + ' ↗'));
      button.addEventListener('click', () => openRecord(link.key, true));
    }
    return button;
  }
  async function openRecord(key, focus = false, offset = 0, limit = 40) {
    if (!validKey(key)) return;
    const version = ++detailVersion;
    state.selected = key;
    renderRecords();
    baseDetail(key[0] + ' / ' + key[1]).append(node('p', 'Loading full record…', 'muted'));
    if (focus) $('detail').focus({preventScroll: true});
    try {
      const result = await api('/api/record', {module: key[0], collection: key[1], id: key[2], offset, limit}, 'detail');
      if (version !== detailVersion) return;
      if (result.error) throw new Error(displayValue(result.error));
      renderDetail({...result, key: validKey(result.key) ? result.key : key});
      if (focus && window.matchMedia('(max-width: 820px)').matches) $('detail').scrollIntoView({behavior: 'smooth', block: 'start'});
    } catch (error) {
      if (!cancelled(error) && version === detailVersion) {
        const detail = baseDetail(key.join(' / '));
        detail.append(node('h2', 'Record unavailable'), node('p', error.message, 'error'));
        const retry = node('button', 'Retry', 'back'); retry.addEventListener('click', () => openRecord(key, false, offset, limit)); detail.append(retry);
      }
    }
  }
  function submitSearch() {
    clearTimeout(debounce);
    state.query = $('search').value.trim();
    loadRecords();
  }
  function setMode(mode) {
    clearTimeout(debounce);
    state.mode = mode;
    for (const value of ['text', 'semantic']) {
      $(value + '-mode').classList.toggle('active', value === mode);
      $(value + '-mode').setAttribute('aria-pressed', String(value === mode));
    }
    $('search').placeholder = mode === 'semantic' ? 'Describe what you are looking for…' : 'Search titles and descriptions…';
    $('search-hint').textContent = mode === 'semantic' ? 'Search by meaning. Press Enter or Search to run.' : 'Text search matches titles and descriptions.';
    $('run-search').textContent = mode === 'semantic' ? 'Search by meaning' : 'Search';
    state.query = '';
    $('search').value = '';
    loadRecords();
  }
  $('search-form').addEventListener('submit', event => {event.preventDefault(); submitSearch();});
  $('search').addEventListener('input', () => {
    clearTimeout(debounce);
    if (state.mode === 'text') debounce = setTimeout(submitSearch, 250);
  });
  $('text-mode').addEventListener('click', () => setMode('text'));
  $('semantic-mode').addEventListener('click', () => setMode('semantic'));
  $('collection').addEventListener('change', () => loadRecords());
  $('type').addEventListener('change', () => {renderViews(); loadRecords();});
  $('load-more').addEventListener('click', () => loadRecords(true));
  $('refresh').addEventListener('click', loadModules);
  loadModules();
})();
