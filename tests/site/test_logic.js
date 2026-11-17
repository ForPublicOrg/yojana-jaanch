'use strict';
// Run with: node tests/site/test_logic.js
var assert = require('assert');
var path = require('path');
var L = require(path.join(__dirname, '..', '..', 'site', 'lib.js'));

var tests = [];
function test(name, fn) { tests.push([name, fn]); }

test('paginate middle page', function () {
  var p = L.paginate(302, '2', 20);
  assert.deepStrictEqual([p.page, p.pages, p.start, p.end, p.corrected], [2, 16, 20, 40, false]);
});
test('paginate last page is partial', function () {
  var p = L.paginate(302, 16, 20);
  assert.deepStrictEqual([p.start, p.end], [300, 302]);
});
test('paginate out of range and junk fall back', function () {
  assert.strictEqual(L.paginate(302, '99', 20).page, 16);
  assert.strictEqual(L.paginate(302, '99', 20).corrected, true);
  assert.strictEqual(L.paginate(302, '0', 20).page, 1);
  assert.strictEqual(L.paginate(302, 'abc', 20).page, 1);
  assert.strictEqual(L.paginate(302, '-3', 20).page, 1);
  assert.strictEqual(L.paginate(302, undefined, 20).corrected, false);
});
test('paginate empty list', function () {
  var p = L.paginate(0, '3', 20);
  assert.deepStrictEqual([p.page, p.pages, p.start, p.end], [1, 1, 0, 0]);
});
test('pageList small shows all', function () {
  assert.deepStrictEqual(L.pageList(2, 5, 1), [1, 2, 3, 4, 5]);
  assert.deepStrictEqual(L.pageList(1, 7, 1), [1, 2, 3, 4, 5, 6, 7]);
});
test('pageList stays compact with many pages', function () {
  assert.deepStrictEqual(L.pageList(1, 150, 1), [1, 2, 3, 4, 5, null, 150]);
  assert.deepStrictEqual(L.pageList(50, 150, 1), [1, null, 49, 50, 51, null, 150]);
  assert.deepStrictEqual(L.pageList(150, 150, 1), [1, null, 146, 147, 148, 149, 150]);
  for (var i = 1; i <= 150; i++) {
    var l = L.pageList(i, 150, 1);
    assert.strictEqual(l.length, 7);
    assert.ok(l.indexOf(i) !== -1);
  }
});
test('parseHash reads path and query', function () {
  var h = L.parseHash('#/state/Punjab/district/Sangrur?block=Bhawani%20Garh&page=3&empty=');
  assert.deepStrictEqual(h.parts, ['state', 'Punjab', 'district', 'Sangrur']);
  assert.deepStrictEqual(h.query, { block: 'Bhawani Garh', page: '3' });
});
test('matchRoute rejects malformed area paths', function () {
  assert.strictEqual(L.matchRoute(L.parseHash('#/state/Punjab/Sangrur').parts).name, 'notfound');
  assert.strictEqual(L.matchRoute(L.parseHash('#/state/Punjab').parts).name, 'state');
  assert.strictEqual(L.matchRoute(L.parseHash('#/state/Punjab/district/Sangrur').parts).name, 'district');
  assert.strictEqual(L.matchRoute(L.parseHash('#/').parts).name, 'home');
  assert.strictEqual(L.matchRoute(L.parseHash('#/cases?kind=same_day').parts).name, 'cases');
  assert.strictEqual(L.matchRoute(L.parseHash('#/nope').parts).name, 'notfound');
  assert.strictEqual(L.matchRoute(L.parseHash('#/case/jsjb-x').parts).id, 'jsjb-x');
});
test('buildHash drops defaults and orders keys', function () {
  assert.strictEqual(L.buildHash('/cases', { page: 1, sort: 'size', kind: 'same_day' }), '/cases?kind=same_day');
  assert.strictEqual(L.buildHash('/cases', { page: 2, block: 'Bhawani Garh', kind: 'photo_reuse' }), '/cases?kind=photo_reuse&block=Bhawani%20Garh&page=2');
  var round = L.parseHash('#' + L.buildHash('/cases', { office: 'BDPO & Co', page: 4 }));
  assert.deepStrictEqual(round.query, { office: 'BDPO & Co', page: '4' });
});
test('changing a filter resets the page', function () {
  var q = L.withQuery({ kind: 'same_day', page: '3' }, { block: 'Andana', page: null });
  assert.deepStrictEqual(q, { kind: 'same_day', block: 'Andana' });
});

