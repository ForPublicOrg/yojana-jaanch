'use strict';
/* node check_summary.js <data folder>: the figures in summary.json, written by checker/output.py, are
   what the site's own functions count from the index files, so the home page shows the same numbers
   the list pages would. */
var assert = require('assert');
var fs = require('fs');
var path = require('path');
var L = require(path.join(__dirname, '..', '..', 'site', 'lib.js'));

var dir = process.argv[2];
function read(rel) { return JSON.parse(fs.readFileSync(path.join(dir, rel), 'utf8')); }
var s = read('summary.json');
var all = [];
s.index.forEach(function (e) {
  var part = read(e.file.replace(/^data\//, '')).cases;
  assert.strictEqual(part.length, e.cases, e.file);
  assert.deepStrictEqual(part.slice().sort(L.dataOrder).map(function (c) { return c.id; }), part.map(function (c) { return c.id; }), e.file + ' is not in data order');
  part.forEach(function (c) { assert.ok(L.same(c.state || '', e.state || ''), c.id + ' is in ' + e.file); });
  all = all.concat(part);
});
all.sort(L.dataOrder);

assert.strictEqual(s.total, all.length);
var v = {};
all.forEach(function (c) { (c.villages || []).forEach(function (x) { v[L.norm(c.block) + '|' + L.norm(x)] = 1; }); });
assert.strictEqual(s.villages_with_case, Object.keys(v).length);
assert.deepStrictEqual(L.kindGroups(s.kinds), L.kindSummary(all));
function top(list) { return L.sortCases(list, 'size').slice(0, 5).map(function (c) { return c.id; }); }
assert.deepStrictEqual(top(s.top), top(all));
assert.deepStrictEqual(s.top.map(function (c) { return c.id; }), all.slice(0, s.top.length).map(function (c) { return c.id; }));
console.log('summary.json matches ' + all.length + ' cases in ' + s.index.length + ' index files');
