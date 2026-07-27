# NixOS module for glowbridge. A plain importable module (function of
# { config, lib, pkgs, ... }), so it works both as a flake output
# (nixosModules.glowbridge) and via a direct import / pinned fetchTarball
# from a classic configuration.nix.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.glowbridge;

  # The rendered config lands in the (world-readable) Nix store. That is safe
  # by design: glowbridge takes its secrets ONLY from the environment
  # (GLOWBRIDGE_GLOW_USERNAME / GLOWBRIDGE_GLOW_PASSWORD / GLOWBRIDGE_HA_TOKEN),
  # never the config file, so this file carries no credentials. See
  # environmentFile.
  format = pkgs.formats.toml { };
  configFile = format.generate "glowbridge.toml" cfg.settings;
in
{
  options.services.glowbridge = {
    enable = lib.mkEnableOption
      "glowbridge, the Glowmarkt/DCC to Home Assistant statistics importer";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
      description = "The glowbridge package to run.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.path;
      example = "/etc/glowbridge.env";
      description = ''
        Path to a root-owned (chmod 600) file, OUTSIDE the Nix store, holding
        the credentials as systemd EnvironmentFile lines:

            GLOWBRIDGE_GLOW_USERNAME=you@example.com
            GLOWBRIDGE_GLOW_PASSWORD=...
            GLOWBRIDGE_HA_TOKEN=...

        systemd reads it as root before dropping to the service's dynamic
        user, so the file stays unreadable by the service and by other users.
        Never put credentials in `settings`: that would render them into the
        world-readable Nix store.
      '';
    };

    settings = lib.mkOption {
      type = format.type;
      default = { };
      example = lib.literalExpression ''
        {
          homeassistant.url = "ws://homeassistant:8123/api/websocket";
          schedule.interval = 1800;
        }
      '';
      description = ''
        glowbridge configuration, rendered to TOML. Mirrors glowbridge.toml
        (see CONFIG.md); `homeassistant.url` is required. Do NOT put
        credentials here — use environmentFile.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = ((cfg.settings.homeassistant or { }).url or null) != null;
        message =
          "services.glowbridge.settings.homeassistant.url must be set"
          + " (the WebSocket URL of your Home Assistant instance).";
      }
    ];

    systemd.services.glowbridge = {
      description =
        "glowbridge: Glowmarkt/DCC to Home Assistant statistics importer";
      documentation = [ "https://github.com/BWeesy/gubbins" ];
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];

      serviceConfig = {
        ExecStart = "${lib.getExe cfg.package} --config ${configFile}";
        EnvironmentFile = cfg.environmentFile;
        Restart = "on-failure";
        RestartSec = 30;

        # State (and status.json) live in /var/lib/glowbridge; glowbridge
        # reads $STATE_DIRECTORY, which systemd sets from this.
        StateDirectory = "glowbridge";
        StateDirectoryMode = "0700";

        # Run as an ephemeral, unprivileged user.
        DynamicUser = true;

        # Hardening: glowbridge needs only outbound network and its state dir.
        # If the unit fails to start, the two most likely culprits are
        # MemoryDenyWriteExecute and the SystemCallFilter — relax those first.
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectClock = true;
        ProtectHostname = true;
        # AF_NETLINK/AF_UNIX are needed by glibc name resolution; the rest is
        # plain outbound TCP.
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" "AF_NETLINK" ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [ "@system-service" "~@privileged" "~@resources" ];
        CapabilityBoundingSet = "";
        UMask = "0077";
      };
    };
  };
}
