'use strict';
/* Bookmarklet that fills the JSJB public dashboard filters and runs the search.
   The case page links to the portal with the filter values in the address (#yj=...). On the portal,
   the first click makes the same requests the dropdowns would make, one at a time, and submits the
   search. A click on a results page marks this case's records there.
   app.js turns this function's source into the bookmark, so it must not use anything outside itself. */
(function (root) {
  function jsjbFill() {
    var NOTE = 'yj-fill-note';
    function say(text, bad) {
      var n = document.getElementById(NOTE);
      if (!n) {
        n = document.createElement('div');
        n.id = NOTE;
        n.setAttribute('role', 'status');
        n.style.cssText = 'position:fixed;bottom:12px;left:12px;right:12px;z-index:99999;padding:12px 16px;font:15px/1.4 system-ui,sans-serif;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.25)';
        document.body.appendChild(n);
      }
      n.style.background = bad ? '#7a1010' : '#14202e';
      n.style.color = '#fff';
      n.textContent = text;
    }
    if (!/(^|\.)jsjb\.mowr\.gov\.in$/.test(location.hostname)) {
      alert('This bookmark works on the JSJB public dashboard. Open a village link on a Yojana Jaanch case page, then click this bookmark there.');
      return;
    }
    var m = /[#&]yj=([^&]+)/.exec(location.hash), t = null, KEY = 'yj-fill';
    try { t = m ? JSON.parse(decodeURIComponent(m[1])) : JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch (e) { t = null; }
    if (!t || !t.s || !t.v) {
      say('No filters found. Open the portal from a village link on a Yojana Jaanch case page, then click the bookmark again.', true);
      return;
    }
    try { sessionStorage.setItem(KEY, JSON.stringify(t)); } catch (e) {}
    var url = location.pathname;
    function clean(x) { return String(x || '').replace(/\s*\(\d+\)\s*$/, '').trim().toLowerCase(); }
    function mark() {
      var ids = {}; (t.ids || []).forEach(function (id) { ids[String(id)] = true; });
      var marked = [];
      Array.prototype.forEach.call(document.querySelectorAll('#gvData tr'), function (tr) {
        var cell = tr.cells && tr.cells[0], mm = cell && /\((\d+)\)/.exec(cell.textContent);
        if (!mm || !ids[mm[1]]) return;
        tr.style.outline = '3px solid #1d4ed8';
        Array.prototype.forEach.call(tr.cells, function (td) { td.style.background = '#cfe3ff'; td.style.color = '#0b1a33'; });
        marked.push(tr);
      });
      if (marked.length) marked[0].scrollIntoView({ block: 'center' });
      var n = Object.keys(ids).length;
      say(marked.length
        ? 'Marked in blue: ' + marked.length + ' of the ' + n + ' records of this case in ' + t.v + ', on this page of results.' + (marked.length < n ? ' After opening another results page, click the bookmark again to mark the rest.' : '')
        : (n ? 'None of the ' + n + ' records of this case in ' + t.v + ' is on this page of results. Open another results page and click the bookmark again.' : 'Showing the results for ' + t.v + '.'));
    }
    /* Already on this village's results (for example after changing the results page): only mark. */
    var cur = document.querySelector('select[name="ddlVillageWard"]');
    if (!m && document.getElementById('gvData') && cur && cur.selectedIndex >= 0 && clean(cur.options[cur.selectedIndex].text) === clean(t.v)) {
      mark();
      return;
    }
    function form(doc) {
      var o = {};
      Array.prototype.forEach.call(doc.querySelectorAll('input[type=hidden]'), function (i) { if (i.name) o[i.name] = i.value; });
      return o;
    }
    function options(doc, id) {
      var s = doc.querySelector('select[name="' + id + '"]') || doc.getElementById(id);
      if (!s) throw new Error('The portal page has no ' + id + ' list. Its layout may have changed.');
      return Array.prototype.filter.call(s.options, function (o) { return o.value && o.value !== '0'; });
    }
    function pick(doc, id, name, label) {
      var hit = options(doc, id).filter(function (o) { return clean(o.text) === clean(name); })[0];
      if (!hit) throw new Error(label + ' "' + name + '" is not in the portal list. Choose it by hand.');
      return hit.value;
    }
    function post(doc, target, values) {
      var body = new URLSearchParams(form(doc));
      body.set('__EVENTTARGET', target);
      body.set('__EVENTARGUMENT', '');
      Object.keys(values).forEach(function (k) { body.set(k, values[k]); });
      return fetch(url, { method: 'POST', body: body, credentials: 'same-origin' }).then(function (r) {
        if (!r.ok) throw new Error('The portal answered ' + r.status + '. Try again in a minute.');
        return r.text();
      }).then(function (html) { return { html: html, doc: new DOMParser().parseFromString(html, 'text/html') }; });
    }
    var v = {}, page;
    say('Choosing ' + t.s + '...');
    fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.text(); }).then(function (html) {
      page = { doc: new DOMParser().parseFromString(html, 'text/html') };
      v.ddlState = pick(page.doc, 'ddlState', t.s, 'State');
      return post(page.doc, 'ddlState', v);
    }).then(function (p) {
      page = p;
      v.ddlDistrict = pick(p.doc, 'ddlDistrict', t.d, 'District');
      var areas = options(p.doc, 'ddlArea');
      if (t.a) areas = areas.filter(function (o) { return clean(o.text) === clean(t.a); });
      if (!areas.length) throw new Error('Area type "' + t.a + '" is not in the portal list. Choose it by hand.');
      say('Choosing ' + t.d + ' and ' + t.b + '...');
      function tryArea(i) {
        var vals = {}; Object.keys(v).forEach(function (k) { vals[k] = v[k]; });
        vals.ddlArea = areas[i].value;
        return post(page.doc, 'ddlArea', vals).then(function (q) {
          var hit = options(q.doc, 'ddlBlockTown').filter(function (o) { return clean(o.text) === clean(t.b); })[0];
          if (hit) { v = vals; v.ddlBlockTown = hit.value; return q; }
          if (i + 1 < areas.length) return tryArea(i + 1);
          throw new Error('Block or town "' + t.b + '" is not in the portal list. Choose it by hand.');
        });
      }
      return tryArea(0);
    }).then(function (p) {
      return post(p.doc, 'ddlBlockTown', v);
    }).then(function (p) {
      v.ddlVillageWard = pick(p.doc, 'ddlVillageWard', t.v, 'Village or ward');
      say('Searching ' + t.v + '...');
      /* The search itself is a normal form submit, so the results page is the portal's own page
         with working photo links and page buttons. Marking needs a second click there. */
      var fields = form(p.doc);
      Object.keys(v).forEach(function (k) { fields[k] = v[k]; });
      fields.__EVENTTARGET = '';
      fields.__EVENTARGUMENT = '';
      fields.btnSearch = 'Search';
      var f = document.createElement('form');
      f.method = 'post';
      f.action = url;
      f.style.display = 'none';
      Object.keys(fields).forEach(function (k) {
        var i = document.createElement('input');
        i.type = 'hidden'; i.name = k; i.value = fields[k];
        f.appendChild(i);
      });
      document.body.appendChild(f);
      HTMLFormElement.prototype.submit.call(f);
    }).catch(function (e) { say(e.message || String(e), true); });
  }

  root.YJ_JSJB_FILL = jsjbFill;
})(typeof window !== 'undefined' ? window : this);
