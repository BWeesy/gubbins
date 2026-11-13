# Builds ha-server-mcp as a self-contained executable backed by a nixpkgs
# Python, the same way glowbridge/nix/package.nix does: the PEP 723 metadata
# is ignored and the interpreter already carries the `mcp` SDK, so the
# entry point is pure, offline and reproducible. The script pins mcp<2
# because nixpkgs ships the 1.x line; keep the two in step.
{ lib
, stdenvNoCC
, python3
, makeWrapper
}:

let
  pythonEnv = python3.withPackages (ps: [ ps.mcp ]);

  # Track the script's own VERSION so the two never drift.
  versionLine = lib.findFirst (lib.hasPrefix "VERSION = ") null
    (lib.splitString "\n" (builtins.readFile ../ha_server_mcp.py));
  version = builtins.head (builtins.match ''VERSION = "([^"]+)"'' versionLine);
in
stdenvNoCC.mkDerivation {
  pname = "ha-server-mcp";
  inherit version;

  src = lib.cleanSource ../.;

  nativeBuildInputs = [ makeWrapper ];

  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    install -Dm0755 ha_server_mcp.py "$out/libexec/ha-server-mcp/ha_server_mcp.py"
    makeWrapper ${pythonEnv}/bin/python "$out/bin/ha-server-mcp" \
      --add-flags "$out/libexec/ha-server-mcp/ha_server_mcp.py"
    runHook postInstall
  '';

  meta = {
    description = "Narrowly scoped stdio MCP server for health checks and upgrades of a NixOS home server";
    homepage = "https://github.com/BWeesy/gubbins";
    license = lib.licenses.mit;
    mainProgram = "ha-server-mcp";
    platforms = lib.platforms.linux;
  };
}
