import argparse
import logging
import os
import platform
import re
import shutil
import sys
import textwrap
import time
from glob import glob
from pathlib import Path
import asyncio

import sh
from sh import apt, dpkg, ping, sed, snap, ssh_keygen, systemctl

apti = apt.bake("install", "-y")


def calculate_ob_num():
    """Calculates Orange Box number based off of hostname.

    Expects hostname to be in the form of OrangeBoxXX, where XX
    is a number divisible by 4.
    """
    hostname = platform.node()
    matches = re.search(r"(\d+)$", hostname)
    try:
        return int(matches.groups()[0])
    except Exception as err:
        print(f"Couldn't parse hostname {hostname}: {err}")
        sys.exit(1)


def setup_networking(ob_num: int):
    """Sets up networking.

    Installs some debs manually that are required for configuring networking,
    since we don't yet have networking and can't apt install them. Then configures
    DNS and network interfaces, then bounces the network interfaces.
    """
    print(" === Setting up networking === ")

    # Install network management packages manually via dpkg, since we can't apt
    # install them without networking already setup.
    print("Installing dependencies for setting up networking...")
    dpkg("-i", *glob("./*.deb"))

    print("Configuring resolved...")
    # Default to the Mikrotik router for DNS, with a fallback of Google's DNS
    sed("-i", "s/^#DNS=$/DNS=172.27.31.254 8.8.8.8/g", "/etc/systemd/resolved.conf")
    sed("-i", "s/^#FallbackDNS=$/FallbackDNS=8.8.8.8 8.8.4.4/g", "/etc/systemd/resolved.conf")
    systemctl("restart", "systemd-resolved")

    # Not sure if still used, but some things might be expecting orange boxen to
    # have this configuration file.
    with open("/etc/orange-box.conf", "w") as f:
        f.writelines([f"orangebox_number={ob_num}"])

    # Disable the external ethernet port (en*) and use both of the internal
    # ones (enx*). The enx* interfaces map to vlan1 and vlan2, which in turn
    # get mapped to `172.27.{orange box #}.X` and `172.27.{orange box # + 2}.X`,
    # respectively. They are both bridged to the wireless network that the
    # orange box is connected to, hence not needing en* connected.
    print("Writing network configuration...")
    interfaces = list(
        sorted(
            Path(iface).name
            for iface in glob("/sys/class/net/*")
            if Path(iface).name.startswith("en")
        )
    )
    internal_ips = [f"172.27.{ob_num}.1", f"172.27.{ob_num + 2}.1"]
    gateway_ips = [f"172.27.{ob_num + 1}.254", f"172.27.{ob_num + 3}.254"]
    sh.ip("addr", "flush", "dev", interfaces[1])
    sh.ifconfig(interfaces[1], f"{internal_ips[1]}/23")
    systemctl("stop", "NetworkManager")
    systemctl("disable", "NetworkManager")

    with open("/etc/network/interfaces", "w") as f:
        f.write(
            textwrap.dedent(
                f"""
            # These are generated by orange-box build scripts
            auto lo
            iface lo inet loopback

            auto {interfaces[0]}
            iface {interfaces[0]} inet manual

            auto {interfaces[1]}
            iface {interfaces[1]} inet manual

            auto {interfaces[2]}
            iface {interfaces[2]} inet manual

            auto br0
            iface br0 inet static
              address {internal_ips[0]}
              netmask 255.255.254.0
              gateway {gateway_ips[0]}
              dns-nameservers {internal_ips[0]} {gateway_ips[0]}
              bridge_ports {interfaces[1]}
              bridge_stp off
              bridge_fd 0
              bridge_maxwait 0

            auto br1
            iface br1 inet static
              address {internal_ips[1]}
              netmask 255.255.254.0
              bridge_ports {interfaces[2]}
              bridge_stp off
              bridge_fd 0
              bridge_maxwait 0"""
            )
        )

    print("Restarting network interfaces...")
    bridges = ["br0", "br1"]

    # Take down all of the interfaces
    for iface in interfaces + bridges:
        sh.ifdown("--force", iface)

    # Bring up all interfaces except enp*
    for iface in interfaces[1:] + bridges:
        sh.ifup("--force", iface)

    print("Waiting for network to come up...")
    for _ in range(60):
        try:
            ping("-c1", "8.8.8.8")
            break
        except sh.ErrorReturnCode_1:
            print(" - Still waiting for 8.8.8.8...")
    else:
        print("Waited too long for network to come up.")
        print("Please fix the network.")
        sys.exit(1)

    print("Waiting for DNS to come up...")
    for _ in range(60):
        try:
            ping("-c1", "launchpad.net")
            break
        except (sh.ErrorReturnCode_1, sh.ErrorReturnCode_2):
            print(" - Still waiting for launchpad.net...")
    else:
        print("Waited too long for DNS to come up.")
        print("Please fix the DNS.")
        sys.exit(1)


