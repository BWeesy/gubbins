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
        let
          glowbridge = pkgs.callPackage ./glowbridge/nix/package.nix { };
          statementflow = pkgs.callPackage ./statementflow/nix/package.nix { };
          ha-server-mcp = pkgs.callPackage ./ha-server-mcp/nix/package.nix { };
        in {
          inherit glowbridge statementflow ha-server-mcp;
          default = glowbridge;
        });

      # Plain importable modules, so a classic configuration.nix can consume
      # them via a pinned fetchTarball just as well as a flake can.
      nixosModules.glowbridge = import ./glowbridge/nix/module.nix;
      nixosModules.statementflow = import ./statementflow/nix/module.nix;
      nixosModules.ha-server-mcp = import ./ha-server-mcp/nix/module.nix;
      nixosModules.default = self.nixosModules.glowbridge;

      checks = forAllSystems (pkgs: {
        glowbridge-package = pkgs.callPackage ./glowbridge/nix/package.nix { };
        glowbridge-vm = pkgs.callPackage ./glowbridge/nix/vm-test.nix {
          glowbridgeModule = self.nixosModules.glowbridge;
        };
        statementflow-package = pkgs.callPackage ./statementflow/nix/package.nix { };
        statementflow-vm = pkgs.callPackage ./statementflow/nix/vm-test.nix {
          statementflowModule = self.nixosModules.statementflow;
        };
        ha-server-mcp-package = pkgs.callPackage ./ha-server-mcp/nix/package.nix { };
        ha-server-mcp-vm = pkgs.callPackage ./ha-server-mcp/nix/vm-test.nix {
          haServerMcpModule = self.nixosModules.ha-server-mcp;
        };
      });
    };
}
