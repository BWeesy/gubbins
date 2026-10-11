# house-pig

Helpers for a Dreame robot vacuum run from Home Assistant through the
[Tasshack dreame-vacuum](https://github.com/Tasshack/dreame-vacuum)
integration. The automations themselves live in Home Assistant; this folder
holds the tools that sit beside them.

## Zone editor

`zone-editor.html` draws a cleaning zone over the vacuum's own map so you can
drag it into place, then copy the `[x1, y1, x2, y2]` numbers (map millimetres)
into the zone helper the cleaning script reads. Open the file straight from
disk; there is no build step and nothing to serve.

### Privacy

The map is a floor plan of the house, and this is a public repo. The editor
therefore reads everything personal from two files that are gitignored:

| File | What it holds | In git? |
| --- | --- | --- |
| `zone-editor.html` | The tool | Yes |
| `map.example.js` | Template with made-up numbers | Yes |
| `map.local.js` | Your calibration, dock position and current zone | **No** |
| `map.png` (any image here) | Your map | **No** |

The `house-pig` CI workflow fails the build if any of the private files is
ever tracked, so a forced `git add` cannot slip one through.

### Setting it up

1. **Map image.** In Home Assistant, open the vacuum's *Current Map* camera
   entity and save the picture as `house-pig/map.png`. Use the camera image
   itself, not a screenshot, so the pixels match the calibration below.
   Don't copy the image URL into anything committed: it carries an access
   token.

2. **Config.** Copy `map.example.js` to `map.local.js`.

3. **Calibration.** The map camera's `rooms` attribute gives every room's
   bounding box in map millimetres (`x0`, `y0`, `x1`, `y1`). Find the same
   room's edges in the image in pixels, then:

   ```
   scale   = (right_px - left_px) / (x1 - x0)
   originX = left_px - scale * x0
   originY = top_px  + scale * y1
   ```

   Check the result against a second room; with a good fit every room lands
   within a pixel or two. If `calibration_points` on the camera is populated,
   it gives the same mapping directly.

4. **Dock and zone.** Set `dock` from the camera's `charger_position`, and
   `zone` from the current value of the zone helper.

Re-do steps 1 and 3 if the map is edited or re-mapped, since the rendered
image (and so the mapping) changes with it.
