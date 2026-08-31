# on-the-angle

An interactive 3D model of field-hockey goalkeeper positioning: place the ball
anywhere in the circle, move the keeper, and see how much of the goal mouth the
keeper actually covers from the shooter's point of view.

Live at <https://bweesy.github.io/gubbins/on-the-angle/>.

Everything lives in `index.html` -- one self-contained page with no build step.
The only external dependency is three.js, loaded from a CDN at runtime.

## Deployment

`.github/workflows/pages.yml` copies this directory to `on-the-angle/` on the
published site and deploys it on every push to `main` that touches it. The
site root is a landing page kept in `.github/pages/`, and Pages is configured
in the repo settings with source "GitHub Actions".

## Local preview

Open `index.html` in a browser, or serve the directory over HTTP:

    python3 -m http.server --directory on-the-angle 8000
