#!/bin/bash
set -eux

# If user is not root then exit
#
if [ "$(id -u)" != "0" ]; then
  echo "Must be run with sudo or by root"
  exit 77
fi

# Install a few utils used in the script to set up networking
dpkg -i ./*.deb

# Configure systemd-resolved to use google's DNS
echo "DNS=8.8.8.8 8.8.4.4" >> /etc/systemd/resolved.conf
sudo systemctl restart systemd-resolved

#Get Orangebox number from the hostname and place into the configuration file
obnum=28
echo "orangebox_number=${obnum}" > /etc/orange-box.conf

# Get interface names of the 3 interfaces on node0 since in Xenial they aren't ethX anymore
# An array is declared and the interface names are placed into the array to be used later on
#
declare interface=()

for inter_face in $(ip a | awk '{print $2}'|egrep 'enp|enx'|sed 's/://')
do
   echo "Interface read $inter_face"
   interface=("${interface[@]}" "$inter_face")
done
echo "Interfaces assigned "${interface[@]}""

# Check to make sure the OrangeBox is divisable by 4 to ensure the network is setup correctly
#
check_orangebox_number() {
	local num=$1
	if [[ $((num/4)) -lt 1 ]]; then
		echo "Your hostname should in the format of OrangeBox??: ex OrangeBox56"
		exit 1;
	fi
}

#Add kernel parameters for networking with MAAS to function correctly
#
echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
echo "net.ipv4.conf.all.accept_redirects = 1" >> /etc/sysctl.conf

# Assign variables with the values for the network setup and execute the check_orangebox_number
#
. /etc/orange-box.conf

check_orangebox_number ${obnum}
internal1_ip="172.27.$((orangebox_number)).1"
gateway1_ip=172.27.$((orangebox_number+1)).254
internal2_ip=172.27.$((orangebox_number+2)).1
gateway2_ip=172.27.$((orangebox_number+3)).254
gateway_ip=$gateway1_ip

# Set up the nic variables
internal0_if="${interface[0]}"
internal1_if="${interface[1]}"
internal2_if="${interface[2]}"

# check with i/f is on which bridge-vlan
ip addr flush dev ${internal1_if}
ifconfig ${internal1_if} ${internal1_ip}/23
internal1_if="${interface[2]}"
internal2_if="${interface[1]}"
# internal1_if="${interface[1]}"
# internal2_if="${interface[2]}"

# Setup the network interfaces for Node0 and populate the /etc/network/networking file with the correct
# information
#
setup_networking() {
	# Disable NetworkManager 
	systemctl stop NetworkManager
	systemctl disable NetworkManager
		
	# gen network configuration /etc/network/interfaces
        cat >/etc/network/interfaces <<-EOF
	#These are generated by orange-box build scripts
	auto lo
	iface lo inet loopback

	auto $internal0_if
	iface $internal0_if inet manual

	auto $internal1_if
	iface $internal1_if inet manual

	auto $internal2_if
	iface $internal2_if inet manual

	auto br0
	iface br0 inet static
	    address ${internal1_ip}
	    netmask 255.255.254.0
	    gateway ${gateway_ip}
	    dns-nameservers ${internal1_ip} ${gateway_ip}
	    bridge_ports $internal1_if
	    bridge_stp off
	    bridge_fd 0
	    bridge_maxwait 0

	auto br1
	iface br1 inet static
	    address ${internal2_ip}
	    netmask 255.255.254.0
	    bridge_ports $internal2_if
	    bridge_stp off
	    bridge_fd 0
	    bridge_maxwait 0
EOF

        # Take down all of the interfaces
        ifdown --force $internal0_if || true
	ifdown --force $internal1_if || true
	ifdown --force $internal2_if || true

	# Take down br interfaces
	ifdown --force br0 || true
	ifdown --force br1 || true

	# Bring up br0, br1
	ifup $internal1_if --force
	ifup $internal2_if --force
	ifup br0 --force
        ifup br1 --force

        # Wait a moment for the network to normalize
	echo "INFO: Ensure networking has settled"
        if ping -c 3 8.8.8.8
        then
             echo ""
             echo "Networking is fine"
             echo ""
        else
             echo ""
             echo "You're having network issues, fix them"
             echo ""
             exit 1
        fi

        # Confirm DNS working
        echo "INFO: Ensure DNS working"
        if ping -c 3 google.com
        then
             echo ""
             echo "DNS is fine"
             echo ""
        else
             echo ""
             echo "DNS is having issues, check /etc/resolv.conf and ifdown/ifup br0/br1 interfaces if need be"
             echo ""
             exit 2
        fi
}

setup_networking

exit 0