def setup_apt():
    """Add universe repo and update.

    Some packages such as openssh-server are only in universe.
    """
    print("\n === Setting up apt === ")
    sh.apt_add_repository("universe")
    apt("update")


def setup_ssh(ssh_key: str):
    print("\n === Setting up SSH === ")
    apti("openssh-server")

    Path("/home/ubuntu/.ssh").mkdir(mode=0o700, exist_ok=True)
    shutil.chown("/home/ubuntu/.ssh", "ubuntu")

    if not Path("/home/ubuntu/.ssh/id_rsa").exists():
        print("Generating SSH key...")
        ssh_keygen(
            "-t", "rsa", "-N", "", "-f", "/home/ubuntu/.ssh/id_rsa", _uid=os.getuid()
        )
        shutil.chown("/home/ubuntu/.ssh/id_rsa", "ubuntu")
        shutil.chown("/home/ubuntu/.ssh/id_rsa.pub", "ubuntu")

    print("Importing public launchpad key...")
    sh.ssh_import_id(ssh_key)


def setup_maas(ob_num: int, ssh_key: str):
    print("\n === Setting up MaaS === ")

    print("Installing PostgreSQL for MaaS...")
    apti("postgresql")

    try:
        sh.sudo(
            "-u", "postgres", "psql", "-c", "CREATE USER maas WITH PASSWORD 'foobar';"
        )
    except sh.ErrorReturnCode_1:
        print("PostgreSQL user maas already exists.")

    try:
        sh.sudo("-u", "postgres", "createdb", "-O", "maas", "maasdb")
    except sh.ErrorReturnCode_1:
        print("PostgreSQL database maasdb already exists.")

    print("Installing dependencies...")
    apti("python3-libmaas")
    snap("install", "maas")

    MAAS_URL = f"http://172.27.{ob_num}.1:5240/MAAS/"

    print("Initializing MaaS...")
    sh.maas(
        "init",
        "--mode=region+rack",
        f"--maas-url={MAAS_URL}",
        "--admin-username=admin",
        "--admin-password=admin",
        "--admin-email=admin@example.com",
        "--admin-ssh-import=lp:knkski",
        "--database-host=localhost",
        "--database-name=maasdb",
        "--database-user=maas",
        "--database-pass=foobar",
        "--database-port=5432",
        _in="no",
    )

    print("Setting up MaaS admin user...")
    try:
        apikey = sh.maas("apikey", "--user", "admin").strip()
    except sh.ErrorReturnCode_1:
        print("Creating MaaS admin user...")
        sh.maas(
            "createadmin",
            "--username=admin",
            "--password=admin",
            "--email=admin@example.com",
            f"--ssh-import={ssh_key}",
        )
        apikey = sh.maas("apikey", "--user", "admin").strip()
    sh.maas("login", "admin", MAAS_URL, apikey)

    from maas.client import connect
    from maas.client.enum import IPRangeType

    client = connect(url=MAAS_URL, apikey=apikey)

    print("Creating ip_ranges...")
    if not client.ip_ranges.list():
        client.ip_ranges.create(
            type=IPRangeType.DYNAMIC,
            start_ip=f"172.27.{ob_num+1}.1",
            end_ip=f"172.27.{ob_num+1}.20",
        )

    print("Setting up VLANs...")
    controller = client.rack_controllers.list()[0]
    for subnet in client.subnets.list():
        if subnet.cidr == f"172.27.{ob_num}.0/23":
            vlan = subnet.vlan
            vlan.dhcp_on = True
            vlan.primary_rack = controller
            vlan.save()

    print("Configuring MaaS...")
    client.maas.set_upstream_dns([f"172.27.{ob_num + 1}.254"])
    client.maas.set_dnssec_validation(client.maas.DNSSEC.NO)
    client.maas.set_kernel_options("net.ifnames=0")

    def create(existing, desired, keys=("name",)):
        existing_keys = set(tuple(getattr(e, key) for key in keys) for e in existing)
        to_create = [
            o for o in desired if tuple(o[key] for key in keys) not in existing_keys
        ]

        for obj in to_create:
            existing.__class__.create(**obj)

    print("Adding SSH key...")
    ubuntu_key = Path("/home/ubuntu/.ssh/id_rsa.pub").read_text().strip()
    create(
        client.ssh_keys.list(), [{"key": ubuntu_key}], ("key", )
    )

    print("Creating boot source selections...")
    boot_source = client.boot_sources.list()[0]

    create(
        client._origin.BootSourceSelections.read(boot_source),
        [
            {
                "os": "ubuntu",
                "release": "focal",
                "arches": "amd64",
                "subarches": "*",
                "labels": "*",
                "boot_source": boot_source,
            },
            {
                "os": "ubuntu",
                "release": "bionic",
                "arches": "amd64",
                "subarches": "*",
                "labels": "*",
                "boot_source": boot_source,
            },
        ],
        ("os", "release"),
    )

    print("Creating zones...")
    create(
        client.zones.list(),
        [
            {"name": "zone1", "description": "Physical machines 1-5"},
            {"name": "zone2", "description": "Physical machines 6-10"},
        ],
    )

    print("Creating tags...")
    create(
        client.tags.list(), [{"name": "physical"}, {"name": "use-fastpath-installer"}],
    )

    print("Starting boot resource importing...")
    client.boot_resources.start_import()

    while True:
        if not client._origin.session.BootResources.is_importing():
            break
        print(" - Waiting for boot resources importing...")
        time.sleep(15)


