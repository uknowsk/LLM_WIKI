"""Client script. Rules: no innerHTML/eval/document.write; every server string goes through textContent.
Article HTML from the server is re-parsed with DOMParser and rebuilt from an allow-list (defense in depth)."""

APP_JS = r"""
(function () {
  'use strict';
  var root = document.getElementById('app');
  var state = { me: null, csrf: null, tab: 'ask' };

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
        if (r.status === 401 && url !== '/login') { state.me = null; state.csrf = null; render(); }
        return { status: r.status, ok: r.ok, data: data };
      });
    });
  }
  var ERR = { unauthorized: '로그인이 필요합니다.', invalid_credentials: '로그인에 실패했습니다.', forbidden: '권한이 없습니다.',
    not_found: '문서를 찾을 수 없습니다.', csrf: '요청이 거부되었습니다. 새로고침 후 다시 시도하세요.', too_large: '파일이 너무 큽니다.',
    bad_extension: '허용되지 않는 파일 형식입니다 (.eml .md .txt .docx .xlsx .pdf).', bad_filename: '파일 이름이 올바르지 않습니다.',
    llm_unavailable: '답변 서버에 연결할 수 없습니다.', bad_content: '파일 내용이 형식과 일치하지 않습니다.' };
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

  function askView() {
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
    return h('section', {}, h('h2', { text: '질문' }), q, h('button', { 'class': 'primary', text: '질문하기', on: { click: ask } }), out);
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
    return h('section', {}, h('h2', { text: '업로드' }), h('label', { text: '공간' }), sel, file,
      h('button', { 'class': 'primary', text: '업로드', on: { click: send } }), msg);
  }

  function render() {
    clear(root);
    if (!state.me) { root.appendChild(loginView()); return; }
    root.appendChild(h('header', {}, h('strong', { text: '사내 위키' }),
      h('span', {}, state.me.name + ' (' + state.me.department + ')',
        h('button', { text: '로그아웃', on: { click: function () {
          api('POST', '/logout').then(function () { state.me = null; state.csrf = null; render(); }); } } }))));
    var views = { ask: askView, doc: docView, up: uploadView };
    var nav = h('nav', {});
    [['ask', '질문'], ['doc', '문서'], ['up', '업로드']].forEach(function (t) {
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
    render(); route();
  });
})();
"""
