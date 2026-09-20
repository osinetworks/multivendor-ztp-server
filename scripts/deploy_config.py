#!/usr/bin/env python3

import os
import sys
import getpass
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException

# Anchored to this script's own directory, where device.lst and
# device_config.cfg actually live — they used to be resolved against the
# caller's cwd, so running this from the repo root always failed.
HERE = os.path.dirname(os.path.abspath(__file__))
DEVICE_LIST_FILE = os.path.join(HERE, "device.lst")
CONFIG_FILE = os.path.join(HERE, "device_config.cfg")

def main():
    if not os.path.exists(DEVICE_LIST_FILE):
        print(f"Error: Device list file '{DEVICE_LIST_FILE}' not found.")
        sys.exit(1)

    if not os.path.exists(CONFIG_FILE):
        print(f"Error: Configuration file '{CONFIG_FILE}' not found.")
        sys.exit(1)

    # Read IP addresses from the device list file
    with open(DEVICE_LIST_FILE, 'r') as f:
        ips = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    if not ips:
        print(f"No IP addresses found in '{DEVICE_LIST_FILE}'.")
        sys.exit(1)

    # Prompt for credentials
    print("Please enter your switch credentials.")
    username = input("Username: ")
    password = getpass.getpass("Password: ")
    
    # Optional enable secret (assume same as password if not provided)
    # enable_password = getpass.getpass("Enable Password (press Enter if same as login password): ")
    # if not enable_password:
    #     enable_password = password
    enable_password = password

    print(f"\nStarting configuration deployment to {len(ips)} devices...\n")

    for ip in ips:
        print(f"[{ip}] Connecting...")
        device = {
            'device_type': 'arista_eos',
            'host': ip,
            'username': username,
            'password': password,
            'secret': enable_password,
            'global_delay_factor': 2, # Helpful for slightly slower ZTP VMs
        }

        try:
            # Connect to the device
            net_connect = ConnectHandler(**device)
            print(f"[{ip}] Connected successfully.")

            # Enter enable mode
            net_connect.enable()
            print(f"[{ip}] Entered enable mode.")

            # Send configuration from file
            # Note: send_config_from_file automatically handles 'conf term' and 'end'
            print(f"[{ip}] Applying configuration from '{CONFIG_FILE}'...")
            output = net_connect.send_config_from_file(CONFIG_FILE)
            
            # Print the output of the configuration application
            for line in output.splitlines():
                if line.strip():
                    print(f"[{ip}] {line}")

            # Save the configuration (wr mem)
            print(f"[{ip}] Saving configuration (wr mem)...")
            save_output = net_connect.save_config()
            print(f"[{ip}] Configuration saved.")

            # Disconnect
            net_connect.disconnect()
            print(f"[{ip}] Disconnected.\n")

        except NetmikoAuthenticationException:
            print(f"[{ip}] Error: Authentication failed. Check your username/password.\n")
        except NetmikoTimeoutException:
            print(f"[{ip}] Error: Connection timed out. Is the device reachable?\n")
        except Exception as e:
            print(f"[{ip}] An unexpected error occurred: {e}\n")

    print("Deployment script finished.")

if __name__ == "__main__":
    main()