async def setup_maas_nodes(ob_num: int):
    from maas.client import connect

    MAAS_URL = f"http://172.27.{ob_num}.1:5240/MAAS/"
    apikey = sh.maas("apikey", "--user", "admin").strip()

    client = await connect(url=MAAS_URL, apikey=apikey)
    machines = await client.machines.list()
    zones = [await client.zones.get("zone1"), await client.zones.get("zone2")]
    existing_hostnames = [m.hostname for m in machines]
    tags = [
        await client.tags.get("physical"),
        await client.tags.get("use-fastpath-installer"),
    ]

    async def run(cmd):
        p = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await p.communicate()
        exit_status = await p.wait()

        if exit_status != 0:
            raise Exception(f"Got exit status {exit_status} for command {cmd}")
        return stdout.decode("utf-8")

    async def setup_node(num: int):
        amtnum = num + 10
        amt_ip = f"172.27.{ob_num}.{amtnum}"
        hostname = f"node{num:02}ob{ob_num}"
        print(f"Setting up {hostname}...")

        try:
            await run(f"ping -c1 {amt_ip}")
        except Exception:
            print(f"Couldn't contact AMT for {hostname}!")
            return

        neighbor = await run(f"ip neighbor show {amt_ip}")
        mac = neighbor.split(" ")[-2]

        if hostname in existing_hostnames:
            machine = next(m for m in machines if m.hostname == hostname)
        else:
            machine = await client.machines.create(
                architecture="amd64",
                mac_addresses=[mac],
                power_type="amt",
                power_parameters={"power_address": amt_ip, "power_pass": "Password1+"},
                hostname=hostname,
            )

        await machine.set_power(
            "amt", {"power_address": amt_ip, "power_pass": "Password1+"}
        )
        for tag in tags:
            await machine.tags.add(tag)

        machine.zone = zones[num // 6]
        await machine.save()
        print(f"Finished setting up {hostname}...")

    await asyncio.wait([setup_node(i) for i in range(1, 11)])


def setup_niceties():
    # Install some convenient packages
    apti('vim', 'curl', 'fish', 'screen', 'python3-is-python')

    # Don't require password for sudo
    with open('/etc/sudoers.d/ubuntu', 'w') as f:
        f.write('%sudo   ALL=(ALL:ALL) ALL\n')


def main():
    parser = argparse.ArgumentParser(description="Setup Orange Box")
    parser.add_argument("--debug", dest="debug", action="store_true")
    parser.add_argument("--no-debug", dest="debug", action="store_false")
    parser.set_defaults(feature=True)
    parser.add_argument(
        "--ob-num", type=int, help="This Orange Box number (e.g. `4` or `56`)"
    )
    parser.add_argument(
        "--ssh-key",
        default="lp:knkski",
        help="Public SSH key to import from launchpad for remote access, e.g. `lp:username`",
    )

    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.INFO)

    if args.ob_num:
        ob_num = args.ob_num
    else:
        print("No Orange Box number passed in, calculating it from hostname...")
        ob_num = calculate_ob_num()

    with sh.contrib.sudo:
        setup_networking(ob_num)
        setup_apt()
        setup_niceties()
        setup_ssh(args.ssh_key)
        setup_maas(ob_num, args.ssh_key)
        asyncio.run(setup_maas_nodes(ob_num))


if __name__ == "__main__":
    main()
