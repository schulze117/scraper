"""GCP proxy pool — test whether a Google Cloud exit IP clears immoscout's WAF.

Spins up N spot e2-micro VMs in Frankfurt, each running TinyProxy on :8888,
probes each one through the *real* fetch layer (the same challenge-clearing +
bot-detection the finder uses), and reports the first proxy that clears the WAF.
Probe-only for now: the winner is torn down too — we're only answering "does a
GCP IP work?", not keeping one for a scrape.

Teardown is belt-and-suspenders because leaked VMs cost money:
  1. orphan sweep before starting (kills leftovers from a crashed run)
  2. explicit delete of the whole pool in a finally / atexit / signal handler
  3. an on-VM self-destruct watchdog (see STARTUP_SCRIPT) as the last net for a
     hard-killed process — this is the part that was broken before: the old
     watchdog shelled out to `gcloud`, which is NOT installed on debian-12, so
     it never fired and VMs were left STOPPED. This one calls the Compute API
     directly with the metadata-server token, no gcloud needed.

Usage:
    python -m lib.gcp_proxy --probe [--n 10] [--url <immoscout URL>]
    python -m lib.gcp_proxy --cleanup        # delete every pool VM right now
    python -m lib.gcp_proxy --list           # list instances (read-only)
"""
import argparse
import atexit
import concurrent.futures
import signal
import socket
import sys
import time
from pathlib import Path

import requests
from google.cloud import compute_v1
from google.oauth2 import service_account

from lib.config import get_config
from lib.logger import get_logger

logger = get_logger("gcp_proxy")
config = get_config()

# --- Configuration ---
PROJECT_ID = "immofinder-438008"
ZONE = "europe-west3-c"                    # Frankfurt — matches our DE geo/tz stealth
MACHINE_TYPE = "e2-micro"
PROXY_PORT = 8888
NAME_PREFIX = "gcp-proxy"                   # distinct from the old "dynamic-proxy"
FIREWALL_RULE_NAME = "allow-dynamic-proxy-access"
PROXY_TAG = "http-proxy-server"
# Key file lives at the repo root (same one the workflows write from the secret).
KEY_PATH = str(Path(__file__).resolve().parent.parent / "immofinder-438008-411bf1440a6c.json")

# How long the on-VM watchdog waits with no :8888 traffic before self-deleting.
# Generous so it never fires mid-probe (Python teardown is the fast primary path);
# it only matters when the Python process is hard-killed. e2-micro spot is a few
# tenths of a cent/hour, so a longer net is cheap insurance.
WATCHDOG_IDLE_LIMIT = 1800  # 30 minutes

STARTUP_SCRIPT = f"""#! /bin/bash
set -e
# --- TinyProxy: open HTTP proxy on :{PROXY_PORT}, security is the GCP firewall ---
apt-get update
apt-get install -y tinyproxy curl python3
sed -i "s/^Allow /#Allow /" /etc/tinyproxy/tinyproxy.conf
echo "Allow 0.0.0.0/0" >> /etc/tinyproxy/tinyproxy.conf
echo "DisableViaHeader Yes" >> /etc/tinyproxy/tinyproxy.conf
systemctl restart tinyproxy

# --- Self-destruct watchdog (no gcloud: debian-12 doesn't ship it) ---
cat << 'WATCHDOG' > /usr/local/bin/idle-checker.sh
#!/bin/bash
IDLE_LIMIT={WATCHDOG_IDLE_LIMIT}
CHECK_INTERVAL=30
IDLE_TIMER=0
META="http://metadata.google.internal/computeMetadata/v1"
HDR="Metadata-Flavor: Google"
ZONE=$(curl -s -H "$HDR" "$META/instance/zone" | awk -F/ '{{print $NF}}')
NAME=$(curl -s -H "$HDR" "$META/instance/name")
PROJECT=$(curl -s -H "$HDR" "$META/project/project-id")
while true; do
  if ss -tn state established sport = :{PROXY_PORT} | grep -q .; then
    IDLE_TIMER=0
  else
    IDLE_TIMER=$((IDLE_TIMER + CHECK_INTERVAL))
  fi
  if [ "$IDLE_TIMER" -ge "$IDLE_LIMIT" ]; then
    TOKEN=$(curl -s -H "$HDR" "$META/instance/service-accounts/default/token" \\
      | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
    CODE=$(curl -s -o /dev/null -w "%{{http_code}}" -X DELETE \\
      -H "Authorization: Bearer $TOKEN" \\
      "https://compute.googleapis.com/compute/v1/projects/$PROJECT/zones/$ZONE/instances/$NAME")
    echo "self-delete DELETE returned $CODE"
    # If the API delete was refused for any reason, at least stop paying for CPU.
    if [ "$CODE" != "200" ]; then shutdown -h now; fi
    exit 0
  fi
  sleep $CHECK_INTERVAL
done
WATCHDOG
chmod +x /usr/local/bin/idle-checker.sh
nohup /usr/local/bin/idle-checker.sh > /var/log/idle-checker.log 2>&1 &
"""


