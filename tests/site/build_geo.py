"""Convert DataMeet shapefiles into small SVG-path JSON files for site/data/geo/.

Usage: python -I build_geo.py STATES.shp STATES.dbf DIST.shp DIST.dbf OUT_DIR
Reads only polygon shapefiles (type 5). Standard library only.
"""
import json, math, os, struct, sys


def read_dbf(path):
    with open(path, 'rb') as f:
        data = f.read()
    n, hlen, rlen = struct.unpack('<IHH', data[4:12])
    fields, pos = [], 32
    while data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b'\0')[0].decode('latin-1')
        fields.append((name, data[pos + 16]))
        pos += 32
    rows = []
    for i in range(n):
        rec = data[hlen + i * rlen + 1: hlen + (i + 1) * rlen]
        row, off = {}, 0
        for name, size in fields:
            row[name] = rec[off:off + size].decode('utf-8', 'replace').strip()
            off += size
        rows.append(row)
    return rows


def read_shp(path):
    with open(path, 'rb') as f:
        data = f.read()
    pos, shapes = 100, []
    while pos < len(data):
        clen = struct.unpack('>I', data[pos + 4:pos + 8])[0] * 2
        rec = data[pos + 8:pos + 8 + clen]
        pos += 8 + clen
        if struct.unpack('<i', rec[:4])[0] != 5:
            shapes.append([])
            continue
        nparts, npts = struct.unpack('<ii', rec[36:44])
        parts = list(struct.unpack('<%di' % nparts, rec[44:44 + 4 * nparts])) + [npts]
        base = 44 + 4 * nparts
        pts = [struct.unpack('<2d', rec[base + 16 * k: base + 16 * k + 16]) for k in range(npts)]
        shapes.append([pts[parts[k]:parts[k + 1]] for k in range(nparts)])
    return shapes


def simplify(pts, tol):
    if len(pts) < 4:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy) or 1e-12
        best, idx = 0, None
        for i in range(a + 1, b):
            d = abs(dy * (pts[i][0] - x1) - dx * (pts[i][1] - y1)) / norm
            if d > best:
                best, idx = d, i
        if idx is not None and best > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, keep) if k]


LAT0 = math.cos(math.radians(23))

STATE_NAMES = {
    'Andaman & Nicobar': 'Andaman and Nicobar Islands',
    'Andaman & Nicobar Island': 'Andaman and Nicobar Islands',
    'Jammu & Kashmir': 'Jammu and Kashmir',
    'Arunanchal Pradesh': 'Arunachal Pradesh',
    'NCT of Delhi': 'Delhi',
    'Dadara & Nagar Havelli': 'Dadra and Nagar Haveli and Daman and Diu',
    'Daman & Diu': 'Dadra and Nagar Haveli and Daman and Diu',
}
# Census 2011 predates Telangana (2014) and Ladakh UT (2019).
TELANGANA_2011 = {'Adilabad', 'Hyderabad', 'Karimnagar', 'Khammam', 'Mahbubnagar',
                  'Medak', 'Nalgonda', 'Nizamabad', 'Rangareddy', 'Warangal'}


def proj(lon, lat):
    return lon * LAT0, -lat


def to_path(rings, tol, min_area):
    out = []
    for ring in rings:
        p = [proj(*xy) for xy in ring]
        xs = [a for a, _ in p]; ys = [b for _, b in p]
        if (max(xs) - min(xs)) * (max(ys) - min(ys)) < min_area:
            continue
        mid = len(p) // 2
        s = simplify(p[:mid + 1], tol)[:-1] + simplify(p[mid:], tol)
        if len(s) < 4:
            continue
        out.append('M' + 'L'.join('%.3f %.3f' % xy for xy in s) + 'Z')
    return ''.join(out)


def bbox(rings):
    p = [proj(*xy) for r in rings for xy in r]
    xs = [a for a, _ in p]; ys = [b for _, b in p]
    return [round(min(xs), 3), round(min(ys), 3), round(max(xs), 3), round(max(ys), 3)]


def label_point(rings):
    best, c = 0, None
    for ring in rings:
        p = [proj(*xy) for xy in ring]
        a = cx = cy = 0
        for (x1, y1), (x2, y2) in zip(p, p[1:]):
            k = x1 * y2 - x2 * y1
            a += k; cx += (x1 + x2) * k; cy += (y1 + y2) * k
        if abs(a) > best:
            best, c = abs(a), [round(cx / (3 * a), 3), round(cy / (3 * a), 3)]
    return c


def main(sshp, sdbf, dshp, ddbf, out):
    os.makedirs(os.path.join(out, 'districts'), exist_ok=True)
    states = {}
    for row, rings in zip(read_dbf(sdbf), read_shp(sshp)):
        name = STATE_NAMES.get(row['ST_NM'], row['ST_NM'])
        states[name] = {'d': to_path(rings, 0.03, 0.0004), 'bbox': bbox(rings), 'c': label_point(rings)}
    with open(os.path.join(out, 'india-states.json'), 'w', encoding='utf-8') as f:
        json.dump(states, f, separators=(',', ':'))
    per_state = {}
    for row, rings in zip(read_dbf(ddbf), read_shp(dshp)):
        if not rings:
            continue
        st, dt = STATE_NAMES.get(row['ST_NM'], row['ST_NM']), row['DISTRICT']
        if dt in ('Leh (ladakh)', 'Kargil'):
            st, dt = 'Ladakh', 'Leh' if dt.startswith('Leh') else dt
        elif st == 'Andhra Pradesh' and dt in TELANGANA_2011:
            st = 'Telangana'
        per_state.setdefault(st, {})[dt] = {'d': to_path(rings, 0.008, 0.00005), 'bbox': bbox(rings), 'c': label_point(rings)}
    index = {}
    for st, dts in per_state.items():
        slug = ''.join(c if c.isalnum() else '-' for c in st.lower()).strip('-')
        with open(os.path.join(out, 'districts', slug + '.json'), 'w', encoding='utf-8') as f:
            json.dump(dts, f, separators=(',', ':'))
        index[st] = 'districts/' + slug + '.json'
    with open(os.path.join(out, 'districts-index.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=0, sort_keys=True)
    print(sorted(states), sorted(index))


if __name__ == '__main__':
    main(*sys.argv[1:6])
