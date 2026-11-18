'use strict';
(function () {
  var L = window.YJ;
  // data: summary.json. cases: the index entries loaded so far. full: whole cases from data/cases/.
  var S = { config: null, data: null, areas: null, geo: null, links: {}, linkFiles: {}, linkDone: {}, waiting: {}, cases: [], indexFiles: {}, full: {},
    distIndex: {}, distGeo: {}, token: 0, lastPath: null, keepFocus: null };
  var view = document.getElementById('view');
  var CASES_PER_PAGE = 20, RECORDS_PER_PAGE = 25, COVERAGE_PER_PAGE = 25, TOP_CASES = 5;
  var REFRESH_CMD = 'python run.py refresh --scheme jsjb --state Punjab --district Sangrur --area Rural --block Sangrur';

  var KINDS = {
    photo_reuse: { label: 'Same photo on several works', tag: 'Reused photo',
      short: 'One photo is attached as evidence to more than one work.',
      text: 'Each photo is fingerprinted from its exact file contents. When the identical file is attached as evidence to more than one work record, every work should have its own photo, so the reuse is flagged.' },
    same_day: { label: 'Many works started and finished the same day', tag: 'Same-day works',
      short: 'Several works in one place have the same start and end date.',
      text: 'Several works in one place whose start date and end date are the same day. Some small works can be done in a day, so this is a weaker signal on its own.' },
    same_gps: { label: 'Many works at one GPS point', tag: 'Same GPS point',
      short: 'Different works are recorded at exactly the same location.',
      text: 'Different works recorded at exactly the same latitude and longitude. Separate structures usually sit at separate points. Works already in a same-photo case are not counted again here; that case page says so when its works share one GPS point.' },
    upload_burst: { label: 'Photos uploaded in a burst', tag: 'Upload burst',
      short: 'One office uploaded many evidence photos within minutes.',
      text: 'A large number of evidence photos uploaded by one office in a short window, which can mean photos were added in bulk rather than at each site.' },
    vision_mismatch: { label: 'Photo may not show the claimed work, needs human review', tag: 'Image check',
      short: 'An automated image check doubts the photo matches the work type.',
      text: 'An optional automated image check, off by default, compares the photo with the type of work recorded. Cases from this check are marked as needing human review, because image models make mistakes.' }
  };
  var BANDS = [{ cls: 'f-1', min: 1, label: '1 to 4' }, { cls: 'f-2', min: 5, label: '5 to 19' }, { cls: 'f-3', min: 20, label: '20 to 99' }, { cls: 'f-4', min: 100, label: '100 or more' }];

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  var norm = L.norm;
  function plural(n, one, many) { return fmt(n) + ' ' + (n === 1 ? one : many); }
  function fmt(n) { return n == null ? 'not recorded' : Number(n).toLocaleString('en-IN'); }
  function kindLabel(k) { return (KINDS[k] || { label: k }).label; }
  function kindTag(k) { return (KINDS[k] || { tag: k }).tag; }
  function getJSON(url) {
    return fetch(url, { cache: 'no-cache' }).then(function (r) {
      if (!r.ok) { var e = new Error(url + ' returned ' + r.status); e.status = r.status; throw e; }
      return r.json();
    });
  }
  function stateHref(st) { return '#/state/' + encodeURIComponent(st); }
  function districtHref(st, d) { return stateHref(st) + '/district/' + encodeURIComponent(d); }
  function caseHref(id) { return '#/case/' + encodeURIComponent(id); }
  function scheme(id) { return (S.data.schemes || []).filter(function (x) { return x.id === id; })[0]; }
  function schemeName(id) { var s = scheme(id); return s ? s.name : id; }
  function schemeUrl(id) { var s = scheme(id); return s ? s.portal_url : null; }
  function villageText(c, cap) {
    var v = c.villages || [];
    if (!v.length) return null;
    if (!cap || v.length <= cap) return v.join(', ');
    return v.slice(0, cap).join(', ') + ' and ' + plural(v.length - cap, 'more village', 'more villages');
  }
  function placeLine(c, cap) {
    return [villageText(c, cap), c.block ? c.block + ' block' : null, c.district ? c.district + ' district' : null, c.state].filter(Boolean).join(', ');
  }

  /* The only place that decides which photo URL a case shows. */
  function photoFor(c, size) {
    if (!c || !c.photo) return { state: 'none' };
    if (!S.links) return { state: 'ok', src: c.photo, full: c.photo };
    var e = S.links[shaOf(c)];
    var full = e && e.full, src = e && (e[size] || e.full);
    return src ? { state: 'ok', src: src, full: full || src } : { state: 'withheld' };
  }
  function shaOf(c) { return String(c.photo).split('/').pop().replace(/\.[^.]*$/, '').toLowerCase(); }
  function photoNote(state) {
    return state === 'withheld' ? 'Photo withheld' : state === 'broken' ? 'Photo could not be loaded' : 'No photo for this kind of check';
  }

  function findKey(obj, name) {
    var n = norm(name);
    for (var k in obj) if (norm(k) === n) return k;
    return null;
  }
  function areaState(st) { var k = findKey(S.areas.states || {}, st); return k ? S.areas.states[k] : null; }
  function areaDistrict(st, d) {
    var a = areaState(st); if (!a || !a.districts) return null;
    var k = findKey(a.districts, d); return k ? a.districts[k] : null;
  }
  function coverageFor(st, d) {
    return (S.data.coverage || []).filter(function (c) {
      return (st == null || norm(c.state) === norm(st)) && (d == null || norm(c.district) === norm(d));
    });
  }
  function casesFor(st, d) {
    return S.cases.filter(function (c) {
      return (st == null || norm(c.state) === norm(st)) && (d == null || norm(c.district) === norm(d));
    });
  }
  function status(st, d) {
    var a = d == null ? areaState(st) : areaDistrict(st, d);
    var n = a ? a.cases || 0 : 0;
    if (n > 0) {
      var band = BANDS[0];
      BANDS.forEach(function (b) { if (n >= b.min) band = b; });
      return { cls: band.cls, n: n, text: plural(n, 'case', 'cases') };
    }
    if (coverageFor(st, d).length) return { cls: 'f-clear', n: 0, text: 'Partly checked, nothing found' };
    return { cls: 'f-none', n: 0, text: 'Not checked yet' };
  }

  function setTitle(t) { document.title = (t ? t + ' | ' : '') + S.config.site_name; }
  function markNav(name) {
    document.querySelectorAll('.masthead nav a').forEach(function (a) {
      var target = name === 'method' ? '#/method' : name === 'cases' ? '#/cases' : name === 'notfound' ? '' : '#/';
      if (a.getAttribute('href') === target) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
  }

  /* Figures */
  function sumOf(rows, k) {
    var t = 0;
    for (var i = 0; i < rows.length; i++) { if (rows[i][k] == null) return null; t += rows[i][k]; }
    return t;
  }
  function figures(items) {
    return '<ul class="figures">' + items.map(function (f) {
      var missing = f.n == null;
      return '<li' + (f.hot ? ' class="hot"' : '') + '><strong' + (missing ? ' class="nr"' : '') + '>' + esc(missing ? 'Not recorded' : fmt(f.n)) + '</strong><span>' + esc(f.label) + '</span></li>';
    }).join('') + '</ul>';
  }
  function coverageItems(cov) {
    var blocks = {};
    cov.forEach(function (c) { blocks[norm(c.state) + '|' + norm(c.district) + '|' + norm(c.block)] = 1; });
    return [
      { n: sumOf(cov, 'villages_checked'), label: 'villages checked' },
      { n: sumOf(cov, 'records_checked'), label: 'records checked' },
      { n: sumOf(cov, 'photos_checked'), label: 'photos checked' },
      { n: Object.keys(blocks).length, label: Object.keys(blocks).length === 1 ? 'block covered' : 'blocks covered' }
    ];
  }
  function areaFigures(cases, cov) {
    var v = {};
    cases.forEach(function (c) { (c.villages || []).forEach(function (x) { v[norm(c.block) + '|' + norm(x)] = 1; }); });
    return countFigures(cases.length, Object.keys(v).length, cov);
  }
  function countFigures(total, n, cov) {
    return figures([{ n: total, label: total === 1 ? 'case found' : 'cases found', hot: total > 0 },
      { n: n, label: n === 1 ? 'village with a case' : 'villages with a case', hot: n > 0 }].concat(coverageItems(cov)));
  }

  /* Map */
  function mapSVG(shapes, opts) {
    var b = [Infinity, Infinity, -Infinity, -Infinity];
    Object.keys(shapes).forEach(function (k) {
      var x = shapes[k].bbox;
      b = [Math.min(b[0], x[0]), Math.min(b[1], x[1]), Math.max(b[2], x[2]), Math.max(b[3], x[3])];
    });
    var pad = (b[2] - b[0]) * 0.02, w = b[2] - b[0] + 2 * pad, h = b[3] - b[1] + 2 * pad;
    var fs = Math.max(w, h) / (opts.small ? 26 : 34);
    var paths = '', labels = '';
    Object.keys(shapes).forEach(function (k) {
      var st = opts.status(k);
      var sel = opts.selected && norm(opts.selected) === norm(k);
      var live = st.cls !== 'f-none';
      paths += '<path d="' + shapes[k].d + '" class="' + st.cls + (sel ? ' sel' : '') + '" data-name="' + esc(k) + '" data-status="' + esc(st.text) + '"' +
        (live ? ' tabindex="0" role="link" aria-label="' + esc(k + ': ' + st.text) + '"' : '') + (sel ? ' aria-current="page"' : '') + '></path>';
      if (st.n > 0 && shapes[k].c) {
        var t = fmt(st.n), bw = fs * (0.62 * t.length + 0.9), bh = fs * 1.45;
        var cx = shapes[k].c[0], cy = shapes[k].c[1], bb = shapes[k].bbox, px = cx, py = cy, lead = '';
        if (bb[2] - bb[0] < bw * 1.6 || bb[3] - bb[1] < bh * 2) {
          px = cx - bw * 0.9; py = cy - bh * 1.7;
          lead = '<line class="lead" x1="' + cx + '" y1="' + cy + '" x2="' + px.toFixed(3) + '" y2="' + py.toFixed(3) + '"/><circle class="lead-dot" cx="' + cx + '" cy="' + cy + '" r="' + (fs * 0.22).toFixed(3) + '"/>';
        }
        labels += lead + '<g class="pin" transform="translate(' + px.toFixed(3) + ' ' + py.toFixed(3) + ')"><rect x="' + (-bw / 2).toFixed(3) + '" y="' + (-bh / 2).toFixed(3) + '" width="' + bw.toFixed(3) + '" height="' + bh.toFixed(3) + '" rx="' + (bh / 2).toFixed(3) + '"/>' +
          '<text style="font-size:' + fs.toFixed(3) + 'px" dy=".05em">' + t + '</text></g>';
      }
    });
    var sel = '';
    if (opts.selected) {
      var sk = findKey(shapes, opts.selected);
      if (sk) sel = '<path class="sel-outline" d="' + shapes[sk].d + '"/>';
    }
    return '<svg viewBox="' + [b[0] - pad, b[1] - pad, w, h].map(function (v) { return v.toFixed(3); }).join(' ') +
      '" role="group" aria-label="' + esc(opts.label) + '"><g class="areas">' + paths + '</g>' + sel + '<g aria-hidden="true">' + labels + '</g></svg>';
  }
  function legend() {
    function li(cls, text) { return '<li><span class="sw ' + cls + '" aria-hidden="true"></span>' + esc(text) + '</li>'; }
    return '<div class="legend"><p class="legend-h">Cases found</p><ul aria-label="Map key">' +
      BANDS.map(function (b) { return li(b.cls, b.label); }).join('') + '</ul>' +
      '<ul class="legend-states" aria-label="Areas with no case">' + li('f-clear', 'Partly checked, nothing found') + li('f-none', 'Not checked yet') + '</ul></div>';
  }
  function mapCard(shapes, opts, note) {
    return '<div class="map-card"><h2 class="panel-h">' + esc(opts.title) + '</h2><div class="map-box" id="map">' + mapSVG(shapes, opts) + '<div class="map-tip" id="map-tip" hidden></div></div>' + legend() +
      (note ? '<p class="map-note">' + note + '</p>' : '') + '</div>';
  }
  function bindMap(box, onPick) {
    var tip = document.getElementById('map-tip');
    function show(p, x, y) {
      tip.innerHTML = '<strong>' + esc(p.getAttribute('data-name')) + '</strong><span>' + esc(p.getAttribute('data-status')) + '</span>';
      tip.hidden = false;
      var bw = box.clientWidth, tw = tip.offsetWidth;
      tip.style.left = Math.max(4, Math.min(bw - tw - 4, x - tw / 2)) + 'px';
      tip.style.top = Math.max(4, y - tip.offsetHeight - 14) + 'px';
    }
    function hide() { tip.hidden = true; }
    box.addEventListener('click', function (e) {
      var p = e.target.closest('path[data-name]');
      if (p) onPick(p.getAttribute('data-name'));
    });
    box.addEventListener('keydown', function (e) {
      var p = e.target.closest && e.target.closest('path[data-name]');
      if (p && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); onPick(p.getAttribute('data-name')); }
    });
    box.addEventListener('mousemove', function (e) {
      var p = e.target.closest('path[data-name]');
      if (!p) { hide(); return; }
      var r = box.getBoundingClientRect();
      show(p, e.clientX - r.left, e.clientY - r.top);
    });
    box.addEventListener('mouseleave', hide);
    box.addEventListener('focusin', function (e) {
      var p = e.target.closest && e.target.closest('path[data-name]');
      if (!p) return;
      var r = box.getBoundingClientRect(), pr = p.getBoundingClientRect();
      show(p, pr.left + pr.width / 2 - r.left, pr.top - r.top + 6);
    });
    box.addEventListener('focusout', hide);
  }
  function loadDistricts(st) {
    var key = findKey(S.distIndex, st);
    if (!key) return Promise.resolve(null);
    if (S.distGeo[key]) return Promise.resolve(S.distGeo[key]);
    return getJSON('vendor/geo/' + S.distIndex[key]).then(function (g) { S.distGeo[key] = g; return g; });
  }

  /* Shared list parts */
  function pager(pg, path, query, label) {
    if (pg.pages < 2) return '';
    function href(p) { return '#' + L.buildHash(path, L.withQuery(query, { page: p })); }
    var prev = pg.page > 1 ? '<a class="pg-step" rel="prev" href="' + href(pg.page - 1) + '">Previous</a>' : '<span class="pg-step pg-off">Previous</span>';
    var next = pg.page < pg.pages ? '<a class="pg-step" rel="next" href="' + href(pg.page + 1) + '">Next</a>' : '<span class="pg-step pg-off">Next</span>';
    var nums = L.pageList(pg.page, pg.pages, 1).map(function (n) {
      if (n === null) return '<li class="pg-gap" aria-hidden="true">...</li>';
      return '<li><a href="' + href(n) + '" aria-label="Page ' + n + '"' + (n === pg.page ? ' aria-current="page"' : '') + '>' + n + '</a></li>';
    }).join('');
    return '<nav class="pager" aria-label="' + esc(label) + '">' + prev + '<ul>' + nums + '</ul>' + next + '</nav>';
  }
  function resultsLine(pg, one, many) {
    var text = pg.total === 0 ? 'No ' + many + ' to show' :
      pg.total === 1 ? 'Showing 1 ' + one :
      pg.pages < 2 ? 'Showing all ' + plural(pg.total, one, many) :
      'Showing ' + fmt(pg.start + 1) + ' to ' + fmt(pg.end) + ' of ' + plural(pg.total, one, many);
    return '<p class="results" id="list-top" tabindex="-1">' + esc(text) + (pg.pages > 1 ? '<span class="pg-of">, page ' + pg.page + ' of ' + pg.pages + '</span>' : '') + '</p>';
  }
  function fixPage(pg, path, query) {
    if (pg.corrected) history.replaceState(null, '', '#' + L.buildHash(path, L.withQuery(query, { page: pg.page })));
  }
  function selectField(f, query, opts) {
    if (opts.length < 2 && !query[f.key]) return '';
    var cur = query[f.key] || '';
    var known = !cur || opts.some(function (o) { return L.same(o.value, cur); });
    var options = '<option value="">' + esc(f.all) + '</option>' +
      (known ? '' : '<option value="' + esc(cur) + '" selected>' + esc(cur) + ' (0)</option>') +
      opts.map(function (o) {
        return '<option value="' + esc(o.value) + '"' + (L.same(o.value, cur) ? ' selected' : '') + '>' + esc(f.name ? f.name(o.value) : o.value) + ' (' + fmt(o.n) + ')</option>';
      }).join('');
    return '<div class="field"><label for="f-' + f.key + '">' + esc(f.label) + '</label><select id="f-' + f.key + '" data-key="' + f.key + '">' + options + '</select></div>';
  }
  function bindFilters(path, query) {
    document.querySelectorAll('.filters select').forEach(function (sel) {
      sel.addEventListener('change', function () {
        var ch = { page: null }; ch[sel.getAttribute('data-key')] = sel.value;
        S.keepFocus = sel.id;
        location.hash = L.buildHash(path, L.withQuery(query, ch));
      });
    });
  }

  function recordIds(c) {
    var ids = (c.records || []).map(function (r) { return r.record_id; });
    var more = (c.record_count || ids.length) - Math.min(ids.length, 3);
    return (ids.length === 1 ? 'Record ' : 'Records ') + ids.slice(0, 3).join(', ') + (more > 0 ? ' and ' + more + ' more' : '');
  }
  // Google Drive refuses image requests that carry a Referer header.
  function photoImg(src, alt, extra) {
    return '<img class="ph" src="' + esc(src) + '" alt="' + esc(alt) + '" referrerpolicy="no-referrer" decoding="async"' + (extra || '') + '>';
  }
  function placeholderText(c) {
    var ph = c.placeholder;
    if (!ph) return '';
    return 'Not a photo: ' + (ph.width ? 'a ' + ph.width + ' x ' + ph.height + ' pixel image' : 'a small image') + ' such as a broken-image icon or a plain graphic';
  }
  function thumb(c, eager) {
    if (c.photo && S.links && !S.linkDone[shaOf(c).slice(0, 2)]) {
      // Filled in by fillThumbs once data/links/ for this photo has arrived; the box keeps its size.
      S.waiting[c.id] = [c, eager];
      return '<span class="thumb is-loading" data-case="' + esc(c.id) + '"></span>';
    }
    var p = photoFor(c, 'thumb');
    if (p.state === 'ok' && c.placeholder) return '<span class="thumb is-loading is-placeholder">' + photoImg(p.src, '', ' loading="' + (eager ? 'eager' : 'lazy') + '" width="400" height="300"') + '<span class="ph-label">' + esc(placeholderText(c)) + '</span></span>';
    if (p.state === 'ok') return '<span class="thumb is-loading">' + photoImg(p.src, '', ' loading="' + (eager ? 'eager' : 'lazy') + '" width="400" height="300"') + '</span>';
    return '<span class="thumb thumb-empty"><span>' + esc(photoNote(p.state)) + '</span></span>';
  }
  function caseCard(c, hl, i) {
    var h = 'h' + (hl || 3);
    return '<li class="card">' + thumb(c, i < 6) + '<div class="card-body">' +
      '<p class="card-tags"><span class="kind-tag">' + esc(kindTag(c.kind)) + '</span>' + (c.needs_human_review ? '<span class="review-tag">Awaiting human review</span>' : '') + '</p>' +
      '<' + h + ' class="card-h"><a href="' + caseHref(c.id) + '">' + esc(c.headline) + '</a></' + h + '>' +
      '<p class="card-count"><strong>' + fmt(c.record_count) + '</strong> ' + (c.record_count === 1 ? 'record' : 'records') + (L.fileCount(c) > 1 ? ', saved as ' + L.fileCount(c) + ' different files' : '') + '</p>' +
      '<p class="card-place">' + esc(placeLine(c, 3)) + '</p>' +
      '<p class="card-meta">' + esc(recordIds(c)) + ((c.offices || []).length ? '. ' + esc(c.offices.join(', ')) : '') + '</p></div></li>';
  }

  /* counts: [{scheme, kind, n}] from summary.json, used in place of `all` on the home page. */
  function summaryBlock(all, path, query, active, level, counts) {
    var linkOnly = !active; active = active || {};
    var hn = 'h' + (level || 2);
    var base = counts ? null : L.filterItems(all, L.withQuery(active, { scheme: null, kind: null }));
    var groups = counts ? L.kindGroups(counts) : L.kindSummary(base), max = 1;
    var total = base ? base.length : groups.reduce(function (t, g) { return t + g.n; }, 0);
    groups.forEach(function (g) { g.kinds.forEach(function (k) { max = Math.max(max, k.n); }); });
    function link(changes, inner, on, cls) {
      return '<a class="' + cls + '" href="#' + L.buildHash(path, L.withQuery(query, L.withQuery(changes, { page: null }))) + '"' + (on && !linkOnly ? ' aria-current="true"' : '') + '>' + inner + '</a>';
    }
    var allOn = !active.scheme && !active.kind;
    var multi = groups.length > 1;
    var html = '<section class="summary" aria-labelledby="sum-h"><' + hn + ' id="sum-h" class="panel-h">What was found</' + hn + '>' +
      link({ scheme: null, kind: null }, '<span>All problems</span><span class="n">' + fmt(total) + '</span>', allOn, 'sum-all');
    groups.forEach(function (g) {
      var sOn = L.same(active.scheme, g.scheme) && !active.kind;
      html += (multi ? link({ scheme: g.scheme, kind: null }, '<span>' + esc(schemeName(g.scheme)) + '</span><span class="n">' + fmt(g.n) + '</span>', sOn, 'sum-scheme') : '<p class="sum-scheme-name">' + esc(schemeName(g.scheme)) + '</p>') +
        '<ul class="kinds">' + g.kinds.map(function (k) {
          var on = L.same(active.kind, k.kind) && (!active.scheme || L.same(active.scheme, g.scheme));
          return '<li>' + link({ scheme: g.scheme, kind: k.kind },
            '<span class="k-row"><span class="k-name">' + esc(kindLabel(k.kind)) + '</span><span class="n">' + fmt(k.n) + '</span></span>' +
            '<span class="k-bar" aria-hidden="true"><span style="width:' + Math.max(2, 100 * k.n / max).toFixed(1) + '%"></span></span>', on, 'k-link') + '</li>';
        }).join('') + '</ul>';
    });
    if (!groups.length) html += '<p class="muted">No cases in this selection.</p>';
    return html + '</section>';
  }

  /* fields: which narrowing selects this list offers */
  function caseList(all, query, fields) {
    var keys = ['scheme', 'kind'].concat(fields.map(function (f) { return f.key; }));
    var active = {};
    keys.forEach(function (k) { if (query[k]) active[k] = query[k]; });
    var sort = query.sort === 'village' ? 'village' : 'size';
    var list = L.sortCases(L.filterItems(all, active), sort);
    return { active: active, sort: sort, list: list, pg: L.paginate(list.length, query.page, CASES_PER_PAGE) };
  }
  function caseListSection(all, path, query, fields, place, wide, cl) {
    var active = cl.active, sort = cl.sort, list = cl.list, pg = cl.pg;
    fixPage(pg, path, query);
    var filtered = Object.keys(active).length > 0;
    var selects = fields.map(function (f) { return selectField(f, query, L.facet(all, active, f.key)); }).join('') +
      '<div class="field"><label for="f-sort">Order</label><select id="f-sort" data-key="sort">' +
      '<option value="size"' + (sort === 'size' ? ' selected' : '') + '>Largest first</option>' +
      '<option value="village"' + (sort === 'village' ? ' selected' : '') + '>Village name, A to Z</option></select></div>';
    var activeText = Object.keys(active).map(function (k) {
      if (k === 'scheme') return 'scheme ' + schemeName(active[k]);
      if (k === 'kind') return 'problem "' + kindLabel(active[k]) + '"';
      return (k === 'office' ? 'office' : k) + ' ' + active[k];
    }).join('; ');
    var kindNote = active.kind && KINDS[active.kind] ? '<p class="kind-note">' + esc(KINDS[active.kind].short) + '</p>' : '';
    var body = list.length
      ? '<ul class="cards">' + list.slice(pg.start, pg.end).map(function (c, i) { return caseCard(c, 3, i); }).join('') + '</ul>' + pager(pg, path, query, 'Pages of cases')
      : '<div class="empty"><p><strong>No cases match this selection.</strong> ' + (filtered ? 'Filters in use: ' + esc(activeText) + '.' : '') + '</p>' +
        (filtered ? '<p><a class="btn" href="#' + L.buildHash(path, {}) + '">Clear all filters</a></p>' : '') +
        '<p>Remove a filter, or pick another problem in "What was found", which shows the counts for the rest of this selection.</p></div>';
    return '<div class="list-layout' + (wide ? ' wide' : '') + '"><div class="list-side">' + summaryBlock(all, path, query, active) + '</div>' +
      '<section class="cases" aria-labelledby="cases-h"><h2 id="cases-h">Cases in ' + esc(place) + '</h2>' +
      '<form class="filters" aria-label="Narrow the cases" onsubmit="return false">' + selects + '</form>' +
      (filtered ? '<p class="active-filters">Narrowed to: ' + esc(activeText) + '. <a href="#' + L.buildHash(path, sort === 'size' ? {} : { sort: sort }) + '">Clear filters</a></p>' : '') + kindNote +
      resultsLine(pg, 'case', 'cases') + body + '</section></div>';
  }

  function when(iso) {
    var d = new Date(iso);
    if (!iso || isNaN(d)) return 'not recorded';
    return d.toLocaleString('en-IN', { day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' }) + ' India time';
  }
  function emptyText(st, d) {
    var s = status(st, d), place = d || st;
    if (s.cls === 'f-none') return '<div class="empty"><p><strong>Not checked yet.</strong> We have not collected any records for ' + esc(place) + ', so nothing has been checked here.</p></div>';
    var cov = coverageFor(st, d);
    return '<div class="empty"><p><strong>Nothing flagged.</strong> Records were collected and checked for ' + esc(plural(cov.length, 'block', 'blocks')) + ' in ' + esc(place) +
      ' (listed on <a href="#/method">About the method</a>), and nothing was flagged. Other parts of ' + esc(place) + ' have not been checked.</p></div>';
  }

  function finder(items, labelText) {
    var checked = items.filter(function (it) { return it.st.cls !== 'f-none'; });
    var rest = items.filter(function (it) { return it.st.cls === 'f-none'; });
    return '<section class="finder" aria-labelledby="finder-h"><div class="finder-head"><h2 id="finder-h">' + esc(labelText) + '</h2>' +
      '<div class="finder-q"><label for="finder-q">Filter by name</label><input id="finder-q" type="search" autocomplete="off"></div></div>' +
      (checked.length ? '<ul class="area-list finder-items">' + checked.map(function (it) {
        return '<li data-q="' + esc(norm(it.name)) + '"><a href="' + it.href + '"><span class="sw ' + it.st.cls + '" aria-hidden="true"></span><span class="a-name">' + esc(it.name) + '</span>' +
          '<span class="tag' + (it.st.n ? ' has' : '') + '">' + esc(it.st.text) + '</span></a></li>';
      }).join('') + '</ul>' : '') +
      (rest.length ? '<details class="unchecked" id="finder-rest"' + (checked.length ? '' : ' open') + '><summary>Not checked yet: ' + esc(fmt(rest.length)) + '</summary>' +
        '<ul class="plain-list finder-items">' + rest.map(function (it) {
          return '<li data-q="' + esc(norm(it.name)) + '"><a href="' + it.href + '">' + esc(it.name) + '</a></li>';
        }).join('') + '</ul></details>' : '') +
      '<p class="status" id="finder-empty" hidden>No area matches that name.</p></section>';
  }
  function bindFinder() {
    var q = document.getElementById('finder-q');
    if (!q) return;
    var rest = document.getElementById('finder-rest'), wasOpen = rest && rest.open;
    q.addEventListener('input', function () {
      var n = norm(q.value), shown = 0;
      document.querySelectorAll('.finder-items li').forEach(function (li) {
        var ok = li.getAttribute('data-q').indexOf(n) !== -1;
        li.hidden = !ok; if (ok) shown++;
      });
      if (rest) rest.open = n ? true : wasOpen;
      document.getElementById('finder-empty').hidden = shown > 0;
    });
  }
  function byStatus(items) {
    return items.sort(function (a, b) {
      var ra = a.st.n ? 0 : a.st.cls === 'f-clear' ? 1 : 2, rb = b.st.n ? 0 : b.st.cls === 'f-clear' ? 1 : 2;
      return ra - rb || b.st.n - a.st.n || a.name.localeCompare(b.name);
    });
  }

  function allStates() {
    var names = {};
    Object.keys(S.geo || {}).forEach(function (k) { names[norm(k)] = k; });
    Object.keys(S.areas.states || {}).forEach(function (k) { if (!names[norm(k)]) names[norm(k)] = k; });
    (S.data.coverage || []).forEach(function (c) { if (c.state && !names[norm(c.state)]) names[norm(c.state)] = c.state; });
    (S.data.index || []).forEach(function (e) { if (e.state && !names[norm(e.state)]) names[norm(e.state)] = e.state; });
    return Object.keys(names).map(function (k) { return names[k]; }).sort();
  }
  function allDistricts(st, shapes) {
    var names = {};
    Object.keys(shapes || {}).forEach(function (k) { names[norm(k)] = k; });
    var a = areaState(st);
    Object.keys((a && a.districts) || {}).forEach(function (k) { names[norm(k)] = k; });
    coverageFor(st).forEach(function (c) { if (c.district) names[norm(c.district)] = c.district; });
    casesFor(st).forEach(function (c) { if (c.district) names[norm(c.district)] = c.district; });
    return Object.keys(names).map(function (k) { return names[k]; }).sort();
  }

  function pageHead(crumbs, title, dek, figs) {
    return '<header class="page-head">' + (crumbs || '') + '<h1>' + title + '</h1>' + (dek ? '<p class="dek">' + dek + '</p>' : '') + (figs || '') + '</header>';
  }
  function crumbs(parts) {
    return '<nav class="crumbs" aria-label="Breadcrumb"><ol>' + parts.map(function (p, i) {
      return '<li>' + (p.href && i < parts.length - 1 ? '<a href="' + p.href + '">' + esc(p.name) + '</a>' : '<span aria-current="page">' + esc(p.name) + '</span>') + '</li>';
    }).join('') + '</ol></nav>';
  }

  function renderNotFound(what) {
    setTitle('Page not found');
    view.innerHTML = '<div class="state-box"><h1>Page not found</h1><p>' + esc(what || 'This address does not match any page on this site.') + ' The link may be mistyped or out of date.</p>' +
      '<p><a class="btn btn-primary" href="#/">Go to the map of India</a></p></div>';
  }

  function topCases() { return L.sortCases(S.data.top || [], 'size').slice(0, TOP_CASES); }
  function renderHome() {
    setTitle('');
    var total = S.data.total || 0, cov = S.data.coverage || [];
    var mapHtml = S.geo
      ? mapCard(S.geo, { title: 'Cases found, by state', status: function (k) { return status(k); }, label: 'Map of India by state. Marked states can be opened from the map; every state is also in the list below.' },
        'Select a marked state, or use the list of states below the map.')
      : '<p class="error">The map could not be loaded. The list below shows every state; only those with collected records or cases have been checked.</p>';
    var side = '';
    if (total) {
      side += '<div class="panel">' + summaryBlock(null, '/cases', {}, null, 2, S.data.kinds || []) + '</div>' +
        '<section class="panel" aria-labelledby="top-h"><h2 id="top-h" class="panel-h">Largest cases</h2><ul class="cards compact">' + topCases().map(function (c, i) { return caseCard(c, 3, i); }).join('') + '</ul>' +
        '<p class="panel-foot"><a class="btn btn-primary" href="#/cases">See all ' + esc(plural(total, 'case', 'cases')) + '</a></p></section>';
    } else {
      side += '<div class="panel"><h2 class="panel-h">What was found</h2><p>No cases have been flagged in the areas checked so far. See <a href="#/method">About the method</a> for what has been covered.</p></div>';
    }
    view.innerHTML = '<h1>Evidence photos that need a second look</h1><div class="intro">' +
      '<p class="dek">Public works under government schemes are recorded on official portals with photos as proof. This site checks those records and lists the ones where the evidence looks wrong.</p>' +
      '<p class="notice"><strong>A flag is a lead for checking, not proof of wrongdoing.</strong> Each case lists its record IDs so anyone can check them on the portal.</p></div>' +
      countFigures(total, S.data.villages_with_case || 0, cov) +
      '<div class="home"><div class="home-map">' + mapHtml + '</div><div class="home-side">' + side + '</div><div class="home-finder">' +
      finder(byStatus(allStates().map(function (s) { return { name: s, href: stateHref(s), st: status(s) }; })), 'States and union territories') + '</div></div>';
    if (S.geo) bindMap(document.getElementById('map'), function (n) { location.hash = stateHref(n); });
    bindFinder();
  }

  var ALL_FIELDS = [
    { key: 'state', label: 'State', all: 'All states' },
    { key: 'district', label: 'District', all: 'All districts' },
    { key: 'block', label: 'Block', all: 'All blocks' },
    { key: 'office', label: 'Office that made the entries', all: 'All offices' }
  ];
  function loadAllCases(query) {
    whenReady(loadIndex(indexFor(null)), function () { renderAllCases(query); });
  }
  function renderAllCases(query) {
    setTitle('All cases');
    var all = S.cases;
    view.innerHTML = pageHead(crumbs([{ name: 'All India', href: '#/' }, { name: 'All cases' }]), 'All cases',
      'Every flagged case in the current data, across all areas checked so far.', areaFigures(all, S.data.coverage || [])) +
      caseListSection(all, '/cases', query, ALL_FIELDS, 'all areas', true, caseList(all, query, ALL_FIELDS));
    bindFilters('/cases', query);
  }

  function renderArea(st, district, query) {
    var token = S.token;
    var key = findKey(S.geo || {}, st) || findKey(arrToObj(allStates()), st);
    if (!key) { renderNotFound('There is no state or union territory called "' + st + '" on this site.'); return settle(); }
    view.innerHTML = '<p class="status loading">Loading the district map for ' + esc(key) + '...</p>';
    Promise.all([loadDistricts(key).catch(function () { return null; }), loadIndex(indexFor(key))]).then(function (r) {
      var shapes = r[0];
      if (token !== S.token) return;
      var dKey = null;
      if (district) {
        dKey = findKey(arrToObj(allDistricts(key, shapes)), district);
        if (!dKey) { renderNotFound('There is no district called "' + district + '" in ' + key + ' on this site.'); return settle(); }
      }
      var place = dKey || key;
      var path = dKey ? districtHref(key, dKey).slice(1) : stateHref(key).slice(1);
      setTitle(dKey ? dKey + ', ' + key : key);
      var all = casesFor(key, dKey);
      var cov = coverageFor(key, dKey);
      var trail = [{ name: 'All India', href: '#/' }, { name: key, href: stateHref(key) }];
      if (dKey) trail.push({ name: dKey });
      var mapHtml = shapes
        ? mapCard(shapes, { title: 'Cases found, by district', selected: dKey, small: true, status: function (k) { return status(key, k); }, label: 'Map of ' + key + ' by district. Every district is also in the list below.' },
          'District boundaries follow Census 2011, so districts formed since then are not drawn separately.')
        : '<p class="status">No district map is available for ' + esc(key) + '. Use the list of districts below.</p>';
      var fields = areaFields(dKey);
      var stx = status(key, dKey), unchecked = stx.cls === 'f-none' && !all.length;
      var list = all.length ? caseListSection(all, path, query, fields, place, false, caseList(all, query, fields)) : unchecked ? '' : '<section class="cases"><h2>Cases in ' + esc(place) + '</h2>' + emptyText(key, dKey) + '</section>';
      var sub = stx.n ? esc(stx.text) + ' found in the parts of ' + esc(place) + ' checked so far.' :
        unchecked ? 'Not checked yet. No records have been collected for ' + esc(place) + ', so there are no figures or cases to show. <a href="#/method">About the method</a> lists the areas checked so far.' : esc(stx.text) + '.';
      var finderHtml = finder(byStatus(allDistricts(key, shapes).map(function (d) { return { name: d, href: districtHref(key, d), st: status(key, d) }; })), 'Districts of ' + key);
      view.innerHTML = pageHead(crumbs(trail), esc(place), sub, unchecked ? '' : areaFigures(all, cov)) +
        '<div class="area-grid"><div class="area-map">' + mapHtml + (dKey ? '<p><a class="btn" href="' + stateHref(key) + '">Show all of ' + esc(key) + '</a></p>' : '') + '</div>' +
        '<div class="area-main">' + (unchecked ? finderHtml : list) + '</div></div>' + (unchecked ? '' : finderHtml);
      if (shapes) bindMap(document.getElementById('map'), function (n) { location.hash = districtHref(key, n); });
      bindFilters(path, query);
      bindFinder();
      settle();
    }, function (e) {
      if (token !== S.token) return;
      loadFailed(e);
      settle();
    });
  }
  function areaFields(dKey) {
    var fields = dKey ? [] : [{ key: 'district', label: 'District', all: 'All districts' }];
    return fields.concat({ key: 'block', label: 'Block', all: 'All blocks' }, { key: 'office', label: 'Office that made the entries', all: 'All offices' });
  }
  function arrToObj(a) { var o = {}; a.forEach(function (k) { o[k] = 1; }); return o; }

  function rtiText(c) {
    var ids = (c.records || []).map(function (r) { return r.record_id; });
    var missing = (c.record_count || 0) - ids.length;
    var portal = c.source_url || schemeUrl(c.scheme) || '';
    var lines = [
      'To,',
      'The Public Information Officer,',
      'Office of ' + ((c.offices && c.offices.length) ? c.offices.join(' / ') : '[name of the office]') + ',',
      [c.block ? 'Block ' + c.block : null, c.district ? 'District ' + c.district : null, c.state].filter(Boolean).join(', '),
      '',
      'Subject: Request for information under Section 6(1) of the Right to Information Act, 2005',
      '',
      'Sir or Madam,',
      '',
      'Please provide the following information about works recorded under ' + schemeName(c.scheme) + ' on the public portal ' + portal + ', for each of these record IDs:',
      '',
      ids.join(', ') + (missing > 0 ? ', [add the other ' + missing + ' record IDs from the portal]' : ''),
      '',
      '1. Certified copies of the work file for each record, including the estimate and the technical and administrative sanction.',
      '2. Copies of every photograph uploaded as evidence for each record, with the date of upload and the name and designation of the official who uploaded it.',
      '3. Copies of the measurement book entries and the completion certificate for each record.',
      '4. Details of all expenditure and payments made for each record, with dates, amounts and payees.',
      '5. Copies of any inspection or verification report for these works.',
      '',
      'I am a citizen of India. I enclose the application fee as prescribed under the applicable RTI rules, by [mode of payment]. If any of this information is held by another public authority, please transfer this request under Section 6(3).',
      '',
      'Name: [your name]',
      'Address: [your postal address]',
      'Phone or email: [optional]',
      'Date: [date]',
      'Signature:'
    ];
    return lines.join('\n');
  }

  function recordsSection(c, query) {
    var path = caseHref(c.id).slice(1);
    var recs = L.sortRecords(c.records || []);
    var active = {};
    if (query.village) active.village = query.village;
    if (query.office) active.created_by = query.office;
    var villages = L.facet(recs, active, 'village'), offices = L.facet(recs, active, 'created_by');
    var list = L.filterItems(recs, active);
    var pg = L.paginate(list.length, query.page, RECORDS_PER_PAGE);
    fixPage(pg, path, query);
    var rows = list.slice(pg.start, pg.end).map(function (r) {
      var gps = r.lat != null && r.lon != null ? r.lat + ', ' + r.lon : '';
      return '<tr><td class="num id">' + esc(r.record_id) + '</td><td>' + esc(r.village) + '</td><td>' + esc(r.structure_type) + '</td><td>' + esc(r.created_by) +
        '</td><td class="num">' + esc(r.start_date) + '</td><td class="num">' + esc(r.end_date) + '</td><td class="num">' + esc(gps) + '</td></tr>';
    }).join('');
    var intro = '<p class="muted">' + (recs.length < c.record_count ? esc('This case covers ' + fmt(c.record_count) + ' records. ' + 'The data file lists ' + fmt(recs.length) + ' of them.') : esc(plural(c.record_count, 'record', 'records')) + ' in this case.') +
      ' Sorted by village, then record ID. A blank cell means no value is recorded in this data.</p>';
    var filters = selectField({ key: 'village', label: 'Village', all: 'All villages' }, query, villages) +
      selectField({ key: 'office', label: 'Entered by', all: 'All offices' }, query, offices);
    var narrowed = query.village || query.office;
    var table = list.length
      ? '<div class="table-wrap" tabindex="0" role="region" aria-label="Records in this case"><table><thead><tr><th scope="col">Record ID</th><th scope="col">Village</th><th scope="col">Work type</th><th scope="col">Entered by</th><th scope="col">Start date</th><th scope="col">End date</th><th scope="col">GPS</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
        pager(pg, path, query, 'Pages of records')
      : '<div class="empty"><p><strong>No records match this selection.</strong></p><p><a class="btn" href="#' + path + '">Show all records</a></p></div>';
    return '<section class="records" aria-labelledby="rec-h"><h2 id="rec-h">The records</h2>' + intro +
      (filters ? '<form class="filters" aria-label="Narrow the records" onsubmit="return false">' + filters + '</form>' : '') +
      (narrowed ? '<p class="active-filters"><a href="#' + path + '">Show all records</a></p>' : '') +
      resultsLine(pg, 'record', 'records') + table + '</section>';
  }

  // The JSJB portal has no link to a single record, only five dropdowns and a Search button. This
  // panel lists the values to choose for each village of the case, links to the portal with those
  // values in the address, and offers a bookmark (site/jsjb-fill.js) that fills them in there.
  var PATH_FIELDS = [
    { key: 'state', label: 'State' }, { key: 'district', label: 'District' }, { key: 'area_type', label: 'Area type' },
    { key: 'block', label: 'Block or town' }, { key: 'village', label: 'Village or ward' }];
  var PORTAL_ROWS_SHOWN = 8;

  function portalGroups(c) {
    var groups = {}, order = [];
    (c.records || []).forEach(function (r) {
      var p = { state: r.state || c.state, district: r.district || c.district, area_type: r.area_type || '', block: r.block || c.block, village: r.village };
      var k = PATH_FIELDS.map(function (f) { return p[f.key] || ''; }).join('|');
      if (!groups[k]) { groups[k] = { path: p, ids: [] }; order.push(k); }
      groups[k].ids.push(r.record_id);
    });
    return order.map(function (k) { return groups[k]; }).sort(function (a, b) { return b.ids.length - a.ids.length || String(a.path.village).localeCompare(String(b.path.village)); });
  }

  function fillBookmark() {
    var f = window.YJ_JSJB_FILL;
    return f ? 'javascript:' + encodeURIComponent('(' + f.toString() + ')()') : null;
  }

  function portalFinder(c, portal) {
    if (c.scheme !== 'jsjb' || !portal) return '';
    var groups = portalGroups(c);
    if (!groups.length) return '';
    var shared = PATH_FIELDS.filter(function (f) {
      var v = groups[0].path[f.key];
      return f.key !== 'village' && v && groups.every(function (g) { return g.path[f.key] === v; });
    });
    var cols = PATH_FIELDS.filter(function (f) { return shared.indexOf(f) < 0 && groups.some(function (g) { return g.path[f.key]; }); });
    var noArea = groups.some(function (g) { return !g.path.area_type; });
    var base = portal.split('#')[0];
    var row = function (g) {
      var t = { s: g.path.state, d: g.path.district, a: g.path.area_type || '', b: g.path.block, v: g.path.village, ids: g.ids };
      return '<tr>' + cols.map(function (f) { return '<td>' + esc(g.path[f.key] || '') + '</td>'; }).join('') +
        '<td class="num r">' + fmt(g.ids.length) + '</td>' +
        '<td><a href="' + esc(base + '#yj=' + encodeURIComponent(JSON.stringify(t))) + '" target="_blank" rel="noopener">Open portal<span class="visually-hidden"> for ' + esc(g.path.village) + ' (opens in a new tab)</span></a></td></tr>';
    };
    var head = '<thead><tr>' + cols.map(function (f) { return '<th scope="col">' + f.label + '</th>'; }).join('') +
      '<th scope="col" class="num">Records</th><th scope="col"><span class="visually-hidden">Link to the portal</span></th></tr></thead>';
    var table = function (list, label) {
      return '<div class="table-wrap" tabindex="0" role="region" aria-label="' + label + '"><table>' + head + '<tbody>' + list.map(row).join('') + '</tbody></table></div>';
    };
    var first = groups.slice(0, PORTAL_ROWS_SHOWN), rest = groups.slice(PORTAL_ROWS_SHOWN);
    var bm = fillBookmark();
    return '<section class="portal-find" aria-labelledby="pf-h"><h2 id="pf-h">Find these records on the portal</h2>' +
      '<p>The portal has no link to a single record. On its page, choose these values in the filters, press Search, and look for the record IDs in the first column, written as <span class="nowrap">"1 (5410072)"</span>.</p>' +
      (shared.length ? '<dl class="path">' + shared.map(function (f) { return '<div><dt>' + f.label + '</dt><dd>' + esc(groups[0].path[f.key]) + '</dd></div>'; }).join('') + '</dl>' : '') +
      (noArea ? '<p class="muted small">This data does not say whether a place is rural or urban. Try Rural first; towns and wards are under Urban.</p>' : '') +
      table(first, 'Where to find the records') +
      (rest.length ? '<details class="more-places"><summary>Show the other ' + plural(rest.length, 'place', 'places') + '</summary>' + table(rest, 'More places with records of this case') + '</details>' : '') +
      (bm ? '<div class="fill-help"><h3>Let your browser fill the filters</h3>' +
        '<p>Drag this button to your browser\'s bookmarks bar once:</p>' +
        '<p><a class="btn" id="fill-bm" href="' + esc(bm) + '">Fill JSJB filters</a> <span class="copied" id="fill-msg" role="status"></span></p>' +
        '<ol class="steps"><li>Open a place from the table above. The portal opens in a new tab.</li>' +
        '<li>Click the bookmark. It chooses the five filters and runs the search.</li>' +
        '<li>Click it again on the results page to mark this case\'s records in blue. The portal shows 100 results per page, so click it again on each page you open.</li></ol>' +
        '<p class="muted small">On a phone, choose the values by hand.</p></div>' : '') +
      '</section>';
  }

  function bindFillBookmark() {
    var a = document.getElementById('fill-bm');
    if (!a) return;
    a.addEventListener('click', function (e) {
      e.preventDefault();
      document.getElementById('fill-msg').textContent = 'This button works from the bookmarks bar on the portal page. Drag it there first.';
    });
  }

  /* Text for the method page when files were also compared by picture, built from the limits in summary.json. */
  function nearMatchText() {
    var m = L.nearMatch(S.data.schemes);
    if (!m) return '';
    return ' For ' + m.schemes.join(', ') + ', the check also compares the pictures, so one picture saved again as a different file counts as the same photo. That covers copies that were re-compressed or resized, cut by a few pixels, given another date or GPS stamp, or shown inside the black or white bars of a phone screenshot (the bars are cut off before the comparison). Two files count as one picture only when three tests all pass: their picture fingerprints differ in at most ' +
      m.distance + ' of 256 bits, small greyscale copies of the two are almost identical pixel by pixel, and their width-to-height ratios match. Nearly blank or very small images are never matched this way, and broken-image icons and plain graphics are not shown as reused photos. The limits were set below what separated the closest pairs of different photos when the test was tried on the collected photos. No pair of different photos passed both tests, and every pair of files counted as one picture is listed for a person to check. Where the tests are in doubt the files stay apart, so some copies are missed, for example one cut down by more than a few pixels.';
  }

  // A case page loads only data/cases/<id>.json, the whole case with all its records.
  function loadCase(id, query) {
    var moved = null;
    var work = S.full[id] ? Promise.resolve() : getJSON('data/cases/' + L.caseFileName(id)).then(function (c) {
      if (!c || !Array.isArray(c.records)) throw new Error('the file of this case is not valid');
      if (c.id === id) S.full[id] = c;
    }, function (e) {
      if (e.status !== 404) throw e;
      // No file under this id: the case may have taken over another one's files and kept its own id.
      return loadIndex(indexFor(null)).then(function () {
        var found = L.findCase(S.cases, id);
        if (found && found.moved) moved = found.c.id;
      });
    });
    whenReady(work.then(function () { return S.full[id] ? loadLinks([S.full[id]]) : null; }), function () {
      if (moved) { location.replace(caseHref(moved)); return; }
      renderCase(id, query);
    }, 'Loading the records of this case...');
  }

  function renderCase(id, query) {
    var c = S.full[id];
    if (!c) {
      setTitle('Case not found');
      view.innerHTML = '<div class="state-box"><h1>Case not found</h1><p>There is no case with this address in the current data. It may have been removed after a recheck.</p><p><a class="btn btn-primary" href="#/">Go to the map of India</a></p></div>';
      return;
    }
    setTitle(c.headline);
    var p = photoFor(c, 'full');
    var files = L.fileCount(c);
    var alt = 'The evidence photo attached to ' + plural(c.record_count, 'record', 'records') + ' in this case';
    var filesNote = files > 1 ? ' The same picture was attached as ' + files + ' different files. This is one of them; the list under "What looks wrong" names every file and how many works it is on.' : '';
    var phNote = c.placeholder ? ' ' + placeholderText(c) + '. The portal shows this same image as the evidence photo for every record in this case.' : '';
    var photo = p.state === 'ok'
      ? '<figure class="case-photo"><a class="photo-frame' + (c.placeholder ? ' is-placeholder' : '') + '" href="' + esc(p.full) + '" target="_blank" rel="noopener noreferrer">' + photoImg(p.src, alt) + '</a>' +
        '<figcaption>Photo as published on the official portal. Select it to open the full size file in a new tab.' + esc(phNote) + esc(filesNote) + '</figcaption></figure>'
      : '<div class="case-photo"><div class="photo-frame thumb-empty"><span>' + esc(photoNote(p.state)) + '</span></div>' +
        '<p class="figcap">' + (p.state === 'withheld' ? 'This photo is not published on this site. Open any record on the official portal to see it.' : 'No copy of a photo is stored with this case. Open any record on the official portal to see it.') + '</p></div>';
    var portal = c.source_url || schemeUrl(c.scheme);
    var villages = (c.villages || []).length, offices = (c.offices || []).length;
    var trail = [{ name: 'All India', href: '#/' }, { name: c.state, href: stateHref(c.state) }];
    if (c.district) trail.push({ name: c.district, href: districtHref(c.state, c.district) });
    trail.push({ name: 'Case' });
    view.innerHTML = crumbs(trail) +
      '<div class="case-top">' +
        '<header class="case-head"><p class="card-tags"><a class="kind-tag" href="#' + L.buildHash('/cases', { scheme: c.scheme, kind: c.kind }) + '">' + esc(kindLabel(c.kind)) + '</a>' +
          (c.needs_human_review ? '<span class="review-tag">Awaiting human review</span>' : '') + '</p>' +
          '<h1>' + esc(c.headline) + '</h1><p class="dek">' + esc(placeLine(c, 6)) + '. ' + esc(schemeName(c.scheme)) + '.</p>' +
          figures([{ n: c.record_count, label: c.record_count === 1 ? 'record in this case' : 'records in this case', hot: true },
            { n: villages, label: villages === 1 ? 'village' : 'villages' }, { n: offices, label: offices === 1 ? 'office made the entries' : 'offices made the entries' }]) + '</header>' +
        photo +
        '<div class="case-side"><p class="notice"><strong>This flag is a lead for checking, not proof of wrongdoing.</strong> There may be an innocent explanation, such as a data entry error.' +
          (c.needs_human_review ? ' This case comes from an automated image check and has not yet been reviewed by a person.' : '') + '</p>' +
          '<section class="wrong" aria-labelledby="wrong-h"><h2 id="wrong-h">What looks wrong</h2><ol class="reasons">' + (c.reasons || []).map(function (r) { return '<li>' + esc(r) + '</li>'; }).join('') + '</ol>' +
        (offices ? '<p class="offices"><span>Offices that entered the records</span> ' + esc(c.offices.join(', ')) + '</p>' : '') + '</section>' +
          '<h2 class="panel-h">Check it yourself</h2>' +
          '<div class="actions"><button type="button" class="btn btn-primary" id="rti-btn" aria-expanded="false" aria-controls="rti-box">Write RTI request draft</button>' +
          (portal ? '<a class="btn" href="' + esc(portal) + '" target="_blank" rel="noopener">Open the official portal<span class="visually-hidden"> (opens in a new tab)</span></a>' : '') + '</div>' +
          '<p class="muted small">A request under the Right to Information Act can get the official file for each work. The draft is built from this case; fill in the parts in square brackets before sending.' +
          (portal && c.scheme === 'jsjb' ? ' To reach the records there, use the filter values under "Find these records on the portal" below.' : portal ? ' The portal has no link to single records: choose the state, district, block and village there, then look for the record IDs below.' : '') + '</p>' +
          '<div id="rti-box" hidden><label for="rti-text" class="visually-hidden">RTI request draft</label><textarea id="rti-text" spellcheck="false"></textarea>' +
          '<div class="actions"><button type="button" class="btn" id="rti-copy">Copy text</button><span class="copied" id="rti-msg" role="status"></span></div></div></div>' +
      '</div>' +
      portalFinder(c, portal) +
      recordsSection(c, query);
    bindFilters(caseHref(c.id).slice(1), query);
    bindFillBookmark();
    var btn = document.getElementById('rti-btn'), box = document.getElementById('rti-box'), ta = document.getElementById('rti-text');
    btn.addEventListener('click', function () {
      var open = box.hidden;
      if (open && !ta.value) ta.value = rtiText(c);
      box.hidden = !open;
      btn.setAttribute('aria-expanded', String(open));
      btn.textContent = open ? 'Hide RTI request draft' : 'Write RTI request draft';
      if (open) ta.focus();
    });
    document.getElementById('rti-copy').addEventListener('click', function () {
      var msg = document.getElementById('rti-msg');
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) {}
      if (ok) { msg.textContent = 'Copied.'; return; }
      msg.textContent = 'Select the text and copy it.';
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(ta.value).then(function () { msg.textContent = 'Copied.'; }, function () {});
      }
    });
  }

  function coverageSection(query) {
    var cov = (S.data.coverage || []).slice().sort(function (a, b) {
      return [a.scheme, a.state, a.district, a.block].join('|').localeCompare([b.scheme, b.state, b.district, b.block].join('|'));
    });
    if (!cov.length) return '<div class="empty"><p>No areas have been checked yet.</p></div>';
    var active = {};
    ['scheme', 'state', 'district'].forEach(function (k) { if (query[k]) active[k] = query[k]; });
    var list = L.filterItems(cov, active);
    var pg = L.paginate(list.length, query.page, COVERAGE_PER_PAGE);
    fixPage(pg, '/method', query);
    var rows = list.slice(pg.start, pg.end).map(function (c) {
      return '<tr><td>' + esc(schemeName(c.scheme)) + '</td><td>' + esc(c.state) + '</td><td>' + esc(c.district) + '</td><td>' + esc(c.block) +
        '</td><td class="num r">' + fmt(c.villages_checked) + '</td><td class="num r">' + fmt(c.records_checked) + '</td><td class="num r">' + fmt(c.photos_checked) + '</td></tr>';
    }).join('');
    var filters = selectField({ key: 'scheme', label: 'Scheme', all: 'All schemes', name: schemeName }, query, L.facet(cov, active, 'scheme')) +
      selectField({ key: 'state', label: 'State', all: 'All states' }, query, L.facet(cov, active, 'state')) +
      selectField({ key: 'district', label: 'District', all: 'All districts' }, query, L.facet(cov, active, 'district'));
    return figures(coverageItems(list)) +
      (filters ? '<form class="filters" aria-label="Narrow the coverage table" onsubmit="return false">' + filters + '</form>' : '') +
      resultsLine(pg, 'block', 'blocks') +
      (list.length
        ? '<div class="table-wrap" tabindex="0" role="region" aria-label="Coverage by block"><table><thead><tr><th scope="col">Scheme</th><th scope="col">State</th><th scope="col">District</th><th scope="col">Block</th><th scope="col" class="num">Villages</th><th scope="col" class="num">Records</th><th scope="col" class="num">Photos</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
          pager(pg, '/method', query, 'Pages of the coverage table')
        : '<div class="empty"><p><strong>No checked blocks match this selection.</strong></p><p><a class="btn" href="#/method">Show all checked blocks</a></p></div>');
  }

  function renderMethod(query) {
    setTitle('About the method');
    var used = {};
    (S.data.kinds || []).forEach(function (k) { used[k.kind] = (used[k.kind] || 0) + k.n; });
    var name = S.config.site_name, hi = S.config.site_name_hi;
    view.innerHTML = '<div class="prose">' + pageHead('', 'About the method', 'Every work under these schemes is published on an official portal with location, dates, the office that entered it and one or more photos as proof. We copy those public records and run simple checks on them.') +
      '<p>' + esc(name) + (hi ? ' (<span lang="hi">' + esc(hi) + '</span>)' : '') + ' means "scheme check" in Hindi. ' + esc(S.config.tagline || '') + (S.config.tagline ? '.' : '') + '</p>' +
      '<p class="notice"><strong>A flag is a lead for checking, not proof of wrongdoing.</strong> Each case page lists the record IDs so anyone can open the same records on the portal.</p>' +
      '<h2>The checks</h2><ul class="checks">' + Object.keys(KINDS).map(function (k) {
        var ran = !S.data.checks_run || S.data.checks_run.indexOf(k) !== -1;
        var count = used[k] || 0;
        return '<li><h3>' + esc(KINDS[k].label) + '</h3><p>' + esc(KINDS[k].text + (k === 'photo_reuse' ? nearMatchText() : '')) + '</p><p class="check-n">' +
          (ran ? 'Cases of this kind in the current data: ' + (count ? '<a class="tap" href="#' + L.buildHash('/cases', { kind: k }) + '">' + fmt(count) + '</a>' : '0') + '.' : 'This check was not run for the current data.') + '</p></li>';
      }).join('') + '</ul>' +
      '<h2>Schemes</h2><ul>' + (S.data.schemes || []).map(function (s) {
        return '<li>' + esc(s.name) + (s.portal_url ? ': <a href="' + esc(s.portal_url) + '" rel="noopener">official portal</a>' : '') + '</li>';
      }).join('') + '</ul></div>' +
      '<h2 id="coverage">What is covered so far</h2>' + coverageSection(query) +
      '<div class="prose"><p>Areas not in this table have not been checked. On the map they are shown in pale grey and listed as "Not checked yet".</p>' +
      '<h2>Data</h2><p>Case data last generated: ' + esc(when(S.data.generated_at)) + '.</p>' +
      '<p>Map boundaries are from the <a href="https://github.com/datameet/maps">DataMeet India community maps</a> under <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>. District shapes follow Census 2011.</p></div>';
    bindFilters('/method', query);
  }

  /* After each render: keep focus on a filter that was just changed, move to the list top on a page change, or go to the top on a new page. */
  function fillThumbs() {
    var els = Array.prototype.slice.call(view.querySelectorAll('.thumb[data-case]'));
    var cases = els.map(function (el) { return (S.waiting[el.getAttribute('data-case')] || [])[0]; }).filter(Boolean);
    if (!cases.length) return;
    loadLinks(cases).then(function () {
      els.forEach(function (el) {
        var w = S.waiting[el.getAttribute('data-case')];
        if (w && el.isConnected) el.outerHTML = thumb(w[0], w[1]);
      });
    });
  }

  function settle() {
    fillThumbs();
    var ctx = S.pending; S.pending = null;
    if (!ctx) return;
    var live = document.getElementById('announce');
    var top = document.getElementById('list-top');
    if (ctx.samePath) {
      var keep = S.keepFocus && document.getElementById(S.keepFocus);
      S.keepFocus = null;
      if (keep) { keep.focus(); if (live && top) live.textContent = top.textContent; return; }
      if (top) { top.focus({ preventScroll: true }); top.scrollIntoView({ block: 'start' }); return; }
    }
    S.keepFocus = null;
    if (ctx.moved) { window.scrollTo(0, 0); document.getElementById('main').focus({ preventScroll: true }); }
  }

  function route() {
    S.token++;
    var p = L.parseHash(location.hash), r = L.matchRoute(p.parts);
    var pathKey = p.parts.join('/');
    S.pending = { samePath: S.lastPath !== null && S.lastPath === pathKey, moved: S.lastPath !== null || pathKey !== '' };
    S.lastPath = pathKey;
    markNav(r.name);
    if (r.name === 'state' || r.name === 'district') { renderArea(r.state, r.district || null, p.query); return; }
    if (r.name === 'case') { loadCase(r.id, p.query); return; }
    if (r.name === 'cases') { loadAllCases(p.query); return; }
    if (r.name === 'home') { whenReady(loadLinks(topCases()), renderHome); return; }
    if (r.name === 'method') renderMethod(p.query);
    else renderNotFound();
    settle();
  }

  /* Runs render once `work` is done; shows a loading line only if that takes a moment. */
  function whenReady(work, render, message) {
    var token = S.token;
    var timer = setTimeout(function () {
      if (token === S.token) view.innerHTML = '<p class="status loading">' + esc(message || 'Loading case data...') + '</p>';
    }, 150);
    work.then(function () {
      clearTimeout(timer);
      if (token !== S.token) return;
      render();
      settle();
    }, function (e) {
      clearTimeout(timer);
      if (token !== S.token) return;
      loadFailed(e);
      settle();
    });
  }
  function loadFailed(e) {
    setTitle('Page not loaded');
    view.innerHTML = '<div class="state-box error" role="alert"><h1>This page could not be loaded</h1><p>Part of the case data did not arrive. Check the connection and try again.</p>' +
      '<p class="muted">Details: ' + esc(e && e.message) + '</p><button type="button" class="btn btn-primary" id="retry">Try again</button></div>';
    document.getElementById('retry').addEventListener('click', route);
  }

  /* summary.json lists one index file per state; each holds that state's cases without their reasons. */
  function indexFor(st) {
    return (S.data.index || []).filter(function (e) { return st == null || norm(e.state) === norm(st); });
  }
  function loadIndex(entries) {
    return Promise.all(entries.map(function (e) {
      if (!S.indexFiles[e.file]) {
        S.indexFiles[e.file] = getJSON(e.file).then(function (d) {
          // Kept in the order of the data files, so lists and filters come out as they did from one file.
          S.cases = S.cases.concat((d && d.cases) || []).sort(L.dataOrder);
        }).catch(function (err) { delete S.indexFiles[e.file]; throw err; });
      }
      return S.indexFiles[e.file];
    }));
  }

  /* Photo links are in data/links/<first two characters of the sha256>.json. A missing file means the
     site has no links at all, so it shows the original photos. */
  function loadLinks(cases) {
    var want = {};
    if (S.links === null) return Promise.resolve();
    (cases || []).forEach(function (c) { if (c && c.photo) want[shaOf(c).slice(0, 2)] = 1; });
    return Promise.all(Object.keys(want).map(function (part) {
      if (!S.linkFiles[part]) {
        S.linkFiles[part] = fetch('data/links/' + part + '.json', { cache: 'no-cache' }).then(function (r) {
          if (r.status === 404) { S.links = null; return; }
          if (!r.ok) throw new Error('links ' + r.status);
          return r.json().then(function (j) {
            if (S.links && j && typeof j === 'object') for (var k in j) S.links[k.toLowerCase()] = j[k];
          });
        }).catch(function () {}).then(function () { S.linkDone[part] = true; });
      }
      return S.linkFiles[part];
    }));
  }

  document.addEventListener('load', function (e) {
    var img = e.target;
    if (img && img.tagName === 'IMG' && img.parentNode && img.parentNode.classList) img.parentNode.classList.remove('is-loading');
  }, true);
  document.addEventListener('error', function (e) {
    var img = e.target;
    if (!img || img.tagName !== 'IMG' || !img.classList.contains('ph')) return;
    var holder = img.closest('.thumb, .photo-frame');
    if (!holder) return;
    holder.classList.add('thumb-empty');
    holder.innerHTML = '<span>' + photoNote('broken') + '</span>';
  }, true);

  var started = false;
  function start() {
    view.innerHTML = '<p class="status loading">Loading case data...</p>';
    var fallback = { site_name: 'Yojana Jaanch', site_name_hi: 'योजना जाँच', tagline: 'Checking the evidence behind government scheme records' };
    var cfg = getJSON('config.json').catch(function () { return fallback; });
    var geo = Promise.all([getJSON('vendor/geo/india-states.json'), getJSON('vendor/geo/districts-index.json')]).catch(function () { return null; });
    Promise.all([cfg, getJSON('data/summary.json'), getJSON('data/areas.json'), geo]).then(function (r) {
      S.config = r[0]; S.data = r[1]; S.areas = r[2];
      for (var k in fallback) if (!S.config[k]) S.config[k] = fallback[k];
      if (r[3]) { S.geo = r[3][0]; S.distIndex = r[3][1]; }
      document.querySelector('.masthead nav').hidden = false;
      document.getElementById('site-name').textContent = S.config.site_name;
      document.getElementById('site-name-hi').textContent = S.config.site_name_hi;
      document.getElementById('site-tagline').textContent = S.config.tagline;
      if (!started) {
        started = true;
        document.querySelector('a.skip').addEventListener('click', function (e) {
          e.preventDefault();
          document.getElementById('main').focus();
        });
        window.addEventListener('hashchange', route);
      }
      route();
    }).catch(function (e) {
      document.querySelector('.masthead nav').hidden = true;
      if (e && e.status === 404) {
        document.title = 'No data collected yet | Yojana Jaanch';
        view.innerHTML = '<div class="state-box" role="alert"><h1>No data collected yet</h1><p>This copy of the site has no case data. The files data/summary.json and data/areas.json are written by the checker after a collection run. ' +
          'To collect one block and build the data, run this in the project folder:</p><pre class="cmd"><code>' + esc(REFRESH_CMD) + '</code></pre>' +
          '<p>The README lists the other options. When the command finishes, check again.</p><button type="button" class="btn btn-primary" id="retry">Check again</button></div>';
      } else {
        view.innerHTML = '<div class="state-box error" role="alert"><h1>The case data could not be loaded</h1><p>The site needs data/summary.json and data/areas.json, which are produced by the checker. ' +
          'If you run this site yourself, run the checker first. Otherwise try again in a moment.</p><p class="muted">Details: ' + esc(e.message) + '</p>' +
          '<button type="button" class="btn btn-primary" id="retry">Try again</button></div>';
      }
      document.getElementById('retry').addEventListener('click', start);
    });
  }
  start();
})();
