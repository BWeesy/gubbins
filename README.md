# gubbins
A repository of the tools I use around the home assistant server.


### glowbridge

A single-file bridge that fetches half-hourly UK smart meter readings from
the Glowmarkt/DCC API (the backend behind Hildebrand's Bright app) and
publishes a monotonic cumulative consumption total per meter to MQTT, with
Home Assistant discovery.
