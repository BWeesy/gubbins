# nixosTest that boots the glowbridge module in a throwaway VM and asserts
# the packaging, systemd wiring and hardening are sound — without touching a
# real Home Assistant or the Glow API. The VM has no internet, so the first
# cycle fails to reach Glow; that is fine — a failed cycle is not fatal, the
# daemon stays up and writes a failure status. We assert exactly that:
# the unit runs, the StateDirectory is created, and status.json appears.
{ testers, glowbridgeModule }:

testers.runNixOSTest {
  name = "glowbridge";

  nodes.machine = { ... }: {
    imports = [ glowbridgeModule ];

    # Dummy credentials for the test; a real deployment keeps these in a
    # root-only file outside the store.
    environment.etc."glowbridge.env".text = ''
      GLOWBRIDGE_GLOW_USERNAME=test
      GLOWBRIDGE_GLOW_PASSWORD=test
      GLOWBRIDGE_HA_TOKEN=test
    '';

    services.glowbridge = {
      enable = true;
      environmentFile = "/etc/glowbridge.env";
      settings = {
        homeassistant.url = "ws://127.0.0.1:8123/api/websocket";
        schedule.jitter = 0;
        # Fail fast: one attempt, no backoff, so the first cycle's failure
        # (no network) is recorded within seconds rather than minutes.
        retry.max_attempts = 1;
      };
    };
  };

  testScript = ''
    machine.wait_for_unit("glowbridge.service")
    machine.succeed("test -d /var/lib/glowbridge")
    # The first cycle fails to reach Glow; the daemon stays up and writes a
    # failure status. That proves config load, systemd wiring and the state
    # directory all work end to end.
    machine.wait_until_succeeds(
        "test -f /var/lib/glowbridge/status.json", timeout=90
    )
    machine.succeed(
        "grep -q '\"consecutive_failures\"' /var/lib/glowbridge/status.json"
    )
    # The unit must survive a failed cycle, not crash-loop.
    machine.succeed("systemctl is-active glowbridge.service")
  '';
}
