# gubbins
A repository of the tools I use around the home assistant server.


### glowbridge

A single-file daemon that fetches hourly UK smart meter readings from the
Glowmarkt/DCC API (the backend behind Hildebrand's Bright app) and imports
them into Home Assistant as long-term statistics, stamped with each hour's
true time so the Energy dashboard shows when energy was used.

### house-pig

Helpers for a Dreame robot vacuum run from Home Assistant. Currently a zone
editor that draws a cleaning zone over the vacuum's own map and gives back the
coordinates to paste into Home Assistant. The map itself is gitignored.

### on-the-angle

An interactive 3D model of field-hockey goalkeeper positioning, published to
GitHub Pages at <https://bweesy.github.io/gubbins/on-the-angle/>. One
self-contained HTML page; no build step.
