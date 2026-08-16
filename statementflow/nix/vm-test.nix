# nixosTest that boots the statementflow module in a throwaway VM and asserts
# the packaging, systemd wiring, loopback-only bind and identity gate are sound
# -- without any real config or tailscale. tailscaleServe is off here (no
# tailscaled in the VM); the app itself is what we exercise.
{ testers, statementflowModule }:

testers.runNixOSTest {
  name = "statementflow";

  nodes.machine = { pkgs, ... }: {
    imports = [ statementflowModule ];

    services.statementflow = {
      enable = true;
      tailscaleServe = false;
      port = 8770;
    };

    environment.systemPackages = [ pkgs.curl pkgs.iproute2 ];
  };

  testScript = ''
    machine.wait_for_unit("statementflow.service")
    machine.succeed("test -d /var/lib/statementflow")

    # The app answers on loopback, and healthz needs no identity.
    machine.wait_until_succeeds("curl -sf http://127.0.0.1:8770/healthz", timeout=60)
    machine.succeed("curl -sf http://127.0.0.1:8770/healthz | grep -q '\"status\":\"ok\"'")

    # Bound to 127.0.0.1 ONLY -- never a wildcard address. This is the invariant
    # the whole trust model rests on (tailscale serve is the sole ingress).
    machine.succeed("ss -ltn | grep -q '127.0.0.1:8770'")
    machine.fail("ss -ltn | grep -qE '(0.0.0.0|\\*|\\[::\\]):8770'")

    # Identity gate: STATEMENTFLOW_REQUIRE_IDENTITY=1, so a request without the
    # Tailscale-User-Login header (i.e. one that did not come through serve) is
    # rejected, and one with it is allowed.
    machine.succeed(
        "test $(curl -s -o /dev/null -w '%{http_code}' "
        "http://127.0.0.1:8770/range) = 403"
    )
    machine.succeed(
        "test $(curl -s -o /dev/null -w '%{http_code}' "
        "-H 'Tailscale-User-Login: someone@example.com' "
        "http://127.0.0.1:8770/range) = 200"
    )
  '';
}
