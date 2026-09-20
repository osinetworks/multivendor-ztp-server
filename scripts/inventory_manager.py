#!/usr/bin/env python3
# ============================================================
# inventory_manager.py
# Loads and queries the ZTP inventory YAML file.
# Supports priority-based provisioning order.
# ============================================================

import yaml
import os
import threading
import logging
from typing import Dict, Any, List, Optional

INVENTORY_PATH = os.environ.get("INVENTORY_PATH", "/var/www/ztp/config/inventory.yaml")
DEFAULT_CONFIG = os.environ.get("DEFAULT_CONFIG", "generic.cfg")

logger = logging.getLogger("ztp.inventory")


def _discard(path: str):
    """Remove a file we created and could not finish writing. Never raises."""
    try:
        os.unlink(path)
    except OSError:
        pass


class InventoryPersistError(RuntimeError):
    """
    Raised when the inventory could not be written to disk.

    _persist() used to swallow the error, so POST/DELETE /api/switches answered
    '{"status": "ok"}' for a change that never left memory and vanished on the
    next restart — the same silent-loss failure the single-file bind mount used
    to cause. The caller now finds out.
    """


def display_name(switch: Dict[str, Any]) -> str:
    """
    Human-readable label for a switch.

    inventory.yaml.template documents a 'description' field, while the working
    inventory.yaml uses 'hostname'. Accept either (description wins) so the
    dashboard, the CLI and the logs are never blank.
    """
    return switch.get("description") or switch.get("hostname") or ""


