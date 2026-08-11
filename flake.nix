{
  description = "gubbins: home tools, including glowbridge (Glowmarkt/DCC smart meter readings into Home Assistant statistics)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs:
        let glowbridge = pkgs.callPackage ./glowbridge/nix/package.nix { };
        in {
          inherit glowbridge;
          default = glowbridge;
        });

      # A plain importable module, so a classic configuration.nix can consume
      # it via a pinned fetchTarball just as well as a flake can.
      nixosModules.glowbridge = import ./glowbridge/nix/module.nix;
      nixosModules.default = self.nixosModules.glowbridge;

      checks = forAllSystems (pkgs: {
        glowbridge-package = pkgs.callPackage ./glowbridge/nix/package.nix { };
        glowbridge-vm = pkgs.callPackage ./glowbridge/nix/vm-test.nix {
          glowbridgeModule = self.nixosModules.glowbridge;
        };
      });
    };
}
