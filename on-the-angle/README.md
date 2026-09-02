# on-the-angle

An interactive 3D model of field-hockey goalkeeper positioning: place the ball
anywhere in the circle, move the keeper, and see how much of the goal mouth the
keeper actually covers from the shooter's point of view.

Live at <https://bweesy.github.io/gubbins/on-the-angle/>.

Everything lives in `index.html` -- one self-contained page with no build step.
The only external dependency is three.js, loaded from a CDN at runtime.

## Deployment

There are two copies of the site, both served from the one Pages deployment a
repository gets:

| | URL | Built from |
| --- | --- | --- |
| production | <https://bweesy.github.io/gubbins/on-the-angle/> | the commit the `prod` tag points at |
| dev | <https://bweesy.github.io/gubbins/dev/on-the-angle/> | whatever is on `main` |

Every merge to `main` updates dev. Production only moves when someone runs the
**promote to production** workflow from the Actions tab, which moves the `prod`
tag and redeploys. That is what keeps links already shared with people steady
while main carries on.

There is no integration branch: `main` is the only branch that deploys, and the
promote workflow refuses to pin anything that is not already an ancestor of it.

Each deploy rebuilds both paths, because a Pages deploy replaces the whole
site. Dev copies get a banner and a `noindex` so they cannot be mistaken for
the shared link. The site root is a landing page kept in `.github/pages/`, and
Pages is configured in the repo settings with source "GitHub Actions".

## Local preview

Open `index.html` in a browser, or serve the directory over HTTP:

    python3 -m http.server --directory on-the-angle 8000
