#!/usr/bin/env python3
# ============================================================
# Generic Switch Config Generator
# Usage: python3 generate_generic_config.py
# ============================================================

from jinja2 import Environment, FileSystemLoader, StrictUndefined
import yaml
import os
import re
import sys
import copy
import ipaddress

# -----------------------------------------------------------
# Path definitions — anchored to the repo root (this file's directory),
# so the script works from any current working directory.
# -----------------------------------------------------------
ROOT           = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR   = os.path.join(ROOT, "templates")
CONFIG_FILE    = os.path.join(ROOT, "config", "config.yaml")
OUTPUT_DIR     = os.path.join(ROOT, "configs")
INVENTORY_FILE = os.path.join(ROOT, "config", "inventory.yaml")

# Which template belongs to which platform. A vars file that hardcodes
# 'template:' must not be able to hand a Cisco switch an Arista config.
PLATFORM_FAMILY = {
    "eos":       "arista",
    "eos64":     "arista",
    "cisco_ios": "cisco",
}


def customer_vars_file(customer: str) -> str:
    """Variable file belonging to a customer: config/config_<customer>.yaml"""
    return os.path.join(ROOT, "config", f"config_{customer}.yaml")


def customer_template(customer: str, family: str) -> str:
    """Template belonging to a customer: <family>_config_template_<customer>.j2"""
    return f"{family}_config_template_{customer}.j2"


def template_family(template_name: str) -> str:
    """Infer the vendor a template belongs to from its filename."""
    name = template_name.lower()
    if "cisco" in name:
        return "cisco"
    if "arista" in name:
        return "arista"
    return "unknown"


# A value the operator was supposed to replace. Shipping one to a switch is
# worse on Cisco than on Arista: EOS rejects 'secret sha512 REPLACE_ME...' as a
# malformed hash, but IOS treats an unrecognised prefix as a CLEARTEXT password
# and happily creates a working privilege-15 account whose password is a string
# published in this repository.
PLACEHOLDER_RE = re.compile(r"^\s*(REPLACE_ME|CHANGE_ME)", re.IGNORECASE)

# Credentials each template family renders. 'required' must be present;
# 'conditional' is required only when custom_username is set.
CREDENTIAL_VARS = {
    "arista": {
        "required":    ["admin_password_hash", "enable_password_hash"],
        "conditional": ["custom_user_password_hash"],
    },
    "cisco": {
        "required":    ["cisco_admin_password_hash"],
        "conditional": ["cisco_custom_user_password_hash"],
        "optional":    ["cisco_enable_password_hash"],
    },
}


def credential_problems(switch_data: dict, family: str):
    """
    Return (missing, unreplaced) credential variable names for this switch.

    Checked before rendering so the operator gets a named, actionable error
    instead of a config that silently sets a switch password to a placeholder.
    """
    spec = CREDENTIAL_VARS.get(family, {})
    required = list(spec.get("required", []))
    if switch_data.get("custom_username"):
        required += spec.get("conditional", [])

    missing    = [v for v in required if not switch_data.get(v)]
    unreplaced = [
        v for v in required + spec.get("optional", [])
        if PLACEHOLDER_RE.match(str(switch_data.get(v) or ""))
    ]
    return missing, unreplaced


def resolve_path(path: str) -> str:
    """Inventory paths like 'config/config_acme.yaml' are repo-relative."""
    return path if os.path.isabs(path) else os.path.join(ROOT, path)

def ip_and_mask(value):
    """
    '172.18.10.11/24' -> '172.18.10.11 255.255.255.0'

    inventory.yaml carries addresses in CIDR form, which is what Arista wants
    and what Cisco IOS refuses: there the syntax is 'ip address <addr> <mask>'.
    Anything that is not CIDR is passed through untouched.
    """
    if not value:
        return value
    text = str(value).strip()
    if "/" not in text:
        return text
    try:
        return f"{ipaddress.ip_interface(text).ip} {ipaddress.ip_interface(text).netmask}"
    except ValueError:
        return text


def ip_only(value):
    """'172.18.10.11/24' -> '172.18.10.11' (drop the prefix length)."""
    if not value:
        return value
    return str(value).strip().split("/")[0]


def expand_interfaces(range_str):
    if not range_str:
        return []
    result = []
    # If multiple ranges separated by commas (e.g. "Eth1-2, Eth4")
    parts = [p.strip() for p in range_str.split(',')]
    for part in parts:
        match = re.match(r"([A-Za-z]+)(\d+)(/[^-\s]+)?-(\d+)(/[^-\s]+)?", part)
        if match:
            prefix = match.group(1)
            start = int(match.group(2))
            sub_start = match.group(3) or ""
            end = int(match.group(4))
            for i in range(start, end + 1):
                result.append(f"{prefix}{i}{sub_start}")
        else:
            result.append(part)
    return result

