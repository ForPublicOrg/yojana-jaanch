# Design notes

Direction: a modern investigations data site. Evidence first, figures before prose, no hype.

## Palette
- Header ink `#16181d`: a dark band gives the name weight and frames the light working area.
- Base `#f2f1ed`, surfaces `#ffffff`, rules `#dcdad3`: a calm warm neutral so photos and red carry the page.
- Text `#16181d`, secondary `#555860`: both pass WCAG AA on the base and on white.
- One accent, red `#b3261e` (dark `#8f1d12`, tint `#fbe9e6`): only for anomalies, meaning case counts, map shading, problem bars and kind labels.
- Buttons and the current page use ink, not red, so red always means "something was flagged".
- Map shading by cases, four solid steps `#d8644d`, `#b8382a`, `#8a2014`, `#55100a` (1 to 4, 5 to 19, 20 to 99, 100 or more): sequential, and every step has at least 3:1 contrast with "Not checked yet" and with the white card.
- "Partly checked, nothing found" is slate `#55707f` (4.6:1 with "Not checked yet"); it is told apart from the red steps by hue.
- "Not checked yet" is near-white `#f1f0eb` with a grey edge `#8a877f` (3.6:1 on white), so unchecked areas recede and any shaded area stands out.
- Lists show checked areas as tiles; unchecked areas are folded into one "Not checked yet" list.
- Focus `#1d5bd8` (light blue `#8cb4ff` on the dark header): visible on every surface and never confused with the red.

## Type
- System sans stack: Segoe UI Variable / Segoe UI, system-ui, San Francisco, Roboto, Noto Sans.
- Hindi uses Nirmala UI, Noto Sans Devanagari or Kohinoor Devanagari, which ship with Windows, Android/Linux and macOS.
- Why no vendored font: no download or licence to manage, nothing fetched at runtime, and each platform's own Devanagari face renders conjuncts and matras correctly.
- Strong size contrast: page titles up to 2.6rem at weight 750, figures up to 2.3rem, body 16px, labels 0.8 to 0.9rem.
- Figures and tables use tabular numerals so columns of counts line up.

## Spacing and shape
- Scale of 4, 8, 12, 16, 20, 24, 28, 40px: tight inside cards, wider between sections, so groups read as groups.
- Radius 6px on cards and panels, 4px on controls: just enough to separate surfaces without looking like pills.
- No shadows except the map tooltip, which floats over the map and needs to lift.
- Thin 1px borders separate surfaces; no gradients, glass or glow.

## Components
- Figures row at the top of each view: every number is computed from the case data and its coverage list (on the home page, from the totals in summary.json).
- Case cards: fixed 4:3 thumbnail (lazy loaded), kind label, headline, record count in red, place, record IDs.
- Missing, withheld or broken photos show a neutral grey box that says which of the three it is.
- Category summary as bars with counts; each bar is a filter link.
- Map: white borders between areas, ink outline on hover, focus and selection, a count pill on each area with cases.
- Small areas get the pill on a short leader line so the shape stays visible.
- Motion: 150ms colour transitions on the map and cards only; switched off for reduced motion.

## Photos
- `photoFor()` in app.js is the single place that picks a photo URL.
- With the files in data/links/ it uses those URLs, and a case not listed shows "Photo withheld".
- Without those files it falls back to the local photos/ folder for private preview.
