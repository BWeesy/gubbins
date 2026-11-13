# NixOS module for ha-server-mcp. A plain importable module (function of
# { config, lib, pkgs, ... }), so it works as a flake output
# (nixosModules.ha-server-mcp) or via a direct import.
#
# What it sets up, and why each piece is shaped the way it is:
#
#   * A `claude` system user: not in wheel, no usable password, reachable
#     only by one SSH key. That key's authorized_keys line carries
#     from="<addresses>",restrict,command="<this server>", so sshd runs the
#     MCP server and nothing else — whatever command the client asks for is
#     ignored, and restrict turns off ptys, forwarding and agent/X11.
#   * One tiny, argument-less helper script per privileged action, and a
#     NOPASSWD sudo rule for each helper's exact store path followed by ""
#     (sudoers for "no arguments allowed"). The server can only pick which
#     fixed helper to run; it can never pass a path, flag or flake ref.
#   * Two oneshot units, the upgrade and the rollback. The upgrade builds
#     ONLY the flake ref in `upgrade.flake`, which the host config sets.
#     Changing what the box runs therefore means changing that repo.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.ha-server-mcp;

  systemctl = "${config.systemd.package}/bin/systemctl";
  journalctl = "${config.systemd.package}/bin/journalctl";

  upgradeUnit = "ha-server-mcp-upgrade.service";
  rollbackUnit = "ha-server-mcp-rollback.service";

  helper = name: text: pkgs.writeShellScript "ha-server-mcp-${name}" ''
    set -euo pipefail
    ${text}
  '';

  # Read status.json and nothing else. iflag=nofollow refuses a symlink at
  # that path, so a compromised glowbridge cannot point it at state.json or
  # /etc/shadow and have root read it out. (Hard links to files it does not
  # own are already blocked by fs.protected_hardlinks.) Capped at 256 KiB.
  statusHelper = helper "glowbridge-status" ''
    exec ${pkgs.coreutils}/bin/dd if=${lib.escapeShellArg cfg.glowbridge.statusFile} \
      iflag=nofollow bs=64K count=4 status=none
  '';

  vmHelper = helper "vm-state" ''
    exec ${config.virtualisation.libvirtd.package}/bin/virsh \
      -c ${lib.escapeShellArg cfg.vm.libvirtUri} \
      domstate --reason ${lib.escapeShellArg cfg.vm.name}
  '';
  vmEnabled = cfg.vm.name != null && config.virtualisation.libvirtd.enable;

  # Refuse to start one job while the other (or itself) is busy, checked as
  # root so the server's own check is only a courtesy.
  startHelper = name: unit: other: helper "start-${name}" ''
    for u in ${unit} ${other}; do
      state=$(${systemctl} show --property=ActiveState --value "$u")
      case "$state" in
        activating|active|deactivating|reloading)
          echo "$u is already running" >&2
          exit 3
          ;;
      esac
    done
    # --no-block: return now; rebuilds take minutes.
    exec ${systemctl} start --no-block ${unit}
  '';
  upgradeHelper = startHelper "upgrade" upgradeUnit rollbackUnit;
  rollbackHelper = startHelper "rollback" rollbackUnit upgradeUnit;

  logUnits = lib.unique (
    lib.optional cfg.glowbridge.enable cfg.glowbridge.unit
    ++ cfg.extraLogUnits
    ++ [ upgradeUnit rollbackUnit ]
  );
  journalHelper = unit:
    helper "journal-${lib.strings.sanitizeDerivationName unit}" ''
      exec ${journalctl} --unit=${lib.escapeShellArg unit} \
        --lines=${toString cfg.maxLogLines} --no-pager --output=short-iso --quiet
    '';
  journalHelpers = lib.genAttrs logUnits journalHelper;

  allHelpers =
    [ upgradeHelper rollbackHelper ]
    ++ lib.attrValues journalHelpers
    ++ lib.optional cfg.glowbridge.enable statusHelper
    ++ lib.optional vmEnabled vmHelper;

  # Holds unit names, helper store paths and the upgrade source: nothing
  # secret, so the world-readable Nix store is fine.
  serverConfig = pkgs.writeText "ha-server-mcp.json" (builtins.toJSON {
    sudo = "${config.security.wrapperDir}/sudo";
    inherit systemctl;
    disk_paths = cfg.diskPaths;
    max_log_lines = cfg.maxLogLines;
    profiles_dir = "/nix/var/nix/profiles";
    upgrade_source = cfg.upgrade.flake;
    glowbridge =
      if cfg.glowbridge.enable
      then { unit = cfg.glowbridge.unit; status_helper = "${statusHelper}"; }
      else null;
    vm = if vmEnabled then { name = cfg.vm.name; helper = "${vmHelper}"; } else null;
    log_units = lib.mapAttrs (_: h: "${h}") journalHelpers;
    upgrade = { unit = upgradeUnit; start_helper = "${upgradeHelper}"; };
    rollback = { unit = rollbackUnit; start_helper = "${rollbackHelper}"; };
  });

  forcedCommand = "${lib.getExe cfg.package} --config ${serverConfig}";
  authorizedKey =
    ''from="${lib.concatStringsSep "," cfg.ssh.allowFrom}",restrict,''
    + ''command="${forcedCommand}" ${cfg.ssh.publicKey}'';

  nixosRebuild = config.system.build.nixos-rebuild;