class GcpProxyManager:
    def __init__(self, n: int = 10, zone: str = ZONE):
        creds = service_account.Credentials.from_service_account_file(KEY_PATH)
        self.instances = compute_v1.InstancesClient(credentials=creds)
        self.firewalls = compute_v1.FirewallsClient(credentials=creds)
        self.n = n
        self.zone = zone
        self.created: list[str] = []          # names we've issued a create for
        self._cleanup_registered = False

    # --- lifecycle / cleanup ------------------------------------------------

    def _register_cleanup(self):
        """Ensure the pool is torn down on normal exit, unhandled error, and
        Ctrl-C / SIGTERM. (A hard SIGKILL is caught only by the on-VM watchdog.)"""
        if self._cleanup_registered:
            return
        atexit.register(self.delete_all)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                pass  # not in main thread / unsupported on this platform
        self._cleanup_registered = True

    def _on_signal(self, signum, frame):
        logger.warning(f"Signal {signum} received — tearing down proxy pool.")
        self.delete_all()
        raise SystemExit(1)

    def sweep_orphans(self):
        """Delete any pre-existing pool VMs (any zone) left by a crashed run."""
        names = []
        for zone, scoped in self.instances.aggregated_list(project=PROJECT_ID):
            for i in getattr(scoped, "instances", []) or []:
                if i.name.startswith(NAME_PREFIX):
                    names.append((zone.split("/")[-1], i.name))
        if names:
            logger.info(f"Sweeping {len(names)} orphaned pool VM(s) before start.")
            ops = [self.instances.delete(project=PROJECT_ID, zone=z, instance=n) for z, n in names]
            for op in ops:
                _wait(op)

    def delete_all(self):
        """Delete every VM this manager created. Idempotent; safe to call twice."""
        if not self.created:
            return
        names = list(self.created)
        logger.info(f"Deleting {len(names)} pool VM(s): {', '.join(names)}")
        ops = []
        for name in names:
            try:
                ops.append(self.instances.delete(project=PROJECT_ID, zone=self.zone, instance=name))
            except Exception as e:
                logger.warning(f"delete({name}) failed to submit: {e}")
        for op in ops:
            try:
                _wait(op)
            except Exception as e:
                logger.warning(f"delete op did not complete cleanly: {e}")
        self.created = []

    # --- firewall -----------------------------------------------------------

    def whitelist_self(self):
        """Allow this machine's public IP to reach the proxies on :8888,
        preserving any IPs already on the rule."""
        my_ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
        cidr = f"{my_ip}/32"
        logger.info(f"Whitelisting {my_ip} on firewall '{FIREWALL_RULE_NAME}'.")
        try:
            rule = self.firewalls.get(project=PROJECT_ID, firewall=FIREWALL_RULE_NAME)
        except Exception:
            rule = None
        if rule is None:
            resource = {
                "name": FIREWALL_RULE_NAME,
                "direction": "INGRESS",
                "priority": 1000,
                "network": "global/networks/default",
                "allowed": [{"I_p_protocol": "tcp", "ports": [str(PROXY_PORT)]}],
                "source_ranges": [cidr],
                "target_tags": [PROXY_TAG],
                "description": "Auto-generated rule for the GCP proxy pool.",
            }
            _wait(self.firewalls.insert(project=PROJECT_ID, firewall_resource=resource))
        elif cidr not in rule.source_ranges:
            ranges = list(rule.source_ranges) + [cidr]
            _wait(self.firewalls.patch(
                project=PROJECT_ID, firewall=FIREWALL_RULE_NAME,
                firewall_resource=compute_v1.Firewall(source_ranges=ranges),
            ))

    # --- instances ----------------------------------------------------------

    def create_pool(self) -> list[tuple[str, str]]:
        """Create N proxies concurrently. Returns [(name, ip), ...] for the ones
        that came up. Names are recorded for teardown as soon as create is issued,
        so a partial failure still gets cleaned up."""
        logger.info(f"Creating pool of {self.n} proxies in {self.zone} ...")
        ready: list[tuple[str, str]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.n) as ex:
            futures = {ex.submit(self._create_vm, i): i for i in range(1, self.n + 1)}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    ready.append(fut.result())
                except Exception as e:
                    logger.warning(f"proxy #{futures[fut]} failed to come up: {e}")
        logger.info(f"{len(ready)}/{self.n} proxies ready.")
        return ready

    def _create_vm(self, idx: int) -> tuple[str, str]:
        name = f"{NAME_PREFIX}-{idx}"
        self.created.append(name)  # record before the call so teardown catches it
        resource = {
            "name": name,
            "machine_type": f"projects/{PROJECT_ID}/zones/{self.zone}/machineTypes/{MACHINE_TYPE}",
            "scheduling": {
                "provisioning_model": "SPOT",
                "preemptible": True,
                "automatic_restart": False,
                "instance_termination_action": "DELETE",
            },
            "disks": [{
                "boot": True,
                "auto_delete": True,
                "initialize_params": {
                    "source_image": "projects/debian-cloud/global/images/family/debian-12",
                    "disk_size_gb": 10,
                },
            }],
            "network_interfaces": [{
                "network": "global/networks/default",
                # proto-plus renames the reserved word `type` -> `type_`
                "access_configs": [{"name": "External NAT", "type_": "ONE_TO_ONE_NAT"}],
            }],
            "metadata": {"items": [{"key": "startup-script", "value": STARTUP_SCRIPT}]},
            "tags": {"items": [PROXY_TAG]},
            # Default compute SA + cloud-platform scope so the on-VM watchdog can
            # call the Compute API to delete itself.
            "service_accounts": [{
                "email": "default",
                "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
            }],
        }
        _wait(self.instances.insert(project=PROJECT_ID, zone=self.zone, instance_resource=resource))
        ip = self._get_ip(name)
        self._wait_for_proxy(ip)
        logger.info(f"[{name}] ready at {ip}:{PROXY_PORT}")
        return name, ip

    def _get_ip(self, name: str) -> str:
        vm = self.instances.get(project=PROJECT_ID, zone=self.zone, instance=name)
        return vm.network_interfaces[0].access_configs[0].nat_i_p

    def _wait_for_proxy(self, ip: str, retries: int = 90):
        """Poll :8888 until TinyProxy accepts connections (startup-script finished)."""
        for _ in range(retries):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            try:
                if sock.connect_ex((ip, PROXY_PORT)) == 0:
                    return
            finally:
                sock.close()
            time.sleep(2)
        raise TimeoutError(f"proxy at {ip}:{PROXY_PORT} never opened")


