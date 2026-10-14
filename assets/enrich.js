/* vk_archive_enrich.py: navigation, photo viewer, sorting and search for the enriched VK archive.
   Plain script, no dependencies; works when the pages are opened straight from disk. */
(function () {
  'use strict';
  var ROOT = document.body.getAttribute('data-root') || '';
  var MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];

  function $(s, el) { return (el || document).querySelector(s); }
  function $$(s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); }
  function store(k, v) {
    try {
      if (v === undefined) return localStorage.getItem(k);
      localStorage.setItem(k, v);
    } catch (e) { /* private mode, blocked storage */ }
    return null;
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  // archive times are "local wall clock" seconds, so read them with UTC getters
  function d(t) { return new Date(t * 1000); }
  function fmtDate(t) { var x = d(t); return pad(x.getUTCDate()) + '.' + pad(x.getUTCMonth() + 1) + '.' + x.getUTCFullYear(); }
  function isoDate(t) { var x = d(t); return x.getUTCFullYear() + '-' + pad(x.getUTCMonth() + 1) + '-' + pad(x.getUTCDate()); }
  function ruDate(t) { var x = d(t); return x.getUTCDate() + ' ' + MONTHS[x.getUTCMonth()] + ' ' + x.getUTCFullYear(); }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; });
  }

  /* ---------- theme: system / dark / light (the <head> script already applied it before drawing) ---------- */
  var THEMES = ['auto', 'dark', 'light'];
  var THEME_ICON = {  // half circle = like the system, moon = dark, sun = light
    auto: '<svg viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 2v14a7 7 0 0 1 0-14z"/></svg>',
    dark: '<svg viewBox="0 0 24 24"><path d="M20.5 14.6A8.5 8.5 0 0 1 9.4 3.5a8.5 8.5 0 1 0 11.1 11.1z"/></svg>',
    light: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4.6"/><path d="M11 1.5h2v3h-2zm0 18h2v3h-2zM1.5 11h3v2h-3zm18 0h3v2h-3zM4.2 5.6l1.4-1.4 2.1 2.1-1.4 1.4zm12.7 12.7 1.4-1.4 2.1 2.1-1.4 1.4zM4.2 18.4l2.1-2.1 1.4 1.4-2.1 2.1zM16.9 6.3l2.1-2.1 1.4 1.4-2.1 2.1z"/></svg>'
  };
  var THEME_NAME = {auto: 'как в системе', dark: 'тёмная', light: 'светлая'};
  var systemDark = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  function themeChoice() { var t = store('exTheme'); return THEMES.indexOf(t) > 0 ? t : 'auto'; }
  function applyTheme() {
    var choice = themeChoice();
    var dark = choice === 'dark' || (choice === 'auto' && systemDark && systemDark.matches);
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    if (themeBtn) {
      themeBtn.innerHTML = THEME_ICON[choice];
      themeBtn.title = 'Тема: ' + THEME_NAME[choice] + ' (нажмите, чтобы сменить)';
    }
  }
  var themeBtn = null;
  var headerBox = $('.page_header');
  if (headerBox) {
    themeBtn = document.createElement('button');
    themeBtn.type = 'button';
    themeBtn.className = 'ex-theme';
    themeBtn.addEventListener('click', function () {
      store('exTheme', THEMES[(THEMES.indexOf(themeChoice()) + 1) % THEMES.length]);
      applyTheme();
    });
    headerBox.appendChild(themeBtn);
  }
  if (systemDark) {
    var onSystem = function () { if (themeChoice() === 'auto') applyTheme(); };
    if (systemDark.addEventListener) systemDark.addEventListener('change', onSystem);
    else if (systemDark.addListener) systemDark.addListener(onSystem);
  }
  applyTheme();

  /* ---------- tooltips for [data-tip] (heatmaps, charts, gallery) ---------- */
  var tip = document.createElement('div');
  tip.className = 'ex-tip';
  document.body.appendChild(tip);
  function placeTip(e) {
    var x = e.clientX + 14, y = e.clientY + 14;
    if (x + tip.offsetWidth > window.innerWidth - 8) x = e.clientX - tip.offsetWidth - 10;
    if (y + tip.offsetHeight > window.innerHeight - 8) y = e.clientY - tip.offsetHeight - 10;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }
  document.addEventListener('mouseover', function (e) {
    var el = e.target.closest ? e.target.closest('[data-tip]') : null;
    if (!el) { tip.style.display = 'none'; return; }
    tip.textContent = el.getAttribute('data-tip');
    tip.style.display = 'block';
    placeTip(e);
  });
  document.addEventListener('mousemove', function (e) { if (tip.style.display === 'block') placeTip(e); });

  /* ---------- pagers: page picker, jump to date, oldest-first ---------- */
  var NAV = window.EXNAV;
  function goToTime(t0) {
    // pages are newest first: the target is the oldest page that still has messages at/after t0
    var target = 0;
    for (var i = NAV.files.length - 1; i >= 0; i--) {
      if (NAV.newest[i] && NAV.newest[i] >= t0) { target = i; break; }
    }
    var here = location.pathname.split('/').pop();
    if (NAV.files[target] === here) { scrollToTime(t0); history.replaceState(null, '', '#t' + t0); }
    else location.href = NAV.files[target] + '#t' + t0;
  }
  function scrollToTime(t0) {
    var best = null, bestT = Infinity, newest = null, newestT = -1;
    $$('.message[data-t]').forEach(function (m) {
      var t = +m.getAttribute('data-t');
      if (t >= t0 && t < bestT) { best = m; bestT = t; }
      if (t > newestT) { newest = m; newestT = t; }
    });
    var el = best || newest;
    if (!el) return;
    $$('.ex-target').forEach(function (x) { x.classList.remove('ex-target'); });
    el.classList.add('ex-target');
    el.scrollIntoView({block: 'center'});
  }

  function applyOrder(oldestFirst) {
    var wrap = $('.wrap_page_content');
    var want = oldestFirst ? 'asc' : 'desc';
    if (!wrap || (wrap.getAttribute('data-order') || 'desc') === want) return;
    var items = $$('.wrap_page_content > .item');
    $$('.wrap_page_content > .ex-day').forEach(function (x) { x.parentNode.removeChild(x); });
    var bottom = $('.wrap_page_content > .ex-pager-bottom');
    items.reverse().forEach(function (it) { wrap.insertBefore(it, bottom); });
    var prev = null;
    items.forEach(function (it) {
      var m = $('.message[data-t]', it), t = m ? +m.getAttribute('data-t') : 0;
      if (!t) return;
      var key = isoDate(t);
      if (key !== prev) {
        var sep = document.createElement('div');
        sep.className = 'ex-day';
        sep.id = 'd' + key.replace(/-/g, '');
        sep.innerHTML = '<span>' + ruDate(t) + '</span>';
        wrap.insertBefore(sep, it);
        prev = key;
      }
    });
    wrap.setAttribute('data-order', want);
  }

  $$('.ex-pager').forEach(function (pg) {
    var kind = pg.getAttribute('data-kind'), cur = +pg.getAttribute('data-cur');
    var files = kind === 'messages' ? (NAV && NAV.files) : JSON.parse(pg.getAttribute('data-files') || '[]');
    if (!files || files.length < 2) return;
    var sel = document.createElement('select');
    sel.className = 'ex-pg-select';
    sel.title = 'Перейти на страницу';
    files.forEach(function (f, i) {
      var o = document.createElement('option');
      o.value = f;
      o.textContent = (kind === 'messages' && NAV.newest[i])
        ? 'Стр. ' + (i + 1) + ': ' + fmtDate(NAV.oldest[i]) + ' – ' + fmtDate(NAV.newest[i])
        : 'Страница ' + (i + 1) + ' из ' + files.length;
      if (i === cur) o.selected = true;
      sel.appendChild(o);
    });
    sel.addEventListener('change', function () { location.href = sel.value; });
    var label = $('.ex-pg-label', pg);
    label.parentNode.replaceChild(sel, label);
    if (kind !== 'messages') return;
    var times = NAV.oldest.concat(NAV.newest).filter(Boolean);
    var date = document.createElement('input');
    date.type = 'date';
    date.title = 'Перейти к дате';
    if (times.length) {
      date.min = isoDate(Math.min.apply(null, times));
      date.max = isoDate(Math.max.apply(null, times));
    }
    date.addEventListener('change', function () {
      if (!date.value) return;
      var p = date.value.split('-');
      goToTime(Date.UTC(+p[0], +p[1] - 1, +p[2]) / 1000);
    });
    pg.appendChild(date);
    if (pg.classList.contains('ex-pager-bottom')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = 'Сначала старые';
    btn.title = 'Порядок сообщений на странице';
    var on = store('exOldestFirst') === '1';
    btn.classList.toggle('ex-on', on);
    btn.addEventListener('click', function () {
      on = !on;
      store('exOldestFirst', on ? '1' : '0');
      btn.classList.toggle('ex-on', on);
      applyOrder(on);
    });
    pg.appendChild(btn);
  });
  if (NAV) {
    if (store('exOldestFirst') === '1') applyOrder(true);
    var mt = /^#t(\d+)$/.exec(location.hash);
    if (mt) scrollToTime(+mt[1]);
  }

  /* ---------- keyboard: arrows switch pages ---------- */
  document.addEventListener('keydown', function (e) {
    if (lb || e.altKey || e.ctrlKey || e.metaKey) return;
    var tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
    var a = null;
    if (e.key === 'ArrowLeft') a = $('.ex-pager a.ex-pg-prev');
    if (e.key === 'ArrowRight') a = $('.ex-pager a.ex-pg-next');
    if (a) location.href = a.getAttribute('href');
  });

  /* ---------- photo viewer ---------- */
  var lb = null, lbList = [], lbIdx = 0;
  function showLb() {
    var a = lbList[lbIdx];
    var full = a.getAttribute('href'), msg = a.getAttribute('data-msg');
    lb.querySelector('img').src = full;
    lb.querySelector('.ex-lb-bar').innerHTML = (lbIdx + 1) + ' / ' + lbList.length +
      '<a href="' + escapeHtml(full) + '" target="_blank">открыть файл</a>' +
      (msg ? '<a href="' + escapeHtml(msg) + '">к сообщению</a>' : '');
  }
  function closeLb() { if (lb) { document.body.removeChild(lb); lb = null; } }
  function step(n) { lbIdx = (lbIdx + n + lbList.length) % lbList.length; showLb(); }
  document.addEventListener('click', function (e) {
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
    var a = e.target.closest ? e.target.closest('a.ex-photo') : null;
    if (!a || !a.querySelector('img')) return;
    e.preventDefault();
    lbList = $$('a.ex-photo').filter(function (x) { return x.querySelector('img'); });
    lbIdx = Math.max(0, lbList.indexOf(a));
    lb = document.createElement('div');
    lb.className = 'ex-lb';
    lb.innerHTML = '<img alt=""><div class="ex-lb-bar"></div><span class="ex-lb-nav ex-lb-prev">‹</span>' +
      '<span class="ex-lb-nav ex-lb-next">›</span><span class="ex-lb-close" title="Закрыть (Esc)">×</span>';
    lb.addEventListener('click', function (ev) {
      if (ev.target.classList.contains('ex-lb-prev')) step(-1);
      else if (ev.target.classList.contains('ex-lb-next')) step(1);
      else if (ev.target === lb || ev.target.classList.contains('ex-lb-close')) closeLb();
    });
    document.body.appendChild(lb);
    showLb();
  });
  document.addEventListener('keydown', function (e) {
    if (!lb) return;
    if (e.key === 'Escape') closeLb();
    else if (e.key === 'ArrowLeft') step(-1);
    else if (e.key === 'ArrowRight') step(1);
  });

  /* ---------- back to top ---------- */
  var top = document.createElement('button');
  top.className = 'ex-top';
  top.title = 'Наверх';
  top.textContent = '↑';
  top.addEventListener('click', function () { window.scrollTo(0, 0); });
  document.body.appendChild(top);
  window.addEventListener('scroll', function () { top.style.display = window.scrollY > 1200 ? 'block' : 'none'; });

  /* ---------- conversation list: sort & filter ---------- */
  var peersBox = $('#ex-peers');
  if (peersBox) {
    var rows = $$('.ex-peer', peersBox), q = $('#ex-q'), flt = $('#ex-filter'), sortSel = $('#ex-sort');
    sortSel.value = store('exPeerSort') || 'last';
    var applyPeers = function () {
      var qs = q.value.trim().toLowerCase(), ft = flt.value, key = sortSel.value;
      var num = function (r, k) { return +r.getAttribute('data-' + k) || 0; };
      rows.slice().sort(function (a, b) {
        if (key === 'name') return a.getAttribute('data-name').localeCompare(b.getAttribute('data-name'), 'ru');
        if (key === 'first') return (num(a, 'first') || Infinity) - (num(b, 'first') || Infinity);
        return num(b, key) - num(a, key);
      }).forEach(function (r) {
        var show = (!qs || r.getAttribute('data-name').indexOf(qs) >= 0) && (!ft || r.getAttribute('data-type') === ft);
        r.style.display = show ? '' : 'none';
        peersBox.appendChild(r);
      });
      store('exPeerSort', key);
    };
    q.addEventListener('input', applyPeers);
    flt.addEventListener('change', applyPeers);
    sortSel.addEventListener('change', applyPeers);
    applyPeers();
  }

  /* ---------- statistics: sent / received / all, optionally without communities ---------- */
  var ST = window.EXSTATS;
  if (ST && $('.ex-stats')) initStats();

  function plural(n, f) {
    n = Math.abs(n) % 100;
    if (n > 10 && n < 20) return f[2];
    n %= 10;
    return n === 1 ? f[0] : (n >= 2 && n <= 4 ? f[1] : f[2]);
  }
  function fnum(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' '); }
  function msgs(n) { return fnum(n) + ' ' + plural(n, ['сообщение', 'сообщения', 'сообщений']); }
  function keyDate(k) { var p = k.split('-'); return p[2] + '.' + p[1] + '.' + p[0]; }

  function initStats() {
    var MSHORT = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
    var WD = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
    var mode = store('exStatsMode') || 'sent';
    var cb = $('#ex-nogroups');
    var noGroups = cb ? store('exStatsNoGroups') !== '0' : false;
    if (cb) cb.checked = noGroups;
    var buttons = $$('.ex-seg button');

    // a = [sent, received, sent to communities, received from communities]
    function split(a) { return a ? [a[0] + (noGroups ? 0 : a[2]), a[1] + (noGroups ? 0 : a[3])] : [0, 0]; }
    function pick(x) { return mode === 'sent' ? x[0] : mode === 'recv' ? x[1] : x[0] + x[1]; }
    function dayVal(k) { return pick(split(ST.days[k])); }

    function dayTip(k, c) {
      if (!c) return keyDate(k) + ' · нет сообщений';
      var x = split(ST.days[k]), t = keyDate(k) + ' · ' + msgs(c);
      if (mode === 'all') t += ' (отправлено ' + fnum(x[0]) + ', получено ' + fnum(x[1]) + ')';
      if (ST.top && ST.top[k]) {
        var top = ST.top[k].filter(function (e) { return !(noGroups && ST.peers[e[0]] && ST.peers[e[0]][2]); })
          .map(function (e) { return [e[0], pick([e[1], e[2]])]; })
          .filter(function (e) { return e[1] > 0; })
          .sort(function (a, b) { return b[1] - a[1]; }).slice(0, 3);
        if (top.length) t += ': ' + top.map(function (e) { return (ST.peers[e[0]] || [e[0]])[0] + ' (' + e[1] + ')'; }).join(', ');
      }
      return t;
    }

    function heatmap() {
      var keys = Object.keys(ST.days).filter(function (k) { return dayVal(k) > 0; });
      if (!keys.length) return '<div class="ex-empty-note">Нет сообщений.</div>';
      var vals = keys.map(dayVal).sort(function (a, b) { return a - b; });
      var th = [0.25, 0.5, 0.75].map(function (q) { return vals[Math.floor(vals.length * q)]; });
      var level = function (c) { var l = c ? 1 : 0; if (c) th.forEach(function (x) { if (c > x) l++; }); return l; };
      var years = {};
      keys.forEach(function (k) { years[k.slice(0, 4)] = 1; });
      var out = Object.keys(years).sort().reverse().map(function (ys) {
        var y = +ys, start = Date.UTC(y, 0, 1), lead = (new Date(start).getUTCDay() + 6) % 7;
        var cells = '', months = '', total = 0, i = 0;
        for (var t = start; new Date(t).getUTCFullYear() === y; t += 86400000, i++) {
          var dd = new Date(t), k = y + '-' + pad(dd.getUTCMonth() + 1) + '-' + pad(dd.getUTCDate());
          var c = dayVal(k), idx = i + lead, x = 26 + Math.floor(idx / 7) * 12, yy = 16 + (idx % 7) * 12;
          total += c;
          var rect = '<rect x="' + x + '" y="' + yy + '" width="10" height="10" rx="2" class="h' + level(c) +
                     '" data-tip="' + escapeHtml(dayTip(k, c)) + '"/>';
          var href = c && ST.links ? ST.links[k] : null;
          cells += href ? '<a href="' + escapeHtml(href) + '">' + rect + '</a>' : rect;
          if (dd.getUTCDate() === 1) months += '<text x="' + x + '" y="10">' + MSHORT[dd.getUTCMonth()] + '</text>';
        }
        var width = 26 + Math.floor((i - 1 + lead) / 7) * 12 + 12;
        var wd = [0, 2, 4].map(function (j) { return '<text x="0" y="' + (16 + j * 12 + 9) + '">' + WD[j] + '</text>'; }).join('');
        return '<div class="ex-heat-year"><div class="ex-heat-label"><b>' + y + '</b><span class="ex-dim">' + msgs(total) +
               '</span></div><svg class="ex-heat" viewBox="0 0 ' + width + ' 102" width="' + width + '" role="img" aria-label="' + y +
               '">' + months + wd + cells + '</svg></div>';
      }).join('');
      var legend = '<div class="ex-heat-legend">меньше ' + [0, 1, 2, 3, 4].map(function (l) { return '<i class="h' + l + '"></i>'; }).join('') + ' больше</div>';
      return '<div class="ex-heatmap">' + out + legend + '</div>';
    }

    function columns(values, labels, tips) {
      var top = Math.max.apply(null, values) || 1;
      return '<div class="ex-cols" style="height:90px">' + values.map(function (v, i) {
        return '<div class="ex-col" data-tip="' + escapeHtml(tips[i]) + '"><i style="height:' + (100 * v / top).toFixed(1) +
               '%"></i><span>' + escapeHtml(labels[i]) + '</span></div>';
      }).join('') + '</div>';
    }

    function draw() {
      buttons.forEach(function (b) { b.classList.toggle('ex-on', b.getAttribute('data-mode') === mode); });
      var keys = Object.keys(ST.days), sent = 0, recv = 0, active = 0, best = null, bestV = 0, byYear = {};
      keys.forEach(function (k) {
        var x = split(ST.days[k]), v = pick(x);
        sent += x[0]; recv += x[1];
        if (v) { active++; byYear[k.slice(0, 4)] = (byYear[k.slice(0, 4)] || 0) + v; }
        if (v > bestV) { bestV = v; best = k; }
      });
      var bestHtml = best ? (ST.links && ST.links[best] ? '<a href="' + escapeHtml(ST.links[best]) + '">' + keyDate(best) + '</a>' : keyDate(best)) +
                            ' · ' + fnum(bestV) : '—';
      // sent / received / total always shown; the one the switch selects is highlighted
      var tiles = [['Отправлено', fnum(sent), mode === 'sent'], ['Получено', fnum(recv), mode === 'recv'],
                   ['Всего', fnum(sent + recv), mode === 'all'], ['Дней с сообщениями', fnum(active)],
                   ['Самый активный день', bestHtml]];
      $('#ex-st-tiles').innerHTML = tiles.map(function (t) {
        return '<div class="ex-tile' + (t[2] ? ' ex-tile-on' : '') + '"><div class="ex-dim">' + t[0] +
               '</div><div class="ex-tile-v">' + t[1] + '</div></div>';
      }).join('');
      $('#ex-st-heat').innerHTML = heatmap();
      var ys = Object.keys(byYear).sort();
      $('#ex-st-years').innerHTML = ys.length ? columns(ys.map(function (y) { return byYear[y]; }), ys,
        ys.map(function (y) { return y + ' · ' + msgs(byYear[y]); })) : '';
      var hv = ST.hours.map(function (a) { return pick(split(a)); }), wv = ST.wdays.map(function (a) { return pick(split(a)); });
      $('#ex-st-hours').innerHTML = columns(hv, hv.map(function (_, h) { return h % 3 === 0 ? String(h) : ''; }),
        hv.map(function (v, h) { return pad(h) + ':00–' + pad(h) + ':59 · ' + msgs(v); }));
      $('#ex-st-wdays').innerHTML = columns(wv, WD, wv.map(function (v, i) { return WD[i] + ' · ' + msgs(v); }));
      var rank = $('#ex-st-rank');
      if (rank && ST.peers) {
        var list = Object.keys(ST.peers).map(function (p) { var e = ST.peers[p]; return [p, e, pick([e[3], e[4]])]; })
          .filter(function (e) { return e[2] > 0 && !(noGroups && e[1][2]); })
          .sort(function (a, b) { return b[2] - a[2]; }).slice(0, 40);
        var mx = list.length ? list[0][2] : 1;
        rank.innerHTML = '<div class="ex-ranks">' + list.map(function (e) {
          var ava = e[1][1] ? '<img class="ex-ava ex-ava-sm" src="' + escapeHtml(e[1][1]) + '" alt="" loading="lazy">'
                            : '<span class="ex-ava ex-ava-sm ex-ava-ph">' + escapeHtml((e[1][0] || '?').charAt(0)) + '</span>';
          return '<div class="ex-rank"><span class="ex-rank-who">' + ava + '<a href="' + e[0] + '/stats.html">' + escapeHtml(e[1][0]) +
                 '</a></span><span class="ex-rank-bar"><i style="width:' + (100 * e[2] / mx).toFixed(1) + '%"></i></span><span class="ex-rank-n">' +
                 fnum(e[2]) + '</span></div>';
        }).join('') + '</div>';
      }
    }

    buttons.forEach(function (b) {
      b.addEventListener('click', function () { mode = b.getAttribute('data-mode'); store('exStatsMode', mode); draw(); });
    });
    if (cb) cb.addEventListener('change', function () { noGroups = cb.checked; store('exStatsNoGroups', noGroups ? '1' : '0'); draw(); });
    draw();
  }

  /* ---------- full-text search over all messages ---------- */
  var searchBox = $('#ex-search');
  if (searchBox) initSearch();

  function initSearch() {
    var S = {peers: [], names: {}, people: {}, me: 0, data: {}, loaded: 0, wanted: 0};
    var sq = $('#ex-sq'), speer = $('#ex-speer'), sauthor = $('#ex-sauthor'), status = $('#ex-sstatus'), out = $('#ex-sresults');
    var shown = 0, hits = [], words = [], timer = null;

    function handle(x) {
      if (x.init) {
        S.peers = x.init[0];
        S.people = x.init[1];
        S.me = x.init[2];
        S.peers.forEach(function (p) {
          S.names[p[0]] = p;
          var o = document.createElement('option');
          o.value = p[0];
          o.textContent = p[1] + ' (' + p[3] + ')';
          speer.appendChild(o);
        });
      } else if (x.peer !== undefined) {
        S.data[x.peer] = {meta: x.meta, rows: x.rows, lower: null};
        S.loaded++;
        progress();
        schedule(30);
      }
    }
    var queue = window.EXSQ || [];
    window.EXSQ = {push: handle};
    queue.forEach(handle);

    var params = new URLSearchParams(location.search);
    if (params.get('q')) sq.value = params.get('q');
    if (params.get('peer')) speer.value = params.get('peer');

    function load(peers) {
      var list = peers.filter(function (p) { return !S.data[p] && !loading[p]; });
      S.wanted += list.length;
      var i = 0;
      function next() {
        if (i >= list.length) return;
        var p = list[i++];
        loading[p] = true;
        var s = document.createElement('script');
        s.src = ROOT + 'search/' + p + '.js';
        s.onload = s.onerror = next;
        document.head.appendChild(s);
      }
      for (var k = 0; k < 6; k++) next();
    }
    var loading = {};
    function progress() {
      if (S.loaded < S.wanted) status.textContent = 'Загружаю переписки: ' + S.loaded + ' из ' + S.wanted + '…';
    }
    function scope() {
      return speer.value ? [+speer.value] : S.peers.map(function (p) { return p[0]; });
    }
    function schedule(ms) { clearTimeout(timer); timer = setTimeout(run, ms); }

    function run() {
      words = sq.value.toLowerCase().split(/\s+/).filter(Boolean);
      hits = [];
      shown = 0;
      out.innerHTML = '';
      var peers = scope();
      load(peers);
      if (!words.length) {
        status.textContent = S.loaded < S.wanted ? status.textContent : 'Введите слова для поиска. Ищутся тексты сообщений, в том числе пересланных.';
        return;
      }
      var who = sauthor.value;
      peers.forEach(function (p) {
        var D = S.data[p];
        if (!D) return;
        if (!D.lower) D.lower = D.rows.map(function (r) { return r[4].toLowerCase(); });
        for (var i = 0; i < D.rows.length; i++) {
          var t = D.lower[i], ok = true;
          for (var w = 0; w < words.length; w++) { if (t.indexOf(words[w]) < 0) { ok = false; break; } }
          if (!ok) continue;
          var author = D.meta.authors[D.rows[i][3]];
          if (who === 'me' && author !== S.me) continue;
          if (who === 'them' && author === S.me) continue;
          hits.push([p, D.rows[i], author]);
        }
      });
      hits.sort(function (a, b) { return b[1][1] - a[1][1]; });
      var done = S.loaded >= S.wanted;
      status.textContent = 'Найдено: ' + hits.length + (done ? '' : ' (ещё загружаю: ' + S.loaded + ' из ' + S.wanted + ')');
      more();
    }

    function snippet(text) {
      var low = text.toLowerCase(), first = low.indexOf(words[0]);
      var start = Math.max(0, first - 120), end = Math.min(text.length, first + 280);
      var cut = (start > 0 ? '…' : '') + text.slice(start, end) + (end < text.length ? '…' : '');
      var html = escapeHtml(cut);
      words.forEach(function (w) {
        var re = new RegExp(w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
        html = html.replace(re, function (m) { return '<mark>' + m + '</mark>'; });
      });
      return html;
    }

    function more() {
      var old = $('.ex-more', out);
      if (old) old.parentNode.removeChild(old);
      var frag = document.createDocumentFragment();
      hits.slice(shown, shown + 100).forEach(function (h) {
        var p = h[0], r = h[1], D = S.data[p], who = S.people[h[2]] || ['id' + h[2], ''];
        var peer = S.names[p] || [p, String(p), ''];
        var link = ROOT + 'messages/' + p + '/' + D.meta.pages[r[2]] + '#m' + r[0];
        var ava = who[1] ? '<img class="ex-ava" src="' + escapeHtml(ROOT + who[1]) + '" alt="" loading="lazy">'
                         : '<span class="ex-ava ex-ava-ph">' + escapeHtml((who[0] || '?').charAt(0)) + '</span>';
        var div = document.createElement('div');
        div.className = 'ex-hit';
        div.innerHTML = ava + '<div class="ex-hit-main"><div class="ex-head"><b>' + escapeHtml(who[0]) + '</b>' +
          (String(h[2]) !== String(p) ? '<span>в переписке «' + escapeHtml(peer[1]) + '»</span>' : '') +
          '<a href="' + escapeHtml(link) + '">' + (r[1] ? fmtDate(r[1]) : 'к сообщению') + '</a></div>' +
          '<div class="ex-hit-text">' + snippet(r[4]) + '</div></div>';
        frag.appendChild(div);
      });
      out.appendChild(frag);
      shown = Math.min(hits.length, shown + 100);
      if (shown < hits.length) {
        var b = document.createElement('button');
        b.className = 'ex-btn ex-more';
        b.textContent = 'Показать ещё (' + (hits.length - shown) + ')';
        b.addEventListener('click', more);
        out.appendChild(b);
      }
    }

    sq.addEventListener('input', function () { schedule(250); });
    speer.addEventListener('change', function () { schedule(0); });
    sauthor.addEventListener('change', function () { schedule(0); });
    run();
  }
})();
