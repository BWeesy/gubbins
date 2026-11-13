# nixosTest that boots the ha-server-mcp module in a throwaway VM and drives
# it the way the real client does: ssh as `claude`, MCP over the forced
# command, every tool called. Alongside that it checks the boundary itself:
# the sudo rules are exactly the fixed helpers, a helper refuses extra
# arguments, and the user cannot read glowbridge's state.json.
#
# glowbridge is faked (a unit that writes a status.json and a state.json
# holding a dummy secret) so the test needs no Glow or Home Assistant. The VM
# has no network, so the upgrade job fails to fetch — that is expected, and
# still proves the unit is wired.
{ pkgs, testers, haServerMcpModule }:

let
  keys = import "${pkgs.path}/nixos/tests/ssh-keys.nix" pkgs;
  secret = "SECRET-GLOW-TOKEN-0d3f";
  client = pkgs.writeShellScript "vm-test-client" ''
    exec ${pkgs.python3.withPackages (ps: [ ps.mcp ])}/bin/python \
      ${./vm-test-client.py} "$@"
  '';
in
testers.runNixOSTest {
  name = "ha-server-mcp";

  nodes.machine = { ... }: {
    imports = [ haServerMcpModule ];

    services.openssh.enable = true;

    # Stand-in for glowbridge: same unit name, DynamicUser and 0700 state
    # directory, so the status helper meets the real permissions.
    systemd.services.glowbridge = {
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        DynamicUser = true;
        StateDirectory = "glowbridge";
        StateDirectoryMode = "0700";
        Type = "simple";
      };
      script = ''
        echo '{"schema": 1, "ok": true}' > "$STATE_DIRECTORY/status.json"
        echo '{"token": "${secret}"}' > "$STATE_DIRECTORY/state.json"
        echo "fake glowbridge up"
        exec sleep infinity
      '';
    };

    services.ha-server-mcp = {
      enable = true;
      glowbridge.enable = true;
      ssh = {
        publicKey = keys.snakeOilEd25519PublicKey;
        allowFrom = [ "127.0.0.1" "::1" ];
      };
      upgrade.flake = "git+ssh://git@example.invalid/host-config?ref=main#machine";
    };
  };

  testScript = ''
    import re

    machine.wait_for_unit("sshd.service")
    machine.wait_for_unit("glowbridge.service")
    machine.wait_until_succeeds("test -f /var/lib/glowbridge/status.json")
    machine.succeed("install -m600 ${keys.snakeOilEd25519PrivateKey} /root/claude-key")

    with subtest("claude is an ordinary, non-wheel user"):
        groups = machine.succeed("id -nG claude").split()
        assert "wheel" not in groups, groups

    with subtest("sudo allows only the fixed helpers, with no arguments"):
        rules = machine.succeed("sudo -l -U claude")
        print(rules)
        granted = rules.split("may run the following commands")[1]
        # Nothing but helper store paths: no ALL, no other binary.
        assert not re.search(r"\bALL\b", granted), granted
        helpers = re.findall(r"/nix/store/[^\s,]+", granted)
        assert helpers and all("-ha-server-mcp-" in h for h in helpers), helpers
        status_helper = next(h for h in helpers if h.endswith("glowbridge-status"))
        machine.succeed(f"runuser -u claude -- sudo -n {status_helper}")
        machine.fail(f"runuser -u claude -- sudo -n {status_helper} extra-arg")
        machine.fail("runuser -u claude -- sudo -n ${pkgs.coreutils}/bin/cat /etc/shadow")

    with subtest("state.json stays unreadable"):
        machine.fail("runuser -u claude -- cat /var/lib/glowbridge/state.json")

    with subtest("status helper refuses a symlinked status.json"):
        machine.succeed(
            "cd /var/lib/private/glowbridge"
            " && mv status.json status.real && ln -s state.json status.json"
        )
        machine.fail(f"runuser -u claude -- sudo -n {status_helper}")
        machine.succeed(
            "cd /var/lib/private/glowbridge && rm status.json && mv status.real status.json"
        )

    with subtest("a plain ssh command gets the MCP server, not a shell"):
        out = machine.succeed(
            "ssh -i /root/claude-key -o StrictHostKeyChecking=no"
            " -o UserKnownHostsFile=/dev/null claude@127.0.0.1 id </dev/null || true"
        )
        assert "uid=" not in out, out

    with subtest("every tool works over ssh + MCP"):
        print(machine.succeed("${client} /root/claude-key ${secret}"))
  '';
}
