# Builds statementflow as a self-contained executable backed by a nixpkgs
# Python, rather than fetching dependencies with uv at runtime. uv's managed
# Pythons do not run on NixOS; here the interpreter already has fastapi,
# uvicorn, pyyaml and python-multipart on its path, so a plain
# `python -m uvicorn app.main:app` entry point works and stays pure, offline
# and reproducible. Dependency versions come from the consuming nixpkgs channel
# rather than the committed uv.lock, which is the normal Nix model.
{ lib
, stdenvNoCC
, python3
, makeWrapper
}:

let
  pythonEnv = python3.withPackages (ps: with ps; [
    fastapi
    uvicorn
    pyyaml
    python-multipart
  ]);

  # Track the app's own __version__ so the two never drift.
  versionLine = lib.findFirst (lib.hasPrefix "__version__ = ") null
    (lib.splitString "\n" (builtins.readFile ../app/__init__.py));
  version = builtins.head (builtins.match ''__version__ = "([^"]+)"'' versionLine);
in
stdenvNoCC.mkDerivation {
  pname = "statementflow";
  inherit version;

  src = lib.cleanSource ../.;

  nativeBuildInputs = [ makeWrapper ];

  dontConfigure = true;
  dontBuild = true;

  # Copy the app next to static/ and profiles/ so the code's __file__-relative
  # lookups (static assets, bank profiles) resolve inside the store. config/ is
  # deliberately NOT shipped: real config is admin-managed at runtime via
  # APP_CONFIG_DIR, never baked into the world-readable store.
  installPhase = ''
    runHook preInstall
    mkdir -p "$out/libexec/statementflow"
    cp -r app static profiles "$out/libexec/statementflow/"
    makeWrapper ${pythonEnv}/bin/python "$out/bin/statementflow" \
      --add-flags "-m uvicorn app.main:app" \
      --chdir "$out/libexec/statementflow" \
      --set PYTHONPATH "$out/libexec/statementflow"
    runHook postInstall
  '';

  meta = {
    description = "Self-hosted monthly cash-flow Sankey for a household, over Tailscale";
    homepage = "https://github.com/BWeesy/gubbins";
    license = lib.licenses.mit;
    mainProgram = "statementflow";
    platforms = lib.platforms.linux;
  };
}
