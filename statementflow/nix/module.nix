# NixOS module for statementflow. A plain importable module (function of
# { config, lib, pkgs, ... }), so it works both as a flake output
# (nixosModules.statementflow) and via a direct import / pinned fetchTarball
# from a classic configuration.nix.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.statementflow;
  stateDir = "/var/lib/statementflow";
in
{
  options.services.statementflow = {
    enable = lib.mkEnableOption
      "statementflow, the household cash-flow Sankey";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
      description = "The statementflow package to run.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8770;
      description = ''
        Loopback port the app binds. It is only ever bound to 127.0.0.1 -- the
        trust model is that `tailscale serve` is the sole ingress, so the port
        must never be exposed directly (see `tailscaleServe`). This is internal;
        to change the port the app is *reached* on, set `servePort`.
      '';
    };

    servePort = lib.mkOption {
      type = lib.types.port;
      default = 443;
      example = 8443;
      description = ''
        Port `tailscale serve` publishes on, i.e. the one in the URL:
        https://<magicdns-name>:<servePort>/. The default 443 gives the bare
        https://<magicdns-name>/ but claims the whole node for this app, so pick
        a distinct port here for each app you serve from the same machine.

        Serving several apps on 443 under different paths (`tailscale serve
        --set-path`) is NOT usable here: the frontend fetches absolute paths
        (/flows, /static/...), so it would break under a path prefix. Use a port.
      '';
    };

    configDir = lib.mkOption {
      type = lib.types.path;
      default = "${stateDir}/config";
      description = ''
        Directory holding the PRIVATE config (users.yaml, accounts.yaml,
        categories.yaml, rules.yaml). This is personal data -- real accounts and
        payees -- so it is admin-managed on the host and never rendered into the
        world-readable Nix store. The service also writes rules.learned.yaml
        here, so it must be writable by the `statementflow` user.

        The module creates this directory (owned by the service user); populate
        it after first start, e.g.:

            sudo cp users.yaml accounts.yaml categories.yaml rules.yaml \
                ${stateDir}/config/
            sudo chown -R statementflow:statementflow ${stateDir}/config
      '';
    };

    tailscaleServe = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Expose the app over `tailscale serve`, which terminates TLS and injects
        the caller's identity as the Tailscale-User-Login header. The app trusts
        that header ONLY because serve is the sole path to the loopback port.
        Reachable at https://<magicdns-name>/ from any device on your tailnet.
        Requires services.tailscale.enable.

        Serve must also be enabled once for the tailnet itself, in the admin
        console. Until it is, the unit fails and retries every minute, and
        `journalctl -u statementflow-tailscale-serve` carries the enable link.
      '';
    };

    tailscalePackage = lib.mkOption {
      type = lib.types.package;
      default = config.services.tailscale.package or pkgs.tailscale;
      defaultText = lib.literalExpression "config.services.tailscale.package";
      description = "The tailscale CLI package used to configure serve.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.tailscaleServe -> config.services.tailscale.enable;
        message =
          "services.statementflow.tailscaleServe requires services.tailscale.enable = true"
          + " (tailscale serve is the app's only ingress).";
      }
    ];

    users.users.statementflow = {
      isSystemUser = true;
      group = "statementflow";
      description = "statementflow service user";
    };
    users.groups.statementflow = { };

    # Ensure the config dir exists and is owned by the service user, so the
    # admin can drop the private YAML in and the app can write learned rules.
    systemd.tmpfiles.rules = [
      "d ${stateDir} 0750 statementflow statementflow -"
      "d ${cfg.configDir} 0750 statementflow statementflow -"
    ];

    systemd.services.statementflow = {
      description = "statementflow: household cash-flow Sankey";
      documentation = [ "https://github.com/BWeesy/gubbins" ];
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];

      environment = {
        APP_CONFIG_DIR = toString cfg.configDir;
        STATEMENTFLOW_DB = "${stateDir}/statementflow.db";
        # Uploaded statements are retained here so an import can be re-run after
        # a profile fix. Personal data -- inside StateDirectory (0750, UMask
        # 0077), and backed up with the rest of /var/lib.
        STATEMENTFLOW_RAW_DIR = "${stateDir}/raw";
        # Require the Tailscale identity header. With serve as the sole ingress,
        # a request without it did not come through the tailnet -- reject it.
        STATEMENTFLOW_REQUIRE_IDENTITY = "1";
      };

      serviceConfig = {
        ExecStart =
          "${lib.getExe cfg.package} --host 127.0.0.1 --port ${toString cfg.port}";
        User = "statementflow";
        Group = "statementflow";
        Restart = "on-failure";
        RestartSec = 10;

        # SQLite DB, retained raw statements and learned rules live here; drops
        # into Restic alongside the rest of /var/lib.
        StateDirectory = "statementflow";
        StateDirectoryMode = "0750";
        ReadWritePaths = [ cfg.configDir ];

        # Hardening. The app needs a loopback socket and its state dir, nothing
        # else. If the unit fails to start, relax MemoryDenyWriteExecute and the
        # SystemCallFilter first.
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

    # Point `tailscale serve` at the loopback app. Serve config is stored in
    # tailscaled's state and persists; this oneshot (re)asserts it on boot.
    systemd.services.statementflow-tailscale-serve = lib.mkIf cfg.tailscaleServe {
      description = "Expose statementflow over tailscale serve";
      after = [ "tailscaled.service" "statementflow.service" ];
      wants = [ "tailscaled.service" ];
      requires = [ "statementflow.service" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        # Serves https://<magicdns>:<servePort>/ -> the loopback app. --https is
        # passed explicitly so the published port is visible in the unit rather
        # than an implicit default.
        ExecStart =
          "${lib.getExe cfg.tailscalePackage} serve --bg"
          + " --https ${toString cfg.servePort}"
          + " http://127.0.0.1:${toString cfg.port}";
        # Deliberately no ExecStop. The only blunt instrument available is
        # `tailscale serve reset`, which clears EVERY serve mapping on the node
        # -- so stopping this unit would take down any other app served from the
        # same machine. Leaving our mapping in place on stop is the lesser evil:
        # it points at a closed port until the unit runs again, and ExecStart is
        # idempotent so a restart simply re-asserts it. Drop the mapping by hand
        # with `tailscale serve reset` if you are retiring the app outright.

        # Serve is a tailnet-wide feature that has to be enabled once, and until
        # it is, `tailscale serve` sits waiting for someone to click the enable
        # link it prints. A oneshot blocks its target while ExecStart runs, so
        # without a bound that wait wedges multi-user.target -- hanging
        # nixos-rebuild, and every subsequent boot. Fail fast instead, and retry
        # quietly so the unit heals itself once Serve is switched on rather than
        # needing a manual start.
        TimeoutStartSec = 60;
        Restart = "on-failure";
        RestartSec = 60;
      };
    };
  };
}
