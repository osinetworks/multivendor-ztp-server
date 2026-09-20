#!/usr/bin/env python3
import os
import sys
import yaml
import getpass
from datetime import datetime

from netmiko import ConnectHandler

# Paths are anchored to the repo root (this file lives in scripts/), so the
# script works no matter which directory it is run from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVENTORY_FILE = os.path.join(ROOT, "config", "inventory.yaml")
BACKUP_DIR = os.path.join(ROOT, "backups")

COMMANDS = [
    "terminal length 0",
    "show running-config",
    "show interface status",
    "show lldp neighbors",
    "show port-channel dense",
    "show version",
    "show mlag",
    "show mlag interfaces"
]

def load_inventory():
    with open(INVENTORY_FILE, 'r') as f:
        data = yaml.safe_load(f)
    return data.get("switches", [])

def get_ip(ip_with_cidr):
    if not ip_with_cidr:
        return None
    return ip_with_cidr.split('/')[0]

def backup_switch(hostname, ip, username, password, backup_folder):
    print(f"Connecting to {hostname} ({ip}) via netmiko...")
    
    device = {
        'device_type': 'arista_eos',
        'host': ip,
        'username': username,
        'password': password,
        'timeout': 15,
    }
    
    try:
        with ConnectHandler(**device) as net_connect:
            output_data = []
            for cmd in COMMANDS:
                print(f"  [{hostname}] Running: {cmd}")
                # netmiko automatically handles sending the command and waiting for the prompt
                output = net_connect.send_command(cmd, read_timeout=30)
                output_data.append(f"===== {cmd} =====\n{output}")
                
            # Write output to file
            filename = os.path.join(backup_folder, f"{hostname}.txt")
            with open(filename, "w") as f:
                f.write("\n\n".join(output_data))
                
            print(f"✓ Backup saved: {filename}")
            
    except Exception as e:
        print(f"✗ Failed to backup {hostname}: {e}")
        return False
    return True

def main():
    if not os.path.exists(INVENTORY_FILE):
        print(f"Error: {INVENTORY_FILE} not found!")
        return 1

    switches = load_inventory()
    if not switches:
        print("No switches found in inventory.")
        return 1

    # Create backup directory with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_folder = os.path.join(BACKUP_DIR, timestamp)
    os.makedirs(backup_folder, exist_ok=True)
    print(f"Created backup directory: {backup_folder}\n")

    username = input("SSH Username [admin]: ").strip() or "admin"
    password = getpass.getpass(f"SSH Password for {username}: ")

    ok = failed = 0
    for switch in switches:
        # inventory.yaml uses 'hostname'; the template documents 'description'
        hostname = switch.get("hostname") or switch.get("description")
        ip_cidr = switch.get("ip_address")
        if not hostname or not ip_cidr:
            print(f"- Skipping {switch.get('serial','?')}: no hostname/ip_address in inventory")
            continue

        ip = get_ip(ip_cidr)
        if backup_switch(hostname, ip, username, password, backup_folder):
            ok += 1
        else:
            failed += 1

    print(f"\nDone: {ok} backed up, {failed} failed → {backup_folder}")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main() or 0)
