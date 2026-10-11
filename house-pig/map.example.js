// Copy to map.local.js (gitignored) and fill in your own values.
// The numbers below are made up; they are only here to show the shape.
window.PIG_MAP = {
  // Map image next to this file. Any *.png / *.jpg here is gitignored.
  image: 'map.png',

  // Map millimetres -> image pixels:
  //   px = originX + scale * x_mm
  //   py = originY - scale * y_mm   (y points up on the map, down in the image)
  // README.md explains how to work these out from the rooms attribute.
  scale: 0.08,
  originX: 350,
  originY: 270,

  // Dock position in map millimetres (charger_position on the map camera).
  dock: [0, -500],

  // The zone the editor opens with, as [x1, y1, x2, y2] in map millimetres:
  // the value currently in input_text.pig_litter_zone.
  zone: [-1500, -1000, 1500, 1000],
};
