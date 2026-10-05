"""Client script. Rules: no innerHTML/eval/document.write; every server string goes through textContent.
Article HTML from the server is re-parsed with DOMParser and rebuilt from an allow-list (defense in depth)."""

APP_JS = r"""
(function () {
  'use strict';
  var root = document.getElementById('app');
  var state = { me: null, csrf: null, tab: 'ask', wasPersonal: false, pst: null };
  var statusLine = null, askNote = null;

  function h(tag, attrs) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'text') el.textContent = attrs[k];
      else if (k === 'on') Object.keys(attrs.on).forEach(function (ev) { el.addEventListener(ev, attrs.on[ev]); });
      else el.setAttribute(k, attrs[k]);
    });
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c == null) continue;
      el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return el;
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

  function api(method, url, opts) {
    opts = opts || {};
    var headers = {};
    var init = { method: method, headers: headers, credentials: 'same-origin' };
    if (state.csrf && method !== 'GET') headers['X-CSRF-Token'] = opts.csrf || state.csrf;
    else if (opts.csrf) headers['X-CSRF-Token'] = opts.csrf;
    if (opts.json !== undefined) { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json); }
    if (opts.raw !== undefined) { headers['Content-Type'] = 'application/octet-stream'; init.body = opts.raw; }
    return fetch(url, init).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (r.status === 401 && url !== '/login') {
          state.wasPersonal = !!(state.me && state.me.personal) || state.wasPersonal;
          state.me = null; state.csrf = null; render();
        }
        return { status: r.status, ok: r.ok, data: data };
      });
    });
  }
  var ERR = { unauthorized: '로그인이 필요합니다.', invalid_credentials: '로그인에 실패했습니다.', forbidden: '권한이 없습니다.',
    not_found: '문서를 찾을 수 없습니다.', csrf: '요청이 거부되었습니다. 새로고침 후 다시 시도하세요.', too_large: '파일이 너무 큽니다.',
    bad_extension: '허용되지 않는 파일 형식입니다 (.eml .md .txt .docx .xlsx .pdf).', bad_filename: '파일 이름이 올바르지 않습니다.',
    llm_unavailable: '답변 서버에 연결할 수 없습니다.', llm_busy: '문서 처리 중이라 답변이 지연됩니다. 잠시 후 다시 시도하세요.', bad_content: '파일 내용이 형식과 일치하지 않습니다.' };
  function errText(d) { return (d && ERR[d.error]) || '오류가 발생했습니다.'; }

  // ---- article HTML: rebuild from allow-list ----
  var TAGS = { H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1, P: 1, UL: 1, OL: 1, LI: 1, STRONG: 1, CODE: 1, PRE: 1, A: 1, BR: 1 };
  function safeHref(u) {
    if (!u || /[\u0000- \u007f-\u009f\\<>"']/.test(u)) return null;
    if (/^#\/article\//.test(u)) return u;
    if (/^[a-z][a-z0-9+.\-]*:/i.test(u)) return /^https?:\/\/./i.test(u) ? u : null;
    return u.indexOf('//') === 0 ? null : u;
  }
  function rebuild(src, dst) {
    src.childNodes.forEach(function (n) {
      if (n.nodeType === 3) { dst.appendChild(document.createTextNode(n.nodeValue)); return; }
      if (n.nodeType !== 1 || !TAGS[n.tagName]) return;
      var el = document.createElement(n.tagName.toLowerCase());
      if (n.tagName === 'A') {
        var href = safeHref(n.getAttribute('href'));
        if (href) { el.setAttribute('href', href); if (!/^#/.test(href)) el.setAttribute('rel', 'noopener noreferrer nofollow'); }
      }
      rebuild(n, el);
      dst.appendChild(el);
    });
  }
  function safeInto(target, htmlText) {
    var doc = new DOMParser().parseFromString(htmlText, 'text/html');
    clear(target);
    rebuild(doc.body, target);
  }

  // ---- views ----
  function expiredView() {
    return h('section', {}, h('h2', { text: '세션이 만료되었습니다' }),
      h('p', { text: '개인 위키를 실행한 창에 표시된 주소로 다시 접속하세요.' }));
  }

  function loginView() {
    var msg = h('p', { 'class': 'error' });
    var input = h('input', { id: 'uid', placeholder: '사용자 ID', autocomplete: 'username' });
    var list = h('datalist', { id: 'users' });
    input.setAttribute('list', 'users');
    var box = h('section', {}, h('h2', { text: '로그인' }), input, list,
      h('button', { 'class': 'primary', text: '로그인', on: { click: doLogin } }), msg);
    var token = null;
    api('GET', '/api/login-info').then(function (r) {
      token = r.data.csrf;
      (r.data.dev_users || []).forEach(function (u) { list.appendChild(h('option', { value: u })); });
    });
    function doLogin() {
      msg.textContent = '';
      api('POST', '/login', { json: { user_id: input.value.trim() }, csrf: token }).then(function (r) {
        if (!r.ok) { msg.textContent = errText(r.data); return; }
        state.me = r.data; state.csrf = r.data.csrf; render();
      });
    }
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') doLogin(); });
    return box;
  }

  function noteText() {
    return state.pst && state.pst.working ? '문서 처리 중이라 답변이 늦어질 수 있습니다' : '';
  }

  function refreshStatus() {
    if (!state.me || !state.me.personal) return;
    api('GET', '/api/personal/status').then(function (r) {
      if (!r.ok) return;
      state.pst = r.data;
      if (statusLine) statusLine.textContent = '개인 위키 · 처리 대기 ' + r.data.pending + '건 / 실패 ' + r.data.failed + '건';
      if (askNote) askNote.textContent = noteText();
    });
  }

  function settingsView() {
    var box = h('section', {}, h('h2', { text: '설정' }));
    api('GET', '/api/personal/info').then(function (r) {
      if (!r.ok) { box.appendChild(h('p', { 'class': 'error', text: errText(r.data) })); return; }
      box.appendChild(h('p', { text: '데이터 폴더: ' + r.data.home }));
      box.appendChild(h('p', { text: '위키 폴더(옵시디언): ' + r.data.wiki }));
      box.appendChild(h('p', { text: '감시 폴더: ' + (r.data.watch.length ? r.data.watch.join(' ; ') : '(없음)') }));
      box.appendChild(h('p', { 'class': 'muted', text: '원본 파일은 읽기만 하며 이동·수정·삭제하지 않습니다. 변경은 환경 파일(WIKI_PERSONAL_WATCH)에서 합니다.' }));
    });
    return box;
  }

  function askView() {
    askNote = h('p', { 'class': 'muted', text: noteText() });
    var q = h('textarea', { rows: '3', maxlength: '2000', placeholder: '질문을 입력하세요' });
    var out = h('div', {});
    function ask() {
      var text = q.value.trim();
      if (!text) return;
      clear(out); out.appendChild(h('p', { 'class': 'muted', text: '답변을 생성하는 중...' }));
      api('POST', '/api/query', { json: { question: text } }).then(function (r) {
        clear(out);
        if (!r.ok) { out.appendChild(h('p', { 'class': 'error', text: errText(r.data) })); return; }
        out.appendChild(h('div', { 'class': 'answer', text: r.data.answer }));
        var cites = h('div', { 'class': 'cites' });
        (r.data.citations || []).forEach(function (c, i) {
          cites.appendChild(h('button', { text: '[' + (i + 1) + '] ' + c.title, on: { click: function () { openArticle(c.path); } } }));
        });
        out.appendChild(cites);
      });
    }
    return h('section', {}, h('h2', { text: '질문' }), q, h('button', { 'class': 'primary', text: '질문하기', on: { click: ask } }), askNote, out);
  }

  var articleBox = h('section', { id: 'article' });
  function openArticle(path) {
    state.tab = 'doc';
    location.hash = '#/article/' + encodeURIComponent(path).replace(/%2F/g, '/');
  }
  function loadArticle(path) {
    clear(articleBox);
    articleBox.appendChild(h('p', { 'class': 'muted', text: '불러오는 중...' }));
    api('GET', '/api/article?path=' + encodeURIComponent(path)).then(function (r) {
      clear(articleBox);
      if (!r.ok) { articleBox.appendChild(h('p', { 'class': 'error', text: errText(r.data) })); return; }
      articleBox.appendChild(h('h2', { text: r.data.title }));
      articleBox.appendChild(h('p', { 'class': 'muted', text: r.data.path + (r.data.updated ? ' · ' + r.data.updated : '') }));
      var body = h('div', {});
      safeInto(body, r.data.html);
      articleBox.appendChild(body);
    });
  }
  function docView() {
    if (!articleBox.firstChild) articleBox.appendChild(h('p', { 'class': 'muted', text: '답변의 인용 링크를 눌러 문서를 엽니다.' }));
    return articleBox;
  }

  function uploadView() {
    var sel = h('select', {});
    state.me.spaces.forEach(function (s) { sel.appendChild(h('option', { value: s, text: s })); });
    var single = state.me.spaces.length === 1;  // one space: no choice to make, use it automatically
    var file = h('input', { type: 'file', accept: '.eml,.md,.txt,.docx,.xlsx,.pdf' });
    var msg = h('p', {});
    function send() {
      var f = file.files[0];
      if (!f || !sel.value) { msg.className = 'error'; msg.textContent = '공간과 파일을 선택하세요.'; return; }
      msg.className = 'muted'; msg.textContent = '업로드 중...';
      var url = '/api/upload?space=' + encodeURIComponent(sel.value) + '&filename=' + encodeURIComponent(f.name);
      api('POST', url, { raw: f }).then(function (r) {
        msg.className = r.ok ? 'ok' : 'error';
        msg.textContent = r.ok ? '업로드 완료: ' + r.data.name : errText(r.data);
      });
    }
    return h('section', {}, h('h2', { text: '업로드' }), single ? null : h('label', { text: '공간' }), single ? null : sel, file,
      h('button', { 'class': 'primary', text: '업로드', on: { click: send } }), msg);
  }

  function render() {
    clear(root);
    if (!state.me) { root.appendChild(state.wasPersonal ? expiredView() : loginView()); return; }
    var personal = !!state.me.personal;
    if (personal) document.title = '개인 위키';
    statusLine = personal ? h('span', { 'class': 'muted' }) : null;
    if (statusLine && state.pst) statusLine.textContent = '개인 위키 · 처리 대기 ' + state.pst.pending + '건 / 실패 ' + state.pst.failed + '건';
    root.appendChild(h('header', {}, h('strong', { text: personal ? '개인 위키' : '사내 위키' }),
      h('span', {}, statusLine, personal ? null : state.me.name + ' (' + state.me.department + ')',
        personal ? null : h('button', { text: '로그아웃', on: { click: function () {
          api('POST', '/logout').then(function () { state.me = null; state.csrf = null; render(); }); } } }))));
    var views = { ask: askView, doc: docView, up: uploadView, set: settingsView };
    var tabs = [['ask', '질문'], ['doc', '문서'], ['up', '업로드']];
    if (personal) tabs.push(['set', '설정']);
    var nav = h('nav', {});
    tabs.forEach(function (t) {
      nav.appendChild(h('button', { text: t[1], 'class': state.tab === t[0] ? 'active' : '',
        on: { click: function () { state.tab = t[0]; render(); } } }));
    });
    root.appendChild(h('main', {}, nav, views[state.tab]()));
  }

  function route() {
    var m = /^#\/article\/(.+)$/.exec(location.hash);
    if (!m || !state.me) return;
    var path;
    try { path = decodeURIComponent(m[1]); } catch (e) { return; }
    state.tab = 'doc'; render(); loadArticle(path);
  }
  window.addEventListener('hashchange', route);
  api('GET', '/api/me').then(function (r) {
    if (r.ok) { state.me = r.data; state.csrf = r.data.csrf; }
    render(); route(); refreshStatus();
  });
  setInterval(refreshStatus, 5000);
})();
"""