var CASES = [
  { id: 'a', scheme: 'jsjb', kind: 'photo_reuse', block: 'Sangrur', villages: ['Zira'], offices: ['BDPO Sangrur'], severity: 50, record_count: 10 },
  { id: 'b', scheme: 'jsjb', kind: 'photo_reuse', block: 'Andana', villages: ['Alpha'], offices: ['BDPO Moonak', 'FRO'], severity: 8, record_count: 3 },
  { id: 'c', scheme: 'jsjb', kind: 'same_day', block: 'Andana', villages: ['Mid'], offices: ['BDPO Moonak'], severity: 20, record_count: 6 },
  { id: 'd', scheme: 'other', kind: 'same_gps', block: 'Sangrur', villages: [], offices: ['BDPO Sangrur'], severity: 5, record_count: 2 }
];
test('filterItems combines filters, offices match any', function () {
  assert.deepStrictEqual(L.filterItems(CASES, { block: 'andana' }).map(function (c) { return c.id; }), ['b', 'c']);
  assert.deepStrictEqual(L.filterItems(CASES, { office: 'FRO' }).map(function (c) { return c.id; }), ['b']);
  assert.deepStrictEqual(L.filterItems(CASES, { block: 'Andana', kind: 'same_gps' }), []);
});
test('facet counts ignore their own filter', function () {
  assert.deepStrictEqual(L.facet(CASES, { block: 'Andana' }, 'block'), [{ value: 'Andana', n: 2 }, { value: 'Sangrur', n: 2 }]);
  assert.deepStrictEqual(L.facet(CASES, { block: 'Andana' }, 'office'), [{ value: 'BDPO Moonak', n: 2 }, { value: 'FRO', n: 1 }]);
});
test('sortCases by size and by village', function () {
  assert.deepStrictEqual(L.sortCases(CASES, 'size').map(function (c) { return c.id; }), ['a', 'c', 'b', 'd']);
  assert.deepStrictEqual(L.sortCases(CASES, 'village').map(function (c) { return c.id; }), ['d', 'b', 'c', 'a']);
  assert.strictEqual(CASES[0].id, 'a');
});
test('kindSummary groups by scheme then kind', function () {
  var s = L.kindSummary(CASES);
  assert.deepStrictEqual(s.map(function (g) { return [g.scheme, g.n]; }), [['jsjb', 3], ['other', 1]]);
  assert.deepStrictEqual(s[0].kinds, [{ kind: 'photo_reuse', n: 2 }, { kind: 'same_day', n: 1 }]);
});
test('sortRecords by village then numeric id', function () {
  var r = L.sortRecords([{ village: 'B', record_id: '10' }, { village: 'A', record_id: '9' }, { village: 'B', record_id: '9' }]);
  assert.deepStrictEqual(r.map(function (x) { return x.village + x.record_id; }), ['A9', 'B9', 'B10']);
});
test('findCase finds a case by its own id or by an id it took over', function () {
  var cases = [{ id: 'jsjb-photo_reuse-aaa', other_ids: ['jsjb-photo_reuse-bbb'] }, { id: 'jsjb-photo_reuse-ccc' }];
  assert.deepStrictEqual(L.findCase(cases, 'jsjb-photo_reuse-ccc'), { c: cases[1], moved: false });
  assert.deepStrictEqual(L.findCase(cases, 'jsjb-photo_reuse-bbb'), { c: cases[0], moved: true });
  assert.strictEqual(L.findCase(cases, 'jsjb-photo_reuse-zzz'), null);
  assert.strictEqual(L.findCase(undefined, 'x'), null);
});
test('fileCount counts the files of a picture', function () {
  assert.strictEqual(L.fileCount({}), 1);
  assert.strictEqual(L.fileCount({ photo_files: [{ sha256: 'a' }] }), 1);
  assert.strictEqual(L.fileCount({ photo_files: [{ sha256: 'a' }, { sha256: 'b' }, { sha256: 'c' }] }), 3);
});
test('nearMatch reports only schemes where files were compared by picture', function () {
  assert.strictEqual(L.nearMatch([{ id: 'jsjb' }]), null);
  assert.strictEqual(L.nearMatch([{ id: 'jsjb', photo_match: { near_matching: false } }]), null);
  assert.deepStrictEqual(L.nearMatch([{ id: 'jsjb', name: 'JSJB', photo_match: { near_matching: true, max_distance: 8 } }]),
    { schemes: ['JSJB'], distance: 8 });
});

var failed = 0;
tests.forEach(function (t) {
  try { t[1](); console.log('ok   ' + t[0]); } catch (e) { failed++; console.log('FAIL ' + t[0] + '\n     ' + e.message); }
});
console.log(tests.length - failed + ' of ' + tests.length + ' passed');
process.exit(failed ? 1 : 0);