def _wait(operation):
    """Block on a GCP operation and surface its error."""
    operation.result()
    if getattr(operation, "error", None):
        raise RuntimeError(f"GCP operation error: {operation.error}")


def _probe_url() -> str:
    """A representative immoscout search URL, built from the configured shape."""
    shape = config.finder.locations.immoscout[0]
    return (f"https://www.immobilienscout24.de/Suche/shape/wohnung-kaufen"
            f"?shape={shape}&enteredFrom=result_list&sorting=2")


def probe_proxy(ip: str, url: str) -> bool:
    """Fetch `url` through the proxy via the real fetch layer. True = the WAF
    cleared and we got real content; False = blocked or the fetch failed."""
    from lib.fetch._seleniumbase import get_html_seleniumbase
    from lib.helpers import has_bot_detection

    proxy_url = f"http://{ip}:{PROXY_PORT}"
    try:
        html = get_html_seleniumbase(url, proxy_url=proxy_url, on_block="return")
    except Exception as e:
        logger.warning(f"probe via {ip} errored: {e}")
        return False
    blocked = has_bot_detection(html)
    logger.info(f"probe via {ip}: {'BLOCKED' if blocked else 'CLEARED'} (len {len(html)})")
    return not blocked


def run_probe(n: int, url: str | None) -> str | None:
    """Spin up the pool, probe sequentially, return the first working IP (or None).
    Always tears the whole pool down before returning."""
    url = url or _probe_url()
    mgr = GcpProxyManager(n=n)
    mgr._register_cleanup()
    winner = None
    try:
        mgr.sweep_orphans()
        mgr.whitelist_self()
        pool = mgr.create_pool()
        if not pool:
            logger.error("No proxies came up — cannot probe.")
            return None
        logger.info(f"Probing {len(pool)} proxies against:\n  {url}")
        for name, ip in pool:
            if probe_proxy(ip, url):
                winner = ip
                logger.info(f"WINNER: {name} @ {ip} cleared the WAF.")
                break
        else:
            logger.info("No proxy in the pool cleared the WAF.")
    finally:
        mgr.delete_all()
        _confirm_empty(mgr)
    return winner


