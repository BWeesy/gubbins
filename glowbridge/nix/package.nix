# Builds glowbridge as a self-contained executable backed by a nixpkgs
# Python, rather than fetching dependencies with uv at runtime. The PEP 723
# metadata in the script is ignored here on purpose: the interpreter already
# has requests and websocket-client on its path, so a plain
# `python glowbridge.py` entry point works and stays pure, offline and
# reproducible — the NixOS way. The trade-off is that dependency versions
# come from the consuming nixpkgs channel rather than the committed
# *.py.lock, which is the normal Nix model.
{ lib
, stdenvNoCC
, python3
, makeWrapper
}:

let
  pythonEnv = python3.withPackages (ps: with ps; [ requests websocket-client ]);

  # Track the script's own VERSION so the two never drift.
  versionLine = lib.findFirst (lib.hasPrefix "VERSION = ") null
    (lib.splitString "\n" (builtins.readFile ../glowbridge.py));
  version = builtins.head (builtins.match ''VERSION = "([^"]+)"'' versionLine);
in
stdenvNoCC.mkDerivation {
  pname = "glowbridge";
  inherit version;

  src = lib.cleanSource ../.;

  nativeBuildInputs = [ makeWrapper ];

  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    install -Dm0755 glowbridge.py "$out/libexec/glowbridge/glowbridge.py"
    makeWrapper ${pythonEnv}/bin/python "$out/bin/glowbridge" \
      --add-flags "$out/libexec/glowbridge/glowbridge.py"
    runHook postInstall
  '';

  meta = {
    description = "Import Glowmarkt/DCC smart meter readings into Home Assistant statistics";
    homepage = "https://github.com/BWeesy/gubbins";
    license = lib.licenses.mit;
    mainProgram = "glowbridge";
    platforms = lib.platforms.linux;
  };
}
