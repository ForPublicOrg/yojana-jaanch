'use strict';
/* Pure helpers for routing, filtering and pagination. No DOM access, so tests can run them in node. */
(function (root) {
  var QUERY_ORDER = ['scheme', 'kind', 'state', 'district', 'block', 'office', 'village', 'sort', 'page'];

  function dec(s) { try { return decodeURIComponent(String(s).replace(/\+/g, ' ')); } catch (e) { return String(s); } }
  function same(a, b) { return String(a == null ? '' : a).trim().toLowerCase() === String(b == null ? '' : b).trim().toLowerCase(); }

  function parseHash(hash) {
    var h = String(hash || '').replace(/^#\/?/, '');
    var qi = h.indexOf('?');
    var path = qi < 0 ? h : h.slice(0, qi), q = qi < 0 ? '' : h.slice(qi + 1);
    var parts = path.split('/').filter(function (p) { return p !== ''; }).map(dec);
    var query = {};
    q.split('&').forEach(function (kv) {
      if (!kv) return;
      var i = kv.indexOf('=');
      var k = dec(i < 0 ? kv : kv.slice(0, i)), v = dec(i < 0 ? '' : kv.slice(i + 1));
      if (k && v !== '') query[k] = v;
    });
    return { parts: parts, query: query };
  }

  function matchRoute(parts) {
    var p = parts || [];
    if (!p.length) return { name: 'home' };
    if (p[0] === 'method' && p.length === 1) return { name: 'method' };
    if (p[0] === 'cases' && p.length === 1) return { name: 'cases' };
    if (p[0] === 'state' && p.length === 2) return { name: 'state', state: p[1] };
    if (p[0] === 'state' && p.length === 4 && p[2] === 'district') return { name: 'district', state: p[1], district: p[3] };
    if (p[0] === 'case' && p.length >= 2) return { name: 'case', id: p.slice(1).join('/') };
    return { name: 'notfound' };
  }

  function buildHash(path, query) {
    var q = query || {}, keys = Object.keys(q).filter(function (k) {
      var v = q[k];
      if (v == null || v === '') return false;
      if (k === 'page' && Number(v) === 1) return false;
      if (k === 'sort' && v === 'size') return false;
      return true;
    });
    keys.sort(function (a, b) {
      var ia = QUERY_ORDER.indexOf(a), ib = QUERY_ORDER.indexOf(b);
      ia = ia < 0 ? 99 : ia; ib = ib < 0 ? 99 : ib;
      return ia - ib || (a < b ? -1 : a > b ? 1 : 0);
    });
    var s = keys.map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(q[k]); }).join('&');
    return path + (s ? '?' + s : '');
  }

  function withQuery(query, changes) {
    var out = {}, k;
    for (k in query) out[k] = query[k];
    for (k in changes) { if (changes[k] == null || changes[k] === '') delete out[k]; else out[k] = changes[k]; }
    return out;
  }

  function paginate(total, page, size) {
    var pages = Math.max(1, Math.ceil(total / size));
    var asked = String(page == null ? '' : page).trim();
    var p = /^\d+$/.test(asked) ? parseInt(asked, 10) : 1;
    if (p < 1) p = 1;
    if (p > pages) p = pages;
    var start = (p - 1) * size, end = Math.min(total, start + size);
    return { page: p, pages: pages, size: size, total: total, start: start, end: end, corrected: asked !== '' && String(p) !== asked };
  }

  /* Page numbers with null for a gap. Always the same number of slots once there are many pages. */
  function pageList(page, pages, siblings) {
    var s = siblings == null ? 1 : siblings, out = [], i;
    var slots = 2 * s + 5;
    if (pages <= slots) { for (i = 1; i <= pages; i++) out.push(i); return out; }
    if (page <= s + 3) {
      for (i = 1; i <= 2 * s + 3; i++) out.push(i);
      return out.concat([null, pages]);
    }
    if (page >= pages - s - 2) {
      out = [1, null];
      for (i = pages - 2 * s - 2; i <= pages; i++) out.push(i);
      return out;
    }
    out = [1, null];
    for (i = page - s; i <= page + s; i++) out.push(i);
    return out.concat([null, pages]);
  }

  var GETTERS = {
    scheme: function (c) { return [c.scheme]; },
    kind: function (c) { return [c.kind]; },
    state: function (c) { return [c.state]; },
    district: function (c) { return [c.district]; },
    block: function (c) { return [c.block]; },
    office: function (c) { return c.offices || []; },
    village: function (r) { return [r.village]; },
    created_by: function (r) { return [r.created_by]; }
  };
  function valuesOf(item, field) { return (GETTERS[field] || function (x) { return [x[field]]; })(item); }

  function matches(item, filters, skip) {
    for (var k in filters) {
      if (skip && skip.indexOf(k) !== -1) continue;
      var want = filters[k];
      if (want == null || want === '') continue;
      if (!valuesOf(item, k).some(function (v) { return same(v, want); })) return false;
    }
    return true;
  }
  function filterItems(items, filters) { return (items || []).filter(function (c) { return matches(c, filters); }); }

  /* Counts for one field, using every other active filter, biggest first. */
  function facet(items, filters, field) {
    var counts = {}, names = {};
    (items || []).forEach(function (c) {
      if (!matches(c, filters, [field])) return;
      var seen = {};
      valuesOf(c, field).forEach(function (v) {
        if (v == null || v === '') return;
        var k = String(v).trim().toLowerCase();
        if (seen[k]) return;
        seen[k] = 1; names[k] = names[k] || String(v); counts[k] = (counts[k] || 0) + 1;
      });
    });
    return Object.keys(counts).map(function (k) { return { value: names[k], n: counts[k] }; })
      .sort(function (a, b) { return b.n - a.n || a.value.localeCompare(b.value); });
  }

  function bySize(a, b) {
    return (b.severity || 0) - (a.severity || 0) || (b.record_count || 0) - (a.record_count || 0) || String(a.id).localeCompare(String(b.id));
  }
  function firstVillage(c) { return (c.villages && c.villages[0]) || ''; }
  function sortCases(list, sort) {
    var out = (list || []).slice();
    if (sort === 'village') out.sort(function (a, b) { return firstVillage(a).localeCompare(firstVillage(b)) || bySize(a, b); });
    else out.sort(bySize);
    return out;
  }

  /* Scheme, then kind, with counts. */
  function kindSummary(cases) {
    var schemes = {};
    (cases || []).forEach(function (c) {
      var s = schemes[c.scheme] = schemes[c.scheme] || { scheme: c.scheme, n: 0, kinds: {} };
      s.n++; s.kinds[c.kind] = (s.kinds[c.kind] || 0) + 1;
    });
    return Object.keys(schemes).map(function (k) {
      var s = schemes[k];
      return { scheme: s.scheme, n: s.n, kinds: Object.keys(s.kinds).map(function (kind) { return { kind: kind, n: s.kinds[kind] }; })
        .sort(function (a, b) { return b.n - a.n || a.kind.localeCompare(b.kind); }) };
    }).sort(function (a, b) { return b.n - a.n || a.scheme.localeCompare(b.scheme); });
  }

  function sortRecords(records) {
    return (records || []).slice().sort(function (a, b) {
      return String(a.village || '').localeCompare(String(b.village || '')) ||
        String(a.record_id).localeCompare(String(b.record_id), undefined, { numeric: true });
    });
  }

  /* The case with this id, or the case that took it over: when one picture saved as several files is
     found, the case of the file on the most records keeps its id and lists the ids of the others in other_ids. */
  function findCase(cases, id) {
    var list = cases || [], i;
    for (i = 0; i < list.length; i++) if (list[i].id === id) return { c: list[i], moved: false };
    for (i = 0; i < list.length; i++) if ((list[i].other_ids || []).indexOf(id) !== -1) return { c: list[i], moved: true };
    return null;
  }

  /* Number of different files a case's picture was saved as; 1 for a single file. */
  function fileCount(c) {
    return c && c.photo_files && c.photo_files.length > 1 ? c.photo_files.length : 1;
  }

  /* The comparison a scheme used: { distance, mad } when files were matched by picture, else null. */
  function nearMatch(schemes) {
    var on = (schemes || []).filter(function (s) { return s.photo_match && s.photo_match.near_matching; });
    if (!on.length) return null;
    return { schemes: on.map(function (s) { return s.name || s.id; }), distance: on[0].photo_match.max_distance };
  }

  var api = { findCase: findCase, fileCount: fileCount, nearMatch: nearMatch, parseHash: parseHash, matchRoute: matchRoute, buildHash: buildHash, withQuery: withQuery, paginate: paginate,
    pageList: pageList, matches: matches, filterItems: filterItems, facet: facet, sortCases: sortCases,
    kindSummary: kindSummary, sortRecords: sortRecords, same: same };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.YJ = api;
})(this);