in
{
  options.services.ha-server-mcp = {
    enable = lib.mkEnableOption
      "ha-server-mcp, a narrowly scoped MCP server reached over SSH";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
      description = "The ha-server-mcp package to run.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "claude";
      description = "The SSH-only system user the MCP server runs as.";
    };

    ssh = {
      publicKey = lib.mkOption {
        type = lib.types.str;
        example = "ssh-ed25519 AAAA... claude@desktop";
        description = ''
          The client's public key, bare (no options): the module prepends
          from=, restrict and command= itself.
        '';
      };
      allowFrom = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        example = [ "192.168.1.20" "100.64.0.7" ];
        description = ''
          Addresses (or sshd patterns) the key is accepted from, rendered
          into the key's from= option.
        '';
      };
    };

    upgrade.flake = lib.mkOption {
      type = lib.types.str;
      example = "git+ssh://git@github.com/you/host-config?ref=main#myhost";
      description = ''
        The only flake the upgrade job builds: `nixos-rebuild switch
        --refresh --flake <this>`. Point it at a branch that only changes by
        reviewed merges.
      '';
    };

    upgrade.sshKeyFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/root/.ssh/host-config-deploy";
      description = ''
        Private key git uses to fetch `upgrade.flake` over SSH — a READ-ONLY
        deploy key, so a compromised host cannot write its way back into its
        own config. A string path outside the Nix store, never a Nix path
        literal (that would copy the key into the store). The host's known
        hosts must already contain the git server's key: fetching runs with
        StrictHostKeyChecking=yes.
      '';
    };

    glowbridge = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = config.services.glowbridge.enable or false;
        defaultText = lib.literalExpression "config.services.glowbridge.enable";
        description = "Whether to expose glowbridge's status and logs.";
      };
      unit = lib.mkOption {
        type = lib.types.str;
        default = "glowbridge.service";
        description = "glowbridge's systemd unit.";
      };
      statusFile = lib.mkOption {
        type = lib.types.str;
        default = "/var/lib/glowbridge/status.json";
        description = ''
          glowbridge's status file. Never state.json, which holds the Glow
          auth token.
        '';
      };
    };

    vm = {
      name = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "haos";
        description = ''
          libvirt domain name of the Home Assistant VM, reported by
          host_health. Null (or libvirtd not enabled) leaves it out.
        '';
      };
      libvirtUri = lib.mkOption {
        type = lib.types.str;
        default = "qemu:///system";
        description = "libvirt connection URI for the VM.";
      };
    };

    extraLogUnits = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "libvirtd.service" "sshd.service" ];
      description = ''
        Units service_logs may read, on top of glowbridge and the upgrade
        and rollback jobs. Each gets its own fixed journal helper.
      '';
    };

    maxLogLines = lib.mkOption {
      type = lib.types.ints.positive;
      default = 500;
      description = "Most journal lines any one log read returns.";
    };

    diskPaths = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "/" ];
      example = [ "/" "/nix" "/var/lib/libvirt" ];
      description = "Mount points whose usage host_health reports.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.ssh.allowFrom != [ ];
        message = "services.ha-server-mcp.ssh.allowFrom must list at least one address.";
      }
      {
        assertion = !(lib.hasInfix "\n" cfg.ssh.publicKey);
        message = "services.ha-server-mcp.ssh.publicKey must be a single key line.";
      }
      {
        assertion = baseNameOf cfg.glowbridge.statusFile != "state.json";
        message = "services.ha-server-mcp.glowbridge.statusFile must not be state.json (it holds the Glow auth token).";
      }
      {
        # The rules below are written for classic sudo; sudo-rs has its own
        # options.
        assertion = config.security.sudo.enable;
        message = "services.ha-server-mcp needs security.sudo.enable.";
      }
      {
        assertion = !config.security.sudo.execWheelOnly;
        message =
          "services.ha-server-mcp: security.sudo.execWheelOnly stops the"
          + " (non-wheel) ${cfg.user} user running sudo at all.";
      }
    ];

    warnings = lib.optional (cfg.vm.name != null && !config.virtualisation.libvirtd.enable)
      "services.ha-server-mcp.vm.name is set but virtualisation.libvirtd is not enabled; host_health will not report the VM.";

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.user;
      description = "ha-server-mcp (SSH forced command only)";
      home = "/var/empty";
      createHome = false;
      # sshd runs the forced command through the login shell, so this cannot
      # be nologin. The command= option means the shell only ever runs the
      # MCP server.
      shell = pkgs.bashInteractive;
      # "*" matches no password, and unlike "!" sshd does not treat it as a
      # locked account, so key login works with or without PAM.
      hashedPassword = "*";
      openssh.authorizedKeys.keys = [ authorizedKey ];
    };
    users.groups.${cfg.user} = { };

    security.sudo.extraRules = [
      {
        users = [ cfg.user ];
        runAs = "root";
        commands = map
          (h: {
            # A trailing "" means: this exact path, with no arguments.
            command = ''${h} ""'';
            options = [ "NOPASSWD" ];
          })
          allHelpers;
      }
    ];

    # The upgrade job is `nixos-rebuild --flake`. Merges with whatever the
    # host already enables.
    nix.settings.experimental-features = [ "nix-command" "flakes" ];

    # Modelled on nixpkgs' system.autoUpgrade, minus the timer: it runs only
    # when started, and only ever builds cfg.upgrade.flake.
    systemd.services.ha-server-mcp-upgrade = {
      description = "NixOS upgrade from the host's git config (via ha-server-mcp)";
      documentation = [ "https://github.com/BWeesy/gubbins" ];
      # Never restart this unit mid-switch because the switch changed it.
      restartIfChanged = false;
      unitConfig.X-StopOnRemoval = false;
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      serviceConfig.Type = "oneshot";
      environment = config.nix.envVars // config.networking.proxy.envVars // {
        HOME = "/root";
      } // lib.optionalAttrs (cfg.upgrade.sshKeyFile != null) {
        GIT_SSH_COMMAND = lib.concatStringsSep " " [
          "${config.programs.ssh.package}/bin/ssh"
          "-i ${lib.escapeShellArg cfg.upgrade.sshKeyFile}"
          "-o IdentitiesOnly=yes"
          "-o StrictHostKeyChecking=yes"
        ];
      };
      path = with pkgs; [
        coreutils
        gnutar
        xz.bin
        gzip
        gitMinimal
        config.nix.package.out
        config.programs.ssh.package
      ];
      script = ''
        ${nixosRebuild}/bin/nixos-rebuild switch --refresh --flake ${lib.escapeShellArg cfg.upgrade.flake}
      '';
    };

    # The primitive form of `nixos-rebuild switch --rollback`: step the
    # system profile back one generation, then activate it. Needs no flake
    # and no network, so it still works when the upgrade source is broken.
    systemd.services.ha-server-mcp-rollback = {
      description = "Roll NixOS back one generation (via ha-server-mcp)";
      documentation = [ "https://github.com/BWeesy/gubbins" ];
      restartIfChanged = false;
      unitConfig.X-StopOnRemoval = false;
      serviceConfig.Type = "oneshot";
      script = ''
        ${config.nix.package.out}/bin/nix-env --profile /nix/var/nix/profiles/system --rollback
        exec /nix/var/nix/profiles/system/bin/switch-to-configuration switch
      '';
    };
  };
}