def _confirm_empty(mgr: GcpProxyManager):
    """Prove nothing is left running — the real 'make sure they shut down'."""
    try:
        left = [i.name for _, s in mgr.instances.aggregated_list(project=PROJECT_ID)
                for i in (getattr(s, "instances", []) or []) if i.name.startswith(NAME_PREFIX)]
        if left:
            logger.warning(f"STILL RUNNING after teardown: {left} — retrying delete.")
            mgr.created = left
            mgr.delete_all()
        else:
            logger.info("Confirmed: no pool VMs left running.")
    except Exception as e:
        logger.warning(f"Could not confirm teardown: {e}")


def cleanup_all():
    """Delete every pool VM anywhere in the project, right now."""
    mgr = GcpProxyManager()
    mgr.sweep_orphans()
    _confirm_empty(mgr)


def list_instances():
    mgr = GcpProxyManager()
    rows = [(z.split("/")[-1], i.name, i.status)
            for z, s in mgr.instances.aggregated_list(project=PROJECT_ID)
            for i in (getattr(s, "instances", []) or [])]
    print(f"{len(rows)} instance(s):")
    for z, name, status in rows:
        print(f"  {name:<20} {z:<16} {status}")


def main() -> int:
    ap = argparse.ArgumentParser(description="GCP proxy pool for WAF testing.")
    ap.add_argument("--probe", action="store_true", help="Spin up, find a working proxy, tear down.")
    ap.add_argument("--cleanup", action="store_true", help="Delete all pool VMs and exit.")
    ap.add_argument("--list", action="store_true", help="List instances (read-only).")
    ap.add_argument("--n", type=int, default=10, help="Pool size (default 10).")
    ap.add_argument("--url", default=None, help="Probe URL (default: immoscout search from config).")
    args = ap.parse_args()

    if args.cleanup:
        cleanup_all()
        return 0
    if args.list:
        list_instances()
        return 0
    if args.probe:
        winner = run_probe(args.n, args.url)
        print("\n" + "=" * 60)
        print(f"RESULT: {'working GCP proxy IP = ' + winner if winner else 'NO working GCP proxy found'}")
        print("=" * 60)
        return 0 if winner else 2
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
