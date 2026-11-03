# sound-lab

A browser page for designing short game sound effects and downloading them as
WAV files. It was built to make the six sounds for BWeesy's Authority Tracker
(−1 ping, −5 thud, −10 explosion, +1 ding, +5 woing, +10 wooom), but anyone can
use it: every sound is synthesised from a handful of numbers, so there is no
sample library and nothing to license.

Live at <https://bweesy.github.io/gubbins/sound-lab/> once promoted (dev:
<https://bweesy.github.io/gubbins/dev/sound-lab/>). Deployment works the same
way as on-the-angle: see [its README](../on-the-angle/README.md#deployment).

Everything lives in `index.html`: one self-contained page, no build step, no
dependencies.

## How a sound is made

Each sound is the same small synth with different settings, rendered sample by
sample in plain JavaScript:

| Layer | What it does | Good for |
| --- | --- | --- |
| Tone | A waveform sweeping from a start pitch to an end pitch, with optional wobble (vibrato), bell overtones and detuned extra voices | pings, dings, woings, wooms |
| Sub bass | A low sine sweeping down (or up) | the weight in a thud or explosion |
| Noise | Seeded white noise through a low-pass filter whose brightness sweeps | the crack of a thud, the body of an explosion, the air in a wooom |
| Envelope | Attack, length and decay curve shared by all three, then the result is normalised to the volume setting | — |

Noise is seeded (the **Variation** slider), so the same settings always give the
same samples: what you hear is exactly what you download.

## Using the results

- **Download WAV** gives a 44.1 kHz, 16-bit mono WAV, named for an Android
  `res/raw/` folder (`sfx_minus_1.wav` and so on).
- **Export settings** saves all six sounds as JSON. Commit that file next to the
  WAVs so the sounds can be regenerated or tweaked later; **Import settings**
  loads it back.
- **Copy share link** puts all six sounds' settings in the URL, which is the
  easiest way to send a set to someone else to try.
- Your latest tweaks are kept in the browser's local storage.