class InventoryManager:
    def __init__(self, path: str = INVENTORY_PATH):
        self.path = path
        self._data: Dict = {}
        self._index: Dict[str, Dict] = {}
        # Reloads and writes happen from request threads; reads must never see
        # a half-built index.
        self._lock = threading.RLock()
        self.reload()

    def reload(self):
        """Load / reload the inventory YAML file."""
        try:
            with open(self.path, "r") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                raise yaml.YAMLError("top-level YAML document is not a mapping")
            with self._lock:
                self._data = data
                self._build_index()
                count = len(self._index)
            logger.info(f"Inventory loaded: {count} switches from {self.path}")
        except FileNotFoundError:
            logger.error(f"Inventory file not found: {self.path}")
            with self._lock:
                self._data = {}
                self._index = {}
        except yaml.YAMLError as e:
            # Keep the previously loaded (valid) inventory rather than dropping
            # every switch because someone saved a broken YAML file.
            logger.error(f"Inventory YAML parse error, keeping previous inventory: {e}")

    def _reload_unlocked(self):
        """Re-read the file while the caller already holds the lock."""
        try:
            with open(self.path, "r") as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                self._data = data
                self._build_index()
            else:
                logger.error(
                    f"Could not re-sync inventory from disk: {self.path} is not a "
                    f"YAML mapping — in-memory inventory may be ahead of the file"
                )
        except (OSError, yaml.YAMLError) as e:
            logger.error(f"Could not re-sync inventory from disk: {e}")

    def _build_index(self):
        """Build serial → switch dict for O(1) lookup. Caller holds the lock."""
        self._index = {}
        for switch in self._data.get("switches") or []:
            if not isinstance(switch, dict):
                logger.warning(f"Skipping malformed inventory entry: {switch!r}")
                continue
            serial = str(switch.get("serial", "")).upper().strip()
            if serial:
                self._index[serial] = switch
            else:
                logger.warning(f"Skipping inventory entry without serial: {switch!r}")

    def get_defaults(self) -> Dict[str, Any]:
        with self._lock:
            defaults = dict(self._data.get("defaults") or {})
        defaults.setdefault("firmware", "EOS-4.34.3M.swi")
        defaults.setdefault("platform", "eos")
        defaults.setdefault("priority", 99)
        defaults.setdefault("config",   DEFAULT_CONFIG)
        return defaults

    @staticmethod
    def _as_priority(value) -> int:
        """Priority must be an int; a typo in YAML must not crash a request."""
        try:
            return int(value)
        except (TypeError, ValueError):
            logger.warning(f"Invalid priority {value!r} — treating as 99")
            return 99

    def get_switch(self, serial: str) -> Optional[Dict[str, Any]]:
        """Return the raw inventory entry for a serial, or None."""
        with self._lock:
            return self._index.get(str(serial).upper().strip())

    def get_priority(self, serial: str) -> int:
        """Return the provisioning priority for a serial number."""
        switch = self.get_switch(serial)
        if switch is not None:
            return self._as_priority(switch.get("priority", 99))
        return self._as_priority(self.get_defaults().get("priority", 99))

    def get_serials_with_priority(self, priority: int) -> List[str]:
        """Return all serial numbers that have a given priority."""
        with self._lock:
            switches = list(self._index.values())
        return [
            str(sw.get("serial", "")).upper().strip()
            for sw in switches
            if self._as_priority(sw.get("priority", 99)) == priority
        ]

    def get_all_priorities(self) -> List[int]:
        """Return sorted list of all unique priority values in inventory."""
        with self._lock:
            switches = list(self._index.values())
        return sorted({self._as_priority(sw.get("priority", 99)) for sw in switches})

    def get_manifest(self, serial: str) -> Dict[str, Any]:
        """
        Return the manifest (config, firmware, description, priority) for a serial.
        Falls back to defaults if serial is not in inventory.
        """
        serial = str(serial).upper().strip()
        defaults = self.get_defaults()
        switch = self.get_switch(serial)
        config_name = f"{serial}.cfg"

        if switch is not None:
            logger.info(
                f"Manifest hit  : {serial} → {config_name} / "
                f"{switch.get('firmware')} (priority {switch.get('priority', 99)})"
            )
            return {
                "serial":        serial,
                "description":   display_name(switch),
                "hostname":      switch.get("hostname", ""),
                "platform":      switch.get("platform",     defaults.get("platform")),
                "firmware":      switch.get("firmware",     defaults.get("firmware")),
                "firmware_md5":  switch.get("firmware_md5", defaults.get("firmware_md5", "")),
                "config":        config_name,
                "fallback_config": defaults.get("config", DEFAULT_CONFIG),
                "priority":      self._as_priority(switch.get("priority", 99)),
                "tags":          switch.get("tags", []),
                "source":        "inventory",
            }

        logger.warning(f"Manifest miss : {serial} → using defaults")
        return {
            "serial":        serial,
            "description":   "Unknown / Unregistered Switch",
            "hostname":      "",
            "platform":      defaults.get("platform"),
            "firmware":      defaults.get("firmware"),
            "firmware_md5":  defaults.get("firmware_md5", ""),
            "config":        config_name,
            "fallback_config": defaults.get("config", DEFAULT_CONFIG),
            "priority":      self._as_priority(defaults.get("priority", 99)),
            "tags":          [],
            "source":        "default",
        }

    def list_switches(self) -> List[Dict]:
        """Return all switches sorted by priority, then serial."""
        with self._lock:
            switches = list(self._index.values())
        return sorted(
            switches,
            key=lambda s: (self._as_priority(s.get("priority", 99)),
                           str(s.get("serial", ""))),
        )

    def add_switch(self, serial: str, firmware: str,
                   description: str = "", hostname: str = "",
                   platform: str = "eos", tags=None, priority: int = 99):
        """
        Add or update a switch entry and persist to YAML.

        An update MERGES into the existing entry — replacing it outright used to
        silently drop fields this API does not know about (ip_address,
        mgmt_svi_ip_address, vars_file, ...) that the config generator needs.
        """
        serial = str(serial).upper().strip()
        updates = {
            "serial":   serial,
            "platform": platform,
            "firmware": firmware,
            "priority": self._as_priority(priority),
            "tags":     list(tags or []),
        }
        if description:
            updates["description"] = description
        if hostname:
            updates["hostname"] = hostname

        with self._lock:
            switches = self._data.setdefault("switches", [])
            if switches is None:
                switches = self._data["switches"] = []

            entry = self._index.get(serial)
            if entry is None:
                entry = dict(updates)
                switches.append(entry)
            else:
                entry.update(updates)
                # Keep the list and the index pointing at the same object
                for i, sw in enumerate(switches):
                    if str(sw.get("serial", "")).upper() == serial:
                        switches[i] = entry
                        break
                else:
                    switches.append(entry)

            self._index[serial] = entry
            try:
                self._persist()
            except InventoryPersistError:
                self._reload_unlocked()
                raise
            result = dict(entry)

        logger.info(f"Switch added/updated: {serial} (priority {updates['priority']})")
        return result

    def remove_switch(self, serial: str) -> bool:
        serial = str(serial).upper().strip()
        with self._lock:
            if serial not in self._index:
                return False
            del self._index[serial]
            self._data["switches"] = [
                sw for sw in (self._data.get("switches") or [])
                if str(sw.get("serial", "")).upper() != serial
            ]
            try:
                self._persist()
            except InventoryPersistError:
                self._reload_unlocked()
                raise
        logger.info(f"Switch removed: {serial}")
        return True

    def _persist(self):
        """
        Write current state back to YAML file. Caller holds the lock.
        Writes to a temp file and renames, so a crash mid-write cannot leave a
        truncated inventory behind.

        The rename replaces the inode, which inside the container means the new
        file belongs to root — and the operator on the host could no longer
        edit their own inventory.yaml. Ownership and mode of the file we are
        replacing are therefore carried over, best-effort.

        yaml.dump cannot preserve comments, so a switch added through the API
        returns the file without any of the operator's notes. That used to be
        invisible because the write failed outright on a single-file bind
        mount; now that it succeeds, the previous file is kept as
        '<inventory>.bak' and the loss is logged.
        """
        tmp = f"{self.path}.tmp"
        try:
            try:
                st = os.stat(self.path)
            except OSError:
                st = None

            # Warned on EVERY rewrite, not just the first: an operator who
            # restored their comments by hand must be told they are going away
            # again.
            if st is not None:
                logger.warning(
                    f"Rewriting {self.path} — YAML comments are not preserved."
                )

            backup = f"{self.path}.bak"
            if st is not None:
                # Only the FIRST rewrite is backed up, and that is the point:
                # a second one would overwrite the backup with the already
                # comment-stripped file and the operator's notes would be gone
                # for good. O_CREAT|O_EXCL|O_NOFOLLOW does that check and the
                # write as one atomic step, so a symlink planted at the backup
                # path cannot redirect a root-owned write somewhere else.
                try:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    flags |= getattr(os, "O_NOFOLLOW", 0)
                    fd = os.open(backup, flags, st.st_mode & 0o7777)
                    # os.fdopen FIRST: a multi-item 'with' enters left to right,
                    # so opening the source first would mean a failed read leaves
                    # the raw fd from os.open with no owner and nothing to close
                    # it — one leaked descriptor per failed backup, for the life
                    # of the container. fdopen closes the fd even when it raises.
                    with os.fdopen(fd, "wb") as dst, open(self.path, "rb") as src:
                        dst.write(src.read())
                        dst.flush()
                        try:
                            os.fchown(dst.fileno(), st.st_uid, st.st_gid)
                            os.fchmod(dst.fileno(), st.st_mode & 0o7777)
                        except (OSError, AttributeError) as e:
                            logger.warning(f"Could not preserve backup permissions: {e}")
                    logger.warning(f"Pre-API version of the inventory saved as {backup}")
                except FileExistsError:
                    logger.warning(
                        f"{backup} already exists — NOT overwriting it, so this "
                        f"rewrite is not backed up. Move it aside to capture a new one."
                    )
                except OSError as e:
                    # os.open already created the file, so a failure partway
                    # through would leave an empty .bak sitting in the
                    # once-only slot — every later rewrite would then skip the
                    # backup and the operator's comments would never be saved.
                    _discard(backup)
                    logger.warning(f"Could not back up inventory before rewrite: {e}")
                except Exception as e:
                    # Anything that is not an OSError gets the same cleanup and
                    # the same verdict: the backup is a convenience, so a failed
                    # one must not abort the inventory write the operator asked
                    # for. Logged with a traceback rather than swallowed, and
                    # KeyboardInterrupt / SystemExit still propagate.
                    _discard(backup)
                    logger.error(
                        f"Unexpected error backing up inventory "
                        f"({type(e).__name__}: {e}) — continuing with the rewrite",
                        exc_info=True,
                    )

            with open(tmp, "w") as f:
                yaml.dump(self._data, f, default_flow_style=False, sort_keys=False)

            if st is not None:
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                    os.chmod(tmp, st.st_mode & 0o7777)
                except (OSError, AttributeError) as e:
                    logger.warning(f"Could not preserve inventory ownership: {e}")

            os.replace(tmp, self.path)
        except Exception as e:
            logger.error(f"Failed to persist inventory: {e}")
            try:
                os.remove(tmp)
            except OSError:
                pass
            # Memory and disk have diverged — drop back to what is on disk so a
            # later reload cannot silently resurrect the rejected change.
            raise InventoryPersistError(f"could not write {self.path}: {e}") from e
