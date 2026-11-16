# Map data sources

All files in this folder are derived from DataMeet India community maps,
licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/).
Attribution: India boundaries by DataMeet India community (http://datameet.org/), https://github.com/datameet/maps

- india-states.json: from States/Admin2.shp. Includes Ladakh as a union territory and Telangana.
- districts/*.json: from Districts/Census_2011/2011_Dist.shp. Boundaries are as of Census 2011.
  Leh and Kargil are placed under Ladakh; the ten 2011 Telangana districts are placed under Telangana.
  Districts created after 2011 (for example Fazilka, Pathankot and Malerkotla in Punjab) have no shape
  and appear only in the district list on the site.

Changes made: converted to SVG path strings with an equirectangular projection
(x = lon * cos 23 degrees, y = -lat), simplified with Douglas-Peucker, tiny islands dropped.
Rebuild with tests/site/build_geo.py.