def main():
    # Memory cache for variable files to avoid repetitive disk I/O
    vars_cache = {}

    # Load the default YAML configuration file
    try:
        with open(CONFIG_FILE, 'r') as f:
            vars_cache[CONFIG_FILE] = yaml.safe_load(f)
    except Exception as e:
        print(f"✗ Error loading {CONFIG_FILE}: {e}")
        print(f"  Create it with: cp {os.path.relpath(CONFIG_FILE, ROOT)}.template "
              f"{os.path.relpath(CONFIG_FILE, ROOT)}")
        return 1

    # Load the inventory file
    try:
        with open(INVENTORY_FILE, 'r') as f:
            inventory_data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"✗ Error loading {INVENTORY_FILE}: {e}")
        print(f"  Create it with: cp {os.path.relpath(INVENTORY_FILE, ROOT)}.template "
              f"{os.path.relpath(INVENTORY_FILE, ROOT)}")
        return 1

    # Create output directory if it does not exist
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Load template
    try:
        env = Environment(
            loader=FileSystemLoader(TEMPLATE_DIR),
            undefined=StrictUndefined,
            keep_trailing_newline=True
        )
        env.filters['expand_interfaces'] = expand_interfaces
        env.filters['ip_and_mask'] = ip_and_mask
        env.filters['ip_only'] = ip_only
    except Exception as e:
        print(f"✗ Error setting up Jinja environment: {e}")
        return 1

    inventory_switches = inventory_data.get('switches') or []
    if not inventory_switches:
        print(f"✗ No switches found in {os.path.relpath(INVENTORY_FILE, ROOT)}.")
        return 1

    # Copy, so appending the generic entry does not mutate the loaded inventory
    switches = list(inventory_switches)

    # A switch without its own 'customer' inherits the inventory-wide default
    inv_defaults = inventory_data.get('defaults') or {}
    default_customer = str(inv_defaults.get('customer') or '').strip().lower()

    # Fallback config for switches that are NOT in the inventory. It stays on the
    # vendor-neutral template set on purpose: an unknown box must not be handed a
    # customer's full BGP/OSPF/PTP config. 'customer' is pinned empty so it never
    # inherits defaults.customer.
    switches.append({
        'hostname': 'GENERIC_SWITCH',
        'serial': 'generic',
        'platform': 'eos',
        'customer': '',
        'tags': ['generic', 'edge'],
        'mgmt_svi_ip_address': '10.255.255.254/24'
    })

    errors = 0
    generated = 0

    for switch in switches:
        hostname_for_msg = switch.get('hostname', switch.get('serial', 'Unknown'))

        # Which customer does this switch belong to? Per-switch wins, then the
        # inventory-wide default. 'customer' is absent/empty → the default set.
        if 'customer' in switch:
            customer = str(switch.get('customer') or '').strip().lower()
        else:
            customer = default_customer

        # Determine which variable file to use for this switch:
        #   1. explicit per-switch 'vars_file:'
        #   2. customer     → config/config_<customer>.yaml
        #   3. no customer  → config/config.yaml
        if switch.get('vars_file'):
            vars_file = resolve_path(switch['vars_file'])
        elif customer:
            vars_file = customer_vars_file(customer)
            if not os.path.isfile(vars_file):
                print(f"✗ {hostname_for_msg}: customer '{customer}' needs "
                      f"{os.path.relpath(vars_file, ROOT)}, which does not exist — skipped.")
                errors += 1
                continue
        else:
            vars_file = CONFIG_FILE

        if vars_file not in vars_cache:
            try:
                with open(vars_file, 'r') as f:
                    vars_cache[vars_file] = yaml.safe_load(f) or {}
                print(f"  Loaded variable file: {os.path.relpath(vars_file, ROOT)}")
            except Exception as e:
                print(f"✗ Error loading {vars_file} for {hostname_for_msg}: {e}")
                errors += 1
                continue

        # Create a DEEP copy to prevent shared references between switches
        switch_data = copy.deepcopy(vars_cache[vars_file])
        # Merge switch specific settings
        hostname = switch.get('hostname', 'generic')
        switch_data['hostname'] = hostname
        # Use mgmt_oob_ip_address from inventory as mgmt_ip
        if 'mgmt_oob_ip_address' in switch:
            switch_data['mgmt_ip'] = switch['mgmt_oob_ip_address']
        
        tags = switch.get('tags') or []
        switch_data['tags'] = tags
        
        # Merge site-specific config if it exists in a 'sites' dictionary
        if 'sites' in switch_data:
            sites = switch_data['sites'] or {}
            site_code = next((tag for tag in tags if tag in sites), None)
            if site_code:
                switch_data.update(sites[site_code])
            else:
                # A site-keyed variable file may define NOTHING at top level —
                # every variable lives under sites.<SITE>. Without a matching tag
                # the render would fail on a wall of undefined variables.
                print(f"✗ {hostname_for_msg}: none of its tags {tags} match a site in "
                      f"{os.path.relpath(vars_file, ROOT)} "
                      f"(available: {', '.join(sorted(sites))}) — skipped.")
                errors += 1
                continue

        # Per-switch identity is re-applied AFTER the site merge. A site block
        # is shared by every switch at that site, so if it carries a 'hostname'
        # it would otherwise overwrite the inventory one and every switch at the
        # site would come out with the same name.
        switch_data['hostname'] = hostname
        switch_data['tags'] = tags
                
        switch_data['mgmt_svi_ip_address'] = switch.get('mgmt_svi_ip_address', '')
        # Always define it: templates run under StrictUndefined, and a switch
        # without a 'mgmt_oob_ip_address' in inventory (e.g. the generic fallback)
        # would otherwise blow up on {% if mgmt_ip %}.
        switch_data.setdefault('mgmt_ip', '')

        # inventory.yaml carries ONE address per switch (the in-band SVI), and
        # commonly repeats it in both mgmt_oob_ip_address and mgmt_svi_ip_address. That
        # used to emit the same address on Management1 AND the SVI. Management1
        # is the OOB port on a different network, so it gets no address here.
        if (switch_data.get('mgmt_svi_ip_address')
                and switch_data.get('mgmt_ip') == switch_data['mgmt_svi_ip_address']):
            print(f"  note: {hostname}: Management1 (OOB) left unaddressed — "
                  f"{switch_data['mgmt_ip']} belongs to the in-band MGMT SVI")
            switch_data['mgmt_ip'] = ''

        # Only fall back to a placeholder when there is no management IP at all
        if not switch_data.get('mgmt_ip') and not switch_data.get('mgmt_svi_ip_address'):
            switch_data['mgmt_ip'] = "172.20.20.11/24"
            
        # Dynamic MLAG parameters based on hostname
        switch_data['local_mlag_ip'] = ''
        switch_data['peer_mlag_ip'] = ''

        hn_lower = hostname.lower()
        if 'leaf' in hn_lower:
            match = re.search(r'leaf(\d+)', hn_lower)
            if match:
                num = int(match.group(1))
                switch_data['local_mlag_ip'] = switch_data.get(f'leaf{num}_mlag_ip', '')
                
                if num % 2 == 1:
                    peer_num = num + 1
                    switch_data['mlag_domain_id'] = f"LEAF{num}LEAF{peer_num}"
                else:
                    peer_num = num - 1
                    switch_data['mlag_domain_id'] = f"LEAF{peer_num}LEAF{num}"
                    
                switch_data['peer_mlag_ip'] = switch_data.get(f'leaf{peer_num}_mlag_ip', '')
                
        elif 'spine' in hn_lower:
            match = re.search(r'spine(\d+)', hn_lower)
            if match:
                num = int(match.group(1))
                switch_data['local_mlag_ip'] = switch_data.get(f'spine{num}_mlag_ip', '')
                
                if num % 2 == 1:
                    peer_num = num + 1
                    switch_data['mlag_domain_id'] = f"SPINE{num}SPINE{peer_num}"
                else:
                    peer_num = num - 1
                    switch_data['mlag_domain_id'] = f"SPINE{peer_num}SPINE{num}"
                    
                switch_data['peer_mlag_ip'] = switch_data.get(f'spine{peer_num}_mlag_ip', '')

        # STP Priority based on tag
        tags = switch_data.get('tags') or []
        if 'spine' in tags:
            switch_data['stp_priority'] = switch_data.get('spine_stp_priority', '0')
        elif 'leaf' in tags:
            switch_data['stp_priority'] = switch_data.get('leaf_stp_priority', '4096')
        elif 'edge' in tags:
            switch_data['stp_priority'] = switch_data.get('edge_stp_priority', '8192')
        else:
            switch_data['stp_priority'] = switch_data.get('default_priority', '32768')

        if 'mlag_domain_id' not in switch_data:
            switch_data['mlag_domain_id'] = "LEAF1LEAF2"

        # Pick the template:
        #   1. explicit per-switch 'template:'
        #   2. customer     → <family>_config_template_<customer>.j2
        #   3. vars file / site 'template:'
        #   4. no customer  → <family>_config.j2
        # A customer's template is chosen from the customer, NOT from the vars
        # file, so a customer switch can never fall back to arista_config.j2 /
        # cisco_config.j2 even if some vars file names them.
        platform = switch.get('platform', 'eos')
        expected_family = PLATFORM_FAMILY.get(platform, 'arista')
        default_template = ('cisco_config.j2' if expected_family == 'cisco'
                            else 'arista_config.j2')

        if switch.get('template'):
            template_name = switch['template']
        elif customer:
            template_name = customer_template(customer, expected_family)
            if not os.path.isfile(os.path.join(TEMPLATE_DIR, template_name)):
                print(f"✗ {hostname}: customer '{customer}' + platform '{platform}' "
                      f"needs templates/{template_name}, which does not exist — skipped.")
                errors += 1
                continue
        else:
            # A variable file may name a template globally — config.yaml ships
            # 'template: arista_config.j2'. Honour that only when it matches
            # the switch's own platform. Without this every cisco_ios switch
            # inherited the Arista template and was then rejected by the guard
            # rail below, so Cisco configs could never be generated with the
            # default variable file at all.
            from_vars = switch_data.get('template')
            if from_vars and template_family(from_vars) in ('unknown', expected_family):
                template_name = from_vars
            else:
                if from_vars:
                    print(f"  note: {hostname}: ignoring 'template: {from_vars}' from "
                          f"{os.path.relpath(vars_file, ROOT)} — platform '{platform}' "
                          f"needs a {expected_family} template; using {default_template}")
                template_name = default_template

        # Guard rail: config/config.yaml hardcodes 'template: arista_config.j2',
        # which previously handed cisco_ios switches an Arista config.
        actual_family = template_family(template_name)
        if actual_family not in ('unknown', expected_family):
            print(f"✗ {hostname}: platform '{platform}' needs a {expected_family} "
                  f"template but '{template_name}' is a {actual_family} one — skipped. "
                  f"Point 'template:' at a {expected_family} template, or correct "
                  f"'platform:' on the switch in inventory.yaml. A template of the "
                  f"wrong family is never accepted.")
            errors += 1
            continue

        # Credentials are checked before rendering, for BOTH vendors. An IOS XE
        # switch rendered with the Arista hashes does not fail — it quietly
        # sets the login password to the literal '$6$...' string, because IOS
        # treats an unrecognised hash prefix as cleartext. The same is true of
        # an unreplaced REPLACE_ME placeholder.
        missing, unreplaced = credential_problems(switch_data, expected_family)
        if missing or unreplaced:
            rel_vars = os.path.relpath(vars_file, ROOT)
            if missing:
                print(f"✗ {hostname}: platform '{platform}' needs "
                      f"{', '.join(missing)} in {rel_vars} — skipped.")
            if unreplaced:
                print(f"✗ {hostname}: still has placeholder value(s) for "
                      f"{', '.join(unreplaced)} in {rel_vars} — skipped.")
            if expected_family == 'cisco':
                print("  IOS does not accept the $6$ hashes used for Arista — it would "
                      "treat one as a CLEARTEXT password. Generate a real hash on any "
                      "IOS XE box:")
                print("    conf t ; username tmp algorithm-type sha256 secret <password>")
                print("    show run | include username tmp")
            else:
                print("  Generate one with: openssl passwd -6")
            errors += 1
            continue

        # Render template
        try:
            tmpl = env.get_template(template_name)
            output = tmpl.render(**switch_data)
        except Exception as e:
            print(f"✗ Error rendering {template_name} for {switch_data['hostname']}: {e}")
            errors += 1
            continue

        # Output filename based on serial number (fallback to hostname)
        serial = switch.get('serial')
        if serial:
            filename = f"{serial}.cfg"
        else:
            filename = f"{switch_data['hostname']}.cfg"
            
        output_path = os.path.join(OUTPUT_DIR, filename)

        # Write the generated config to file
        with open(output_path, "w") as f:
            f.write(output)

        generated += 1
        print(f"✓ {os.path.relpath(output_path, ROOT)}  "
              f"[customer={customer or 'default'} | vars={os.path.relpath(vars_file, ROOT)} "
              f"| template={template_name}]")

    print()
    print(f"Generated {generated} config file(s) in {OUTPUT_DIR}")
    if errors:
        print(f"{errors} switch(es) FAILED — see the messages above.")
    return 1 if errors else 0

if __name__ == "__main__":
    sys.exit(main() or 0)
