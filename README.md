# gubbins
A repository of the tools I use around the home assistant server.


### glowbridge

A single-file daemon that fetches hourly UK smart meter readings from the
Glowmarkt/DCC API (the backend behind Hildebrand's Bright app) and imports
them into Home Assistant as long-term statistics, stamped with each hour's
true time so the Energy dashboard shows when energy was used.
