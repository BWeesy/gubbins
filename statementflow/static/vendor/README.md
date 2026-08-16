<!--
SPDX-License-Identifier: MIT
-->

# Vendored frontend libraries

These are committed on purpose rather than fetched from a CDN at runtime: this
app shows financial data, so we do not want a third-party CDN able to inject
script into the page (and vendoring also lets the UI work offline over the
tailnet).

## echarts.min.js

- **Version:** 5.6.0
- **Source:** `https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js`
- **SHA-256:** `bf4a223524e40b77c304bec67e1222cf551f14880cf42c69dc046558e11c07b1`
- **Licence:** Apache-2.0 (Apache Software Foundation)

To update: download the new pinned version, replace the file, and update the
version + SHA-256 above. Verify the hash before committing.
