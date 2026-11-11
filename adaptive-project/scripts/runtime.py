#!/usr/bin/env python3
"""Local SQLite multi-chat project state engine; Python 3.8 standard library."""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone


class RuntimeErrorDetail(Exception):
    def __init__(self, message, code="invalid_request"):
        super().__init__(message)
        self.code = code


def require(value, message, code="invalid_request"):
    if not value:
        raise RuntimeErrorDetail(message, code)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_json(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def text(value, field="value"):
    require(isinstance(value, str) and value.strip(), field + " must be nonempty text")
    return value


def ident(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value), "Invalid identifier")
    return value


def list_text(value, field="value", empty=False):
    require(isinstance(value, list) and (empty or value), field + " must be a list")
    for item in value:
        text(item, field)
    return value


def inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class Runtime:
    TERMINAL = {"done", "cancelled", "failed"}
    WORKER_OPS = {"task.claim", "task.heartbeat", "task.checkpoint", "task.submit", "memory.propose"}
    CONTROL_OPS = {"control.pause", "control.resume", "budget.set", "exploration.budget.set", "scheduler.bind", "scheduler.enable"}
    SHUTDOWN_OPS = {"shutdown.prepare", "shutdown.confirm"}
    RECONCILE_OPS = {"outbox.resolve", "outbox.reconcile"} | SHUTDOWN_OPS
    MAINTENANCE_OPS = {"artifact.export", "artifact.restore"}
    DEFAULT_BUDGETS = {"max_wakes": 20, "max_claims": 30, "max_dispatches": 30,
                       "max_actions_per_wake": 20, "max_wake_seconds": 300}

    def __init__(self, project, allow_test_faults=False):
        self.root = Path(project).resolve()
        require(self.root.is_dir(), "Project directory must exist")
        self.meta = self.path(".adaptive-v4")
        self.db = self.path(".adaptive-v4/state.sqlite3")
        self.allow_test_faults = allow_test_faults

    def path(self, value, file=False):
        require(isinstance(value, (str, Path)) and str(value) and "://" not in str(value), "Expected local path")
        path = (self.root / value).resolve()
        require(inside(path, self.root), "Path escapes project: " + str(value), "path_escape")
        require(not file or path.is_file(), "Missing file: " + str(value), "missing_file")
        return path

    def refs(self, values):
        require(isinstance(values, list) and values, "Evidence list must be nonempty")
        result = []
        for item in values:
            path = self.path(item.get("ref") if isinstance(item, dict) else item, True)
            result.append({"ref": path.relative_to(self.root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        return result

    def unchanged(self, refs):
        fresh = self.refs(refs)
        require(all(a["ref"] == b["ref"] and a["sha256"] == b["sha256"] for a, b in zip(fresh, refs)), "Referenced evidence changed", "stale_input")

    def connect(self, create=False):
        self.meta = self.path(".adaptive-v4")
        if create:
            self.meta.mkdir(exist_ok=True)
        require(create or self.db.is_file(), "Project not initialized", "not_initialized")
        self.db = self.path(".adaptive-v4/state.sqlite3")
        con = sqlite3.connect(str(self.db), timeout=10, isolation_level=None)
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def init(self, charter):
        charter = copy.deepcopy(charter)
        ident(charter.get("project_id")); text(charter.get("objective"), "objective")
        list_text(charter.get("acceptance"), "acceptance")
        require(charter.get("authority", {}).get("status") == "bound", "Authorization not bound")
        authority_sources = self.refs(charter["authority"].get("source_refs"))
        require(self.path(charter.get("scope", {}).get("project_root")) == self.root, "project_root mismatch")
        roots = [self.path(p) for p in list_text(charter["scope"].get("write_roots"), "write_roots")]
        require(any(inside(self.meta, p) for p in roots), "State directory outside authorized roots")
        coordinator = text(charter.get("execution", {}).get("state_writer_id"), "state_writer_id")
        operator = text(charter.get("execution", {}).get("operator_id"), "operator_id")
        require(operator != coordinator, "Operator and coordinator must be distinct")
        mode = charter.get("execution", {}).get("mode", "foreground")
        require(mode in {"foreground", "background"}, "Invalid execution mode")
        config = charter.get("scheduler", {})
        require(isinstance(config, dict), "scheduler must be an object")
        budgets = dict(self.DEFAULT_BUDGETS)
        supplied = config.get("budgets", {})
        require(isinstance(supplied, dict) and not set(supplied) - set(budgets), "Unknown scheduler budget")
        budgets.update(supplied)
        require(all(type(v) is int and v >= 1 for v in budgets.values()), "Budgets must be positive integers")
        ceiling = dict(budgets)
        supplied_ceiling = config.get("budget_ceiling", {})
        require(isinstance(supplied_ceiling, dict) and not set(supplied_ceiling) - set(budgets), "Unknown budget ceiling")
        ceiling.update(supplied_ceiling)
        require(all(type(v) is int and v >= budgets[k] for k, v in ceiling.items()), "Budget ceiling below initial budget")
        cooldown = config.get("cooldown_seconds", 60)
        backoff = config.get("max_backoff_seconds", 3600)
        not_before = config.get("not_before", 0)
        expires = config.get("expires_at", time.time() + 86400)
        for value in [cooldown, backoff, not_before, expires]:
            require(type(value) in {int, float} and math.isfinite(value) and value >= 0, "Invalid scheduler time")
        require(backoff >= max(cooldown, 1) and expires > 0, "Invalid backoff or deadline")
        limits = {"max_nonretired_units": 8, "max_active_chats": 6, "max_running_leases": 3,
                  "max_attempts_per_task": 3, "max_unit_depth": 3, "max_lease_seconds": 1800}
        limits.update({k: v for k, v in charter.get("limits", {}).items() if k in limits})
        require(all(type(v) is int and v >= 1 for v in limits.values()), "Invalid limits")
        state = {"schema_version": "4.0", "project_id": charter["project_id"], "revision": 0,
                 "project_status": "active", "coordinator_id": coordinator, "operator_id": operator, "charter": charter,
                 "authority_sources": authority_sources, "limits": limits, "units": [], "chats": [],
                 "tasks": [], "memories": [], "outbox": [], "checkpoints": [], "events": [], "updated_at": stamp()}
        state["execution_mode"] = mode
        state["exploration"] = self.init_exploration(charter.get("exploration", {}))
        state["control"] = {"revision": 1, "status": "running", "reason": "Initialized"}
        state["scheduler"] = {"revision": 1, "binding": None, "enabled": False, "budgets": budgets, "budget_ceiling": ceiling,
            "usage": {"wakes": 0, "claims": 0, "dispatches": 0}, "active_wake": None, "fencing_token": 0,
            "wakes": [], "next_wake_at": 0, "not_before": not_before, "expires_at": expires,
            "blocked_reason": None, "idle_streak": 0, "error_streak": 0,
            "cooldown_seconds": cooldown, "max_backoff_seconds": backoff}
        state["units"].append({"id": "coordinator", "revision": 1, "name": "协调", "mission": charter["objective"],
                               "parent_id": None, "status": "active", "creation_reason": "Project bootstrap",
                               "exit_condition": "Acceptance met"})
        con = self.connect(True)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("CREATE TABLE IF NOT EXISTS project (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, hash TEXT NOT NULL, response TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS blobs (id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, content BLOB NOT NULL)")
            require(con.execute("SELECT 1 FROM project").fetchone() is None, "Project already initialized", "already_initialized")
            con.execute("INSERT INTO project VALUES (1,?)", (encode(state),))
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return state

    @staticmethod
    def get(state, collection, item_id):
        items = [item for item in state[collection] if item["id"] == item_id]
        require(len(items) == 1, "Unknown " + collection + " ID: " + str(item_id), "unknown_entity")
        return items[0]

    @staticmethod
    def version(entity, operation, field):
        require(type(operation.get(field)) is int and operation[field] == entity["revision"], "Entity revision conflict", "stale_revision")

    def load(self, con):
        row = con.execute("SELECT state FROM project WHERE id=1").fetchone()
        require(row is not None, "Project not initialized", "not_initialized")
        try:
            state = json.loads(row[0])
        except ValueError as error:
            raise RuntimeErrorDetail("Corrupt state: " + str(error), "corrupt_state")
        return state

    def read(self):
        con = self.connect()
        try:
            con.execute("BEGIN")
            state = self.load(con)
            self.validate(state)
            con.commit()
            return state
        finally:
            con.close()

    def storage_recover(self, acknowledge_stopped_writers=False):
        """Let SQLite settle a DELETE journal without changing logical records.

This is explicit physical storage maintenance, not read-only inspection or a
project transaction. The caller must have stopped original writers. SQLite
may recover a hot journal on open; an equal user_version write also commits
away a cold journal that ordinary SELECT/no-op UPDATE can leave behind.
"""
        require(acknowledge_stopped_writers is True,
                "Confirm original writers are stopped with --acknowledge-stopped-writers", "writers_not_acknowledged")
        # Inspect the unresolved descendants before opening: self.path() would
        # resolve away a database/directory link before lstat could reject it.
        # The constructor already normalized the user-supplied project root.
        database = self.root / ".adaptive-v4" / "state.sqlite3"
        for component in list(reversed(database.parents)) + [database]:
            info = component.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400),
                    "Storage recovery does not follow symbolic links or reparse points", "unsafe_path")
        info = database.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "Expected an ordinary database with one hard link", "unsafe_path")
        require(not any(os.path.lexists(str(database) + suffix) for suffix in ("-wal", "-shm")),
                "WAL/shared-memory sidecars require separate diagnosis; journal mode will not be changed", "unsupported_journal")
        journal = Path(str(database) + "-journal")
        had_journal = os.path.lexists(str(journal))
        if had_journal:
            info = journal.lstat()
            require(stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_nlink == 1 and
                    not (getattr(info, "st_file_attributes", 0) & 0x400), "Unsafe SQLite journal path", "unsafe_path")

        def fingerprint(connection):
            result = {}
            queries = {
                "project": "SELECT id,state FROM project ORDER BY id",
                "requests": "SELECT key,hash,response FROM requests ORDER BY key",
                "blobs": "SELECT id,sha256,content FROM blobs ORDER BY id",
                "schema": "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"}
            for name, query in queries.items():
                digest, count = hashlib.sha256(), 0
                for row in connection.execute(query):
                    count += 1
                    for value in row:
                        raw = value if isinstance(value, bytes) else encode(value).encode("utf-8")
                        digest.update(("bytes" if isinstance(value, bytes) else "json").encode("ascii"))
                        digest.update(str(len(raw)).encode("ascii") + b":" + raw)
                    digest.update(b";")
                result[name] = {"rows": count, "sha256": digest.hexdigest()}
            result["metadata"] = {name: connection.execute("PRAGMA " + name).fetchone()[0]
                                  for name in ("user_version", "schema_version", "application_id")}
            return result

        # mode=rw prevents creating a replacement database if it disappears.
        con = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None)
        try:
            con.execute("PRAGMA busy_timeout=10000")
            con.execute("PRAGMA trusted_schema=OFF")
            con.execute("PRAGMA synchronous=FULL")
            require(con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete",
                    "Only the original DELETE journal mode is supported", "unsupported_journal")
            con.execute("BEGIN IMMEDIATE")
            require(con.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "SQLite integrity check failed", "corrupt_state")
            tables = dict(con.execute("SELECT name,type FROM sqlite_master WHERE type IN ('table','view','trigger')"))
            require(tables == {"project": "table", "requests": "table", "blobs": "table"},
                    "Unexpected database format", "unsupported_schema")
            require(con.execute("SELECT COUNT(*) FROM project").fetchone()[0] == 1, "Expected one project record", "corrupt_state")
            state = self.load(con); self.validate(state)
            before = fingerprint(con)
            method = "sqlite_open_only"
            if os.path.lexists(str(journal)):
                con.execute("PRAGMA user_version=" + str(before["metadata"]["user_version"]))
                method = "sqlite_equal_user_version_commit"
            require(fingerprint(con) == before, "Storage recovery changed logical content", "storage_recovery_mismatch")
            con.commit()
            after = fingerprint(con)
            require(after == before, "Logical content changed after storage commit; stop and inspect writers", "storage_recovery_mismatch")
        except Exception:
            con.rollback(); raise
        finally:
            con.close()
        require(not any(os.path.lexists(str(database) + suffix) for suffix in ("-journal", "-wal", "-shm")),
                "A SQLite sidecar remains; stop and inspect, do not delete it manually", "storage_recovery_incomplete")
        return {"storage_recovered": True, "read_only": False, "method": method,
                "had_journal": had_journal, "journal_remaining": False, "logical_content_unchanged": True,
                "state_revision": state["revision"], "project_id": state["project_id"],
                "logical_fingerprints": after, "project_transaction_created": False,
                "note": "Physical database bytes may change; no state, requests, blobs, budget, control, expiry, or logical metadata was changed. Re-run recovery.py before business work."}

    def auth(self, state, name):
        require(state["charter"]["authority"].get(name) is True, "Unauthorized capability: " + name, "unauthorized")

    def actor_unit(self, state, actor):
        if actor in {state["coordinator_id"], state["operator_id"]}:
            return "coordinator"
        chat = self.get(state, "chats", actor)
        require(chat["status"] == "active", "Chat not active", "inactive_actor")
        unit = self.get(state, "units", chat["unit_id"])
        require(unit["status"] == "active", "Unit not active", "inactive_actor")
        return unit["id"]

    def inputs(self, state, task):
        refs = []
        for item in task["inputs"]:
            if isinstance(item, str) or "ref" in item:
                refs.extend(self.refs([item]))
            elif "memory_id" in item:
                memory = self.get(state, "memories", item["memory_id"])
                require(memory["status"] == "active" and memory["revision"] == item.get("revision"), "Memory version stale", "stale_input")
                self.unchanged(memory["sources"])
                if memory.get("valid_until"):
                    expiry = datetime.fromisoformat(memory["valid_until"])
                    require(expiry.tzinfo is not None and expiry > datetime.now(timezone.utc), "Memory expired", "stale_input")
                refs.append({"memory_id": memory["id"], "revision": memory["revision"]})
            else:
                raise RuntimeErrorDetail("Invalid input")
        return refs

    def ready(self, state, task):
        return all(self.get(state, "tasks", d)["status"] == "done" for d in task["dependencies"])

    def live(self, state, task, actor, operation):
        lease = task.get("lease")
        require(task["status"] == "running" and lease, "No active lease", "stale_lease")
        require(lease["actor_id"] == actor and lease["lease_id"] == operation.get("lease_id") and lease["fencing_token"] == operation.get("fencing_token"), "Lease holder or fence mismatch", "stale_lease")
        require(lease["expires_at"] > time.time(), "Lease expired", "lease_expired")
        require(self.actor_unit(state, actor) == task["owner_unit_id"], "Actor no longer owns task", "unauthorized")
        return lease

    @staticmethod
    def attempt(task, attempt_id=None):
        attempt_id = attempt_id or task["lease"]["attempt_id"]
        return next(a for a in task["attempts"] if a["id"] == attempt_id)

    def release(self, state, task, reason):
        if task.get("lease"):
            attempt = self.attempt(task)
            attempt.update(status="interrupted", ended_at=stamp(), interruption_reason=reason)
        task["lease"] = None
        task["fencing_token"] += 1
        task["revision"] += 1
        task["status"] = "ready" if self.ready(state, task) else "blocked"

    @staticmethod
    def coordinator_creation_matches(item, binding, delivered=True):
        result = item.get("result") or {}
        return (bool(binding) and item["kind"] == "create_chat" and
                item["payload"].get("unit_id") == "coordinator" and
                (not delivered or item["status"] == "delivered") and
                (result.get("host_id"), result.get("thread_id")) ==
                (binding["host_id"], binding["thread_id"]))

    @classmethod
    def coordinator_registration(cls, state):
        binding = state["scheduler"]["binding"]
        if not binding:
            return {"status": "unbound", "outbox_id": None}
        oid = binding.get("coordinator_outbox_id")
        if oid is not None:
            matches = [i for i in state["outbox"] if i["id"] == oid and cls.coordinator_creation_matches(i, binding)]
            return {"status": "registered" if len(matches) == 1 else "invalid", "outbox_id": oid}
        # Older projects may already have registered the controller through a
        # dormant chat. Reading them must not rewrite or resurrect reservations.
        if any((c["host_id"], c["thread_id"]) == (binding["host_id"], binding["thread_id"]) for c in state["chats"]):
            return {"status": "legacy_chat_registered", "outbox_id": None}
        matches = [i for i in state["outbox"] if cls.coordinator_creation_matches(i, binding, delivered=False)]
        # A controller creation without a complete receipt may be this very
        # carrier. Do not mistake an omitted resolve for a preexisting host.
        unlocated = any(i["kind"] == "create_chat" and i["payload"].get("unit_id") == "coordinator" and
                        i["status"] != "delivered" and
                        not all(isinstance((i.get("result") or {}).get(k), str) and (i.get("result") or {})[k].strip()
                                for k in ("host_id", "thread_id")) for i in state["outbox"])
        status = "unregistered" if any(i["status"] == "delivered" for i in matches) else "unresolved" if matches or unlocated else "external"
        return {"status": status, "outbox_id": None}

    @classmethod
    def check_coordinator_registration(cls, state):
        registration = cls.coordinator_registration(state)
        require(registration["status"] not in {"invalid", "unregistered", "unresolved"},
                "Reconcile coordinator creation and bind its exact delivered outbox before enabling", "dispatch_ambiguous")

    @staticmethod
    def creation_reservations(state):
        # A delivered creation is reconciled once a binding was recorded, even
        # after that binding is later retired. Retirement must not resurrect a reservation.
        bound = {(c["host_id"], c["thread_id"]) for c in state["chats"]}
        reserved = 0
        binding = state["scheduler"]["binding"]
        for item in state["outbox"]:
            if item["kind"] == "create_chat":
                result = item.get("result") or {}
                # One controller carrier is registered by scheduler.bind, not
                # by a fabricated worker identity. All other creations retain
                # their normal reservation, including duplicate host receipts.
                if (binding and item["id"] == binding.get("coordinator_outbox_id") and
                        Runtime.coordinator_creation_matches(item, binding)):
                    continue
                if item["status"] != "delivered" or (result.get("host_id"), result.get("thread_id")) not in bound:
                    reserved += 1
        return reserved

    @classmethod
    def chat_capacity(cls, state):
        active = sum(c["status"] == "active" for c in state["chats"])
        reserved = cls.creation_reservations(state)
        limit = state["limits"]["max_active_chats"]
        return {"limit": limit, "active_workers": active, "creation_reservations": reserved,
                "available_workers": max(0, limit - active - reserved),
                "coordinator_registration": cls.coordinator_registration(state)}

    def validate(self, state):
        require(state["schema_version"] == "4.0", "Unsupported state version")
        require(state["operator_id"] != state["coordinator_id"], "Operator cannot become coordinator")
        require(state["control"]["status"] in {"running", "paused"}, "Invalid control state")
        self.validate_exploration(state)
        intents = state.get("shutdown_intents", [])
        require(isinstance(intents, list), "Invalid shutdown ledger")
        require(len({i["id"] for i in intents}) == len(intents), "Duplicate shutdown intent IDs")
        for intent in intents:
            ident(intent["id"])
            require(intent["status"] in {"prepared", "confirmed"} and
                    intent["closure_basis"] in {"completed", "paused", "expired"} and
                    intent["credentials_usable"] is False and
                    intent["binding"] == intent["guard"]["binding"] and
                    type(intent["prepared_at_unix"]) in {int, float} and math.isfinite(intent["prepared_at_unix"]),
                    "Invalid shutdown intent")
            require(intent["evidence"] and type(intent["prepared_revision"]) is int, "Shutdown preparation lacks evidence")
            if intent["status"] == "confirmed":
                confirmation = intent.get("confirmation", {})
                require(confirmation.get("evidence") and confirmation.get("confirmed_revision", 0) > intent["prepared_revision"],
                        "Shutdown confirmation lacks separate committed preparation")
        scheduler = state["scheduler"]
        require(type(scheduler["enabled"]) is bool, "Invalid scheduler enabled state")
        require(all(type(v) is int and v >= 1 for v in scheduler["budgets"].values()), "Invalid budget")
        require(all(v <= scheduler["budget_ceiling"][k] for k, v in scheduler["budgets"].items()), "Budget ceiling exceeded")
        require(all(type(v) is int and v >= 0 for v in scheduler["usage"].values()), "Invalid usage")
        if state["control"]["status"] == "paused":
            require(scheduler["active_wake"] is None and not any(t["lease"] for t in state["tasks"]), "Paused state retains live work")
        units = {u["id"]: u for u in state["units"]}
        tasks = {t["id"]: t for t in state["tasks"]}
        chats = {c["id"]: c for c in state["chats"]}
        require(len(units) == len(state["units"]) and len(tasks) == len(state["tasks"]) and len(chats) == len(state["chats"]), "Duplicate entity IDs")
        require(sum(u["status"] != "retired" for u in units.values()) <= state["limits"]["max_nonretired_units"], "Unit cap exceeded", "resource_limit")
        require(sum(c["status"] == "active" for c in chats.values()) <= state["limits"]["max_active_chats"], "Active chat cap exceeded", "resource_limit")
        require(sum(c["status"] == "active" for c in chats.values()) + self.creation_reservations(state) <= state["limits"]["max_active_chats"], "Chat bindings consumed reserved creation capacity", "resource_limit")
        bindings = [(c["host_id"], c["thread_id"]) for c in chats.values() if c["status"] != "retired"]
        require(len(set(bindings)) == len(bindings), "Duplicate thread binding")
        for unit in units.values():
            cursor, seen = unit, set()
            while cursor["parent_id"] is not None:
                require(cursor["id"] not in seen and cursor["parent_id"] in units, "Invalid unit hierarchy")
                seen.add(cursor["id"]); cursor = units[cursor["parent_id"]]
                require(cursor["status"] != "retired" or unit["status"] == "retired", "Live unit below retired parent")
            require(len(seen) <= state["limits"]["max_unit_depth"], "Unit depth exceeded", "resource_limit")
        for chat in chats.values():
            require(chat["unit_id"] in units, "Chat owner missing")
            require(chat["status"] == "retired" or units[chat["unit_id"]]["status"] != "retired", "Chat under retired unit")
        def visit(tid, parents):
            require(tid not in parents, "Task dependency cycle")
            for dep in tasks[tid]["dependencies"]:
                require(dep in tasks, "Missing task dependency")
                visit(dep, parents | {tid})
        running = 0
        for task in tasks.values():
            visit(task["id"], set())
            require(task["owner_unit_id"] in units, "Task owner missing")
            require(task["status"] in self.TERMINAL or units[task["owner_unit_id"]]["status"] != "retired", "Unfinished task owned by retired unit")
            if task["status"] == "running":
                require(task["lease"] is not None, "Running task without lease")
                running += 1
            else:
                require(task["lease"] is None, "Inactive task still leased")
            if task["status"] == "done":
                require(task["accepted_artifacts"] and task["verification"][-1]["verdict"] == "pass", "Done task lacks evidence")
        require(running <= state["limits"]["max_running_leases"], "Running lease cap exceeded", "resource_limit")
        if state["project_status"] == "completed":
            require(tasks and all(t["status"] in {"done", "cancelled"} for t in tasks.values()), "Completed project has unfinished work")

    def accepted_copy(self, con, task, submitted):
        result = []
        for index, item in enumerate(submitted):
            path = self.path(item["ref"], True)
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            require(sha == item["sha256"], "Submission changed", "stale_input")
            blob_id = uuid.uuid4().hex
            destination = self.path(".adaptive-v4/accepted/{}/{}/{}-{}".format(task["id"], blob_id, index, path.name))
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            con.execute("INSERT INTO blobs VALUES (?,?,?)", (blob_id, sha, sqlite3.Binary(data)))
            result.append({"ref": destination.relative_to(self.root).as_posix(), "sha256": sha, "blob_id": blob_id})
        return result

    def materialize(self, con, state, operation, restore=False):
        blob_id = text(operation.get("blob_id"), "blob_id")
        matches = [a for t in state["tasks"] if t["status"] == "done" for a in t["accepted_artifacts"] if a["blob_id"] == blob_id]
        require(len(matches) == 1, "Blob is not an accepted artifact", "unknown_artifact")
        artifact = matches[0]
        row = con.execute("SELECT sha256,content FROM blobs WHERE id=?", (blob_id,)).fetchone()
        require(row is not None and hashlib.sha256(row[1]).hexdigest() == row[0] == artifact["sha256"], "Canonical artifact bytes corrupt", "corrupt_artifact")
        if restore:
            raw = self.root / artifact["ref"]
            target = self.path(artifact["ref"])
            require(raw == target, "Accepted projection path was redirected", "path_escape")
        else:
            target = self.path(operation.get("output"))
            require(not any(inside(target, self.path(name)) for name in [".adaptive", ".adaptive-v2", ".adaptive-v4"]), "Cannot export over runtime metadata", "path_escape")
            roots = [self.path(p) for p in state["charter"]["scope"]["write_roots"]]
            require(any(inside(target, root) for root in roots), "Export exceeds write roots", "path_escape")
            protected = list(state["authority_sources"])
            for task in state["tasks"]:
                protected.extend(i for i in task["inputs"] if isinstance(i, str) or "ref" in i)
                protected.extend(task["artifacts"])
                for review in task["verification"]: protected.extend(review["evidence"])
            for memory in state["memories"]: protected.extend(memory["sources"])
            for value in protected:
                ref = value.get("ref") if isinstance(value, dict) else value
                require(target != self.path(ref), "Cannot export over input or evidence", "path_escape")
        require(not target.exists() or target.is_file(), "Output is not a file")
        previous_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        require(restore or previous_hash in {None, artifact["sha256"]}, "Refusing to overwrite unrelated output", "output_conflict")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".artifact-", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(row[1]); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            if os.path.exists(temp): os.unlink(temp)
        return {"blob_id": blob_id, "ref": target.relative_to(self.root).as_posix(), "sha256": artifact["sha256"], "previous_sha256": previous_hash, "restored": restore}

    def plan_state(self, state):
        scheduler = state["scheduler"]
        now = time.time()
        remaining = {name: max(0, scheduler["budgets"]["max_" + name] - scheduler["usage"][name])
                     for name in ["wakes", "claims", "dispatches"]}
        active = scheduler["active_wake"]
        due = max(scheduler["not_before"], scheduler["next_wake_at"])
        if state["project_status"] == "completed": decision, reason = "completed", "project_completed"
        elif state["control"]["status"] == "paused": decision, reason = "paused", "operator_paused"
        elif state["execution_mode"] == "foreground":
            decision, reason = ("expired", "schedule_deadline") if now >= scheduler["expires_at"] else ("backoff", "not_due") if now < scheduler["not_before"] else ("run", "foreground_work")
        elif not scheduler["binding"]: decision, reason = "unbound", "automation_not_bound"
        elif self.coordinator_registration(state)["status"] in {"invalid", "unregistered", "unresolved"}: decision, reason = "unbound", "coordinator_registration_required"
        elif not scheduler["enabled"]: decision, reason = "disabled", "scheduler_disabled"
        elif now >= scheduler["expires_at"]: decision, reason = "expired", "schedule_deadline"
        elif active and active["expires_at"] > now: decision, reason = "live", "wake_already_active"
        elif not remaining["wakes"]: decision, reason = "budget", "wake_budget_exhausted"
        elif due > now: decision, reason = "backoff", "not_due"
        else: decision, reason = "run", "work_check_due"
        return {"decision": decision, "reason": reason, "remaining": remaining, "exploration": self.exploration_plan(state), "chat_capacity": self.chat_capacity(state), "execution_mode": state["execution_mode"], "not_before": due,
                "expires_at": scheduler["expires_at"], "active_wake": copy.deepcopy(active),
                "should_pause_host": any(i["status"] == "prepared" and self.shutdown_current(state, i)
                                         for i in state.get("shutdown_intents", [])),
                "shutdown_protocol_required": decision in {"completed", "paused", "disabled", "expired", "budget"},
                "shutdown_closure_basis": self.shutdown_closure(state),
                "shutdown_current_intent_ids": [i["id"] for i in state.get("shutdown_intents", [])
                                                 if self.shutdown_current(state, i)],
                "ready_tasks": [t["id"] for t in state["tasks"] if t["status"] in {"ready", "blocked"} and self.ready(state, t)],
                "review_tasks": [t["id"] for t in state["tasks"] if t["status"] == "review"],
                "unresolved_outbox": [o["id"] for o in state["outbox"] if o["status"] != "delivered"]}

    def plan(self):
        return self.plan_state(self.read())

    @staticmethod
    def init_exploration(config):
        require(isinstance(config, dict), "exploration must be an object")
        budgets = {"max_candidates": 6, "max_validations": 8, "max_cost": 20}
        supplied = config.get("budgets", {})
        require(isinstance(supplied, dict) and not set(supplied) - set(budgets), "Invalid exploration budgets")
        budgets.update(supplied)
        require(all(type(v) is int and v > 0 for v in budgets.values()), "Exploration budgets must be positive integers")
        ceiling = dict(budgets)
        supplied = config.get("budget_ceiling", {})
        require(isinstance(supplied, dict) and not set(supplied) - set(budgets), "Invalid exploration ceiling")
        ceiling.update(supplied)
        require(all(type(v) is int and v >= budgets[k] for k, v in ceiling.items()), "Invalid exploration ceiling")
        return {"revision": 1, "status": "open", "budgets": budgets, "budget_ceiling": ceiling,
                "usage": {"candidates": 0, "validations": 0, "cost": 0}, "active_candidate_id": None,
                "questions": [], "candidates": [], "validations": [], "decisions": []}

    @staticmethod
    def exploration_get(state, collection, item_id):
        found = [x for x in state["exploration"][collection] if x["id"] == item_id]
        require(len(found) == 1, "Unknown exploration entity: " + str(item_id), "not_found")
        return found[0]

    def validate_exploration(self, state):
        e = state["exploration"]
        require(state["execution_mode"] in {"foreground", "background"}, "Invalid execution mode")
        require(e["status"] in {"open", "closed"}, "Invalid exploration status")
        require(all(type(v) is int and 0 <= v <= e["budgets"]["max_" + k] <= e["budget_ceiling"]["max_" + k]
                    for k, v in e["usage"].items()), "Invalid exploration accounting")
        for name in ["questions", "candidates", "validations", "decisions"]:
            require(len({x["id"] for x in e[name]}) == len(e[name]), "Duplicate exploration ID")
        for v in e["validations"]:
            require(v["plan_hash"] == hash_json(v["plan"]), "Validation plan changed", "stale_input")
            require(v["status"] in {"planned", "running", "completed", "cancelled"}, "Invalid validation status")
        selected = [c["id"] for c in e["candidates"] if c["status"] == "selected"]
        require(selected == ([e["active_candidate_id"]] if e["active_candidate_id"] else []), "Selected candidate mismatch")
        if e["status"] == "closed":
            require(e["decisions"] and e["decisions"][-1]["action"] in {"stop", "deliver"}, "Missing final decision")
            require(not any(v["status"] in {"planned", "running"} for v in e["validations"]), "Exploration closed with outstanding validations")

    def exploration_plan(self, state):
        e = state["exploration"]
        remaining = {k: e["budgets"]["max_" + k] - v for k, v in e["usage"].items()}
        pending = [v["id"] for v in e["validations"] if v["status"] == "running"]
        planned = [v["id"] for v in e["validations"] if v["status"] == "planned"]
        action = "deliver" if e["status"] == "closed" else "collect_results" if pending else "decide_or_close" if not remaining["validations"] or not remaining["cost"] else "evaluate_next_probe"
        return {"status": e["status"], "revision": e["revision"], "remaining": remaining,
                "active_candidate_id": e["active_candidate_id"], "running_validations": pending,
                "planned_validations": planned, "open_questions": [q["id"] for q in e["questions"] if q["status"] == "open"],
                "suggested_action": action}

    def check_exploration_evidence(self, state):
        e = state["exploration"]
        for q in e["questions"]:
            if q.get("evidence"): self.unchanged(q["evidence"])
        for v in e["validations"]:
            if v["plan"].get("inputs"): self.unchanged(v["plan"]["inputs"])
            if v.get("result"): self.unchanged(v["result"]["evidence"])
            if v.get("cancellation"): self.unchanged(v["cancellation"]["evidence"])
        for d in e["decisions"]: self.unchanged(d["evidence"])

    def exploration_operation(self, state, o, actor):
        require(actor in {state["coordinator_id"], state["operator_id"]}, "Exploration requires coordinator", "unauthorized")
        e, op = state["exploration"], o["op"]
        fields = {
            "exploration.budget.set": {"expected_exploration_revision", "budgets"},
            "question.add": {"id", "question", "importance"},
            "question.resolve": {"id", "expected_question_revision", "status", "answer", "evidence"},
            "candidate.add": {"id", "title", "proposal", "assumptions", "scope_fit", "question_ids"},
            "validation.plan": {"id", "candidate_id", "hypothesis", "method", "criteria", "cost", "inputs"},
            "validation.start": {"id", "expected_validation_revision"},
            "validation.finish": {"id", "expected_validation_revision", "outcome", "summary", "limitations", "criteria_assessment", "evidence"},
            "validation.cancel": {"id", "expected_validation_revision", "reason", "evidence"},
            "decision.record": {"id", "expected_exploration_revision", "action", "candidate_id", "alternatives", "result_ids", "rationale", "evidence", "residual_question_ids", "task_dispositions"}}
        require(op in fields and not set(o) - fields[op] - {"op"}, "Unknown exploration operation or fields")
        if op == "exploration.budget.set":
            require(actor == state["operator_id"], "Only operator may change exploration budgets", "unauthorized")
            self.version(e, o, "expected_exploration_revision")
            wake = state["scheduler"]["active_wake"]
            require(not wake or wake["expires_at"] <= time.time(), "Pause before changing active exploration budgets", "wake_active")
            values = o.get("budgets")
            require(isinstance(values, dict) and values and not set(values) - set(e["budgets"]), "Invalid exploration budget fields")
            require(all(type(v) is int and v > 0 and e["usage"][k[4:]] <= v <= e["budget_ceiling"][k] for k, v in values.items()), "Exploration budget outside used amount or authorized ceiling", "resource_limit")
            e["budgets"].update(values)
            e["revision"] += 1
            return copy.deepcopy(e)
        require(e["status"] == "open", "Exploration already closed", "exploration_closed")
        item_id = ident(o.get("id"))
        result = None
        if op in {"question.add", "candidate.add", "validation.plan", "decision.record"}:
            collection = {"question.add": "questions", "candidate.add": "candidates", "validation.plan": "validations", "decision.record": "decisions"}[op]
            require(not any(x["id"] == item_id for x in e[collection]), "Exploration ID already exists", "already_exists")
        if op == "question.add":
            result = {"id": item_id, "revision": 1, "question": text(o.get("question")), "importance": text(o.get("importance")), "status": "open", "created_at": stamp()}
            e["questions"].append(result)
        elif op == "question.resolve":
            result = self.exploration_get(state, "questions", item_id)
            self.version(result, o, "expected_question_revision")
            require(result["status"] == "open", "Question already resolved")
            require(o.get("status") in {"answered", "deferred"}, "Invalid question resolution")
            result.update(status=o["status"], answer=text(o.get("answer")), evidence=self.refs(o.get("evidence")), resolved_at=stamp())
            result["revision"] += 1
        elif op == "candidate.add":
            require(e["usage"]["candidates"] < e["budgets"]["max_candidates"], "Candidate budget exhausted", "resource_limit")
            questions = list_text(o.get("question_ids"), "question_ids")
            require(len(set(questions)) == len(questions), "Duplicate question links")
            for q in questions: self.exploration_get(state, "questions", q)
            result = {"id": item_id, "revision": 1, "title": text(o.get("title")), "proposal": text(o.get("proposal")),
                      "assumptions": list_text(o.get("assumptions"), "assumptions"), "scope_fit": text(o.get("scope_fit")),
                      "question_ids": questions, "status": "candidate", "evidence_status": "unvalidated", "created_at": stamp()}
            e["candidates"].append(result); e["usage"]["candidates"] += 1
        elif op == "validation.plan":
            c = self.exploration_get(state, "candidates", o.get("candidate_id"))
            require(c["status"] != "retired", "Cannot validate retired candidate")
            require(e["usage"]["validations"] < e["budgets"]["max_validations"], "Validation count exhausted", "resource_limit")
            cost = o.get("cost")
            require(type(cost) is int and cost > 0, "Validation cost must be a positive integer")
            require(cost <= e["budgets"]["max_cost"] - e["usage"]["cost"], "Exploration cost exhausted", "resource_limit")
            criteria = o.get("criteria")
            require(isinstance(criteria, dict) and set(criteria) == {"positive", "negative", "inconclusive"}, "Define all three outcome criteria")
            for value in criteria.values(): text(value, "criterion")
            inputs = o.get("inputs", [])
            require(isinstance(inputs, list), "inputs must be a list")
            plan = {"candidate_id": c["id"], "hypothesis": text(o.get("hypothesis")), "method": text(o.get("method")),
                    "criteria": copy.deepcopy(criteria), "cost": cost, "inputs": self.refs(inputs) if inputs else []}
            result = {"id": item_id, "revision": 1, "status": "planned", "plan": plan, "plan_hash": hash_json(plan), "created_at": stamp(), "result": None}
            e["validations"].append(result)
        elif op in {"validation.start", "validation.finish", "validation.cancel"}:
            result = self.exploration_get(state, "validations", item_id)
            self.version(result, o, "expected_validation_revision")
            if op == "validation.start":
                require(result["status"] == "planned", "Validation is not planned", "invalid_transition")
                c = self.exploration_get(state, "candidates", result["plan"]["candidate_id"])
                require(c["status"] != "retired", "Cannot start validation of retired candidate")
                if result["plan"]["inputs"]: self.unchanged(result["plan"]["inputs"])
                require(e["usage"]["validations"] < e["budgets"]["max_validations"] and e["usage"]["cost"] + result["plan"]["cost"] <= e["budgets"]["max_cost"], "Exploration validation budget exhausted", "resource_limit")
                e["usage"]["validations"] += 1; e["usage"]["cost"] += result["plan"]["cost"]
                result.update(status="running", started_at=stamp())
            elif op == "validation.finish":
                require(result["status"] == "running", "Validation must be started before recording results", "invalid_transition")
                if result["plan"]["inputs"]: self.unchanged(result["plan"]["inputs"])
                require(o.get("outcome") in {"positive", "negative", "inconclusive"}, "Invalid outcome")
                assessment = o.get("criteria_assessment")
                require(isinstance(assessment, dict) and set(assessment) == set(result["plan"]["criteria"]), "Assess all registered criteria")
                for value in assessment.values(): text(value, "criteria assessment")
                result["result"] = {"outcome": o["outcome"], "summary": text(o.get("summary")), "limitations": list_text(o.get("limitations"), "limitations", empty=True),
                                    "criteria_assessment": copy.deepcopy(assessment), "evidence": self.refs(o.get("evidence")), "plan_hash": result["plan_hash"], "recorded_at": stamp()}
                result["status"] = "completed"
                c = self.exploration_get(state, "candidates", result["plan"]["candidate_id"])
                outcomes = {v["result"]["outcome"] for v in e["validations"] if v["plan"]["candidate_id"] == c["id"] and v["status"] == "completed"}
                c["evidence_status"] = "mixed" if {"positive", "negative"} <= outcomes else "supported" if "positive" in outcomes else "refuted" if "negative" in outcomes else "inconclusive"
                c["revision"] += 1
            else:
                require(result["status"] in {"planned", "running"}, "Validation already terminal", "invalid_transition")
                result["cancellation"] = {"reason": text(o.get("reason")), "evidence": self.refs(o.get("evidence")), "at": stamp()}
                result["status"] = "cancelled"
            result["revision"] += 1
        elif op == "decision.record":
            self.version(e, o, "expected_exploration_revision")
            self.check_exploration_evidence(state)
            action = o.get("action")
            require(action in {"select", "pivot", "retire", "stop", "deliver"}, "Invalid decision action")
            c = self.exploration_get(state, "candidates", o.get("candidate_id")) if action != "stop" else None
            require(c is None or c["status"] != "retired", "Candidate already retired")
            alternatives = list_text(o.get("alternatives"), "alternatives", empty=True)
            require(len(set(alternatives)) == len(alternatives), "Duplicate alternatives")
            for alt in alternatives:
                self.exploration_get(state, "candidates", alt)
                require(c is None or alt != c["id"], "Chosen candidate cannot be its own alternative")
            refs = list_text(o.get("result_ids"), "result_ids", empty=True)
            require(len(set(refs)) == len(refs), "Duplicate result references")
            validations = [self.exploration_get(state, "validations", vid) for vid in refs]
            require(all(v["status"] == "completed" for v in validations), "Decisions require completed validation results")
            previous = e["active_candidate_id"]
            if action == "select":
                require(previous is None, "Use pivot to change an active direction", "invalid_transition")
            elif action == "pivot":
                require(previous is not None and previous != c["id"] and validations, "Pivot needs an old direction, different candidate and validation evidence", "invalid_transition")
                require(any(v["plan"]["candidate_id"] in {previous, c["id"]} for v in validations), "Pivot evidence unrelated to either direction")
            elif action == "deliver":
                require(previous == c["id"], "Deliver must reference the selected direction")
                require(any(v["plan"]["candidate_id"] == c["id"] and v["result"]["outcome"] == "positive" for v in validations), "Deliver requires positive evidence for selected direction", "insufficient_evidence")
            if action in {"stop", "deliver"}:
                require(not any(v["status"] in {"planned", "running"} for v in e["validations"]), "Finish or cancel outstanding validations", "validation_pending")
                residual = list_text(o.get("residual_question_ids"), "residual_question_ids", empty=True)
                require(len(set(residual)) == len(residual) and set(residual) == {q["id"] for q in e["questions"] if q["status"] == "open"}, "Final decision must disclose all open questions")
            affected_id = previous if action in {"pivot", "stop"} else c["id"] if action == "retire" else None
            affected_ids = {t["id"] for t in state["tasks"] if affected_id and t.get("candidate_id") == affected_id and t["status"] not in self.TERMINAL}
            while True:
                downstream = {t["id"] for t in state["tasks"] if t["status"] not in self.TERMINAL and set(t["dependencies"]) & affected_ids}
                if downstream <= affected_ids: break
                affected_ids |= downstream
            affected = [t for t in state["tasks"] if t["id"] in affected_ids]
            dispositions = o.get("task_dispositions", [])
            require(isinstance(dispositions, list) and all(isinstance(d, dict) for d in dispositions), "Invalid task dispositions")
            require(len({d.get("id") for d in dispositions}) == len(dispositions) and {d.get("id") for d in dispositions} == {t["id"] for t in affected}, "Explicitly dispose every unfinished affected task", "task_impact_unresolved")
            for d in dispositions:
                require(set(d) == {"id", "expected_task_revision", "action", "reason"}, "Invalid task disposition fields")
                t = self.get(state, "tasks", d["id"])
                self.version(t, d, "expected_task_revision")
                require(d.get("action") in {"cancel", "retain"}, "Invalid task disposition action")
                text(d.get("reason"), "task disposition reason")
                if d["action"] == "cancel":
                    if t["lease"]: self.release(state, t, "Direction decision cancelled task")
                    t["status"] = "cancelled"
                t.setdefault("direction_decisions", []).append({"decision_id": item_id, "action": d["action"], "reason": d["reason"]})
                t["revision"] += 1
            result = {"id": item_id, "action": action, "candidate_id": c["id"] if c else None, "previous_candidate_id": previous,
                      "alternatives": alternatives, "result_ids": refs, "rationale": text(o.get("rationale")), "evidence": self.refs(o.get("evidence")),
                      "residual_question_ids": o.get("residual_question_ids", []), "task_dispositions": copy.deepcopy(dispositions), "created_at": stamp()}
            if action in {"select", "pivot"}:
                if previous:
                    old = self.exploration_get(state, "candidates", previous)
                    old["status"] = "candidate"; old["revision"] += 1
                c["status"] = "selected"; c["revision"] += 1; e["active_candidate_id"] = c["id"]
            elif action == "retire":
                require(not any(v["plan"]["candidate_id"] == c["id"] and v["status"] in {"planned", "running"} for v in e["validations"]), "Finish or cancel candidate validations before retirement", "validation_pending")
                c["status"] = "retired"; c["revision"] += 1
                if previous == c["id"]: e["active_candidate_id"] = None
            elif action in {"stop", "deliver"}:
                e["status"] = "closed"
                if action == "stop" and previous:
                    old = self.exploration_get(state, "candidates", previous)
                    old["status"] = "candidate"; old["revision"] += 1; e["active_candidate_id"] = None
            e["decisions"].append(result)
        e["revision"] += 1
        return copy.deepcopy(result)

    def wake_live(self, state, request):
        scheduler = state["scheduler"]
        wake = scheduler["active_wake"]
        require(wake and wake["id"] == request.get("wake_id") and wake["fence"] == request.get("wake_fence"),
                "Wake identity or fence mismatch", "stale_wake")
        require(wake["actor_id"] == request["actor_id"] == state["coordinator_id"], "Wrong wake actor", "unauthorized")
        require(wake["expires_at"] > time.time(), "Wake expired", "wake_expired")
        require(state["control"]["status"] == "running" and scheduler["enabled"], "Project paused or scheduler disabled", "paused")
        require(time.time() < scheduler["expires_at"], "Schedule deadline reached", "schedule_expired")
        return wake

    def close_wake(self, state, outcome):
        scheduler = state["scheduler"]
        wake = scheduler["active_wake"]
        if wake:
            wake.update(status=outcome, ended_at=stamp())
            scheduler["wakes"].append(copy.deepcopy(wake))
            scheduler["active_wake"] = None
            scheduler["fencing_token"] += 1
            scheduler["revision"] += 1

    def authorize_request(self, state, request):
        actor = request["actor_id"]
        names = [o.get("op") for o in request["operations"]]
        operator = actor == state["operator_id"]
        coordinator = actor == state["coordinator_id"]
        control = any(n in self.CONTROL_OPS for n in names)
        if control:
            require(operator and len(names) == 1, "Controls require operator and standalone transaction", "unauthorized")
            return
        if any(n in {"wake.begin", "wake.end"} for n in names):
            require(state["execution_mode"] == "background", "Foreground projects do not use wakes", "unauthorized")
            require(coordinator and len(names) == 1, "Wake operation requires coordinator and standalone transaction", "unauthorized")
            if names == ["wake.end"]: self.wake_live(state, request)
            return
        if all(n in self.RECONCILE_OPS for n in names):
            require(operator or coordinator, "Reconciliation requires coordinator or operator", "unauthorized")
            return
        if all(n in self.MAINTENANCE_OPS for n in names) and state["project_status"] == "completed":
            require(operator or coordinator, "Maintenance requires coordinator or operator", "unauthorized")
            return
        require(state["control"]["status"] == "running", "Project is paused", "paused")
        require(state["project_status"] == "active", "Project not active", "project_inactive")
        require(time.time() < state["scheduler"]["expires_at"], "Schedule deadline reached", "schedule_expired")
        require(time.time() >= state["scheduler"]["not_before"], "Project start time not reached", "not_due")
        if operator:
            wake = state["scheduler"]["active_wake"]
            require(not wake or wake["expires_at"] <= time.time(), "Operator setup would race an active wake", "wake_active")
        elif coordinator:
            if state["execution_mode"] == "background": self.wake_live(state, request)
        else:
            require(all(n in self.WORKER_OPS for n in names), "Worker cannot coordinate", "unauthorized")
            self.actor_unit(state, actor)

    def shutdown_closure(self, state):
        """Global admission closure, independent of host state and wake quotas."""
        if state["project_status"] == "completed": return "completed"
        if state["control"]["status"] == "paused": return "paused"
        if time.time() >= state["scheduler"]["expires_at"]: return "expired"
        return None

    def shutdown_guard(self, state):
        scheduler = state["scheduler"]
        return {"control_revision": state["control"]["revision"],
                "fencing_token": scheduler["fencing_token"],
                "binding_revision": scheduler.get("binding_revision", 0),
                "binding": copy.deepcopy(scheduler["binding"]),
                "project_status": state["project_status"], "expires_at": scheduler["expires_at"]}

    def shutdown_current(self, state, intent):
        return bool(self.shutdown_closure(state)) and intent["guard"] == self.shutdown_guard(state)

    def shutdown_intent(self, state, identity):
        matches = [i for i in state.get("shutdown_intents", []) if i["id"] == identity]
        require(matches, "Shutdown intent has not been prepared", "shutdown_not_prepared")
        return matches[0]

    def shutdown_operation(self, state, o, actor):
        scheduler = state["scheduler"]
        identity = ident(o.get("id"))
        if o["op"] == "shutdown.prepare":
            closure = self.shutdown_closure(state)
            require(closure, "Close all business admission before pausing the host", "shutdown_admission_open")
            require(state["execution_mode"] == "background" and scheduler["binding"],
                    "Host shutdown requires the original background binding", "shutdown_unbound")
            # Expiry denies every business operation even if old credentials remain in the ledger.
            now = time.time()
            usable = now < scheduler["expires_at"] and (
                (scheduler["active_wake"] and scheduler["active_wake"]["expires_at"] > now) or
                any(t["lease"] and t["lease"]["expires_at"] > now for t in state["tasks"]))
            require(not usable, "Closure still retains usable work credentials", "shutdown_live_credentials")
            intents = state.setdefault("shutdown_intents", [])
            require(not any(i["id"] == identity for i in intents), "Shutdown ID already used", "idempotency_conflict")
            require(not any(self.shutdown_current(state, i) for i in intents),
                    "Use the existing current shutdown intent", "shutdown_pending")
            intent = {"id": identity, "status": "prepared", "actor_id": actor,
                      "binding": copy.deepcopy(scheduler["binding"]), "guard": self.shutdown_guard(state),
                      "scheduler_revision": scheduler["revision"], "closure_basis": closure,
                      "credentials_usable": False, "prepared_at": stamp(), "prepared_at_unix": now,
                      "prepared_revision": state["revision"] + 1, "evidence": self.refs(o.get("evidence"))}
            intents.append(intent)
        else:
            intent = self.shutdown_intent(state, identity)
            require(self.shutdown_current(state, intent), "Shutdown intent is stale; inspect and prepare afresh", "shutdown_stale")
            require(intent["status"] == "prepared", "Shutdown already confirmed", "shutdown_already_confirmed")
            self.unchanged(intent["evidence"])
            observation = o.get("host_observation")
            require(isinstance(observation, dict), "Host observation required", "shutdown_observation_invalid")
            require(all(observation.get(k) == intent["binding"].get(k) for k in ("automation_id", "thread_id", "host_id")) and
                    observation.get("status") == "PAUSED", "Observation must show the original bound host PAUSED", "shutdown_host_mismatch")
            observed = observation.get("observed_at_unix")
            require(type(observed) in (int, float) and math.isfinite(observed) and
                    intent["prepared_at_unix"] <= observed <= time.time() + 5,
                    "Host observation must follow prepare and not be in the future", "shutdown_observation_invalid")
            if "configuration_updated_at_unix" in observation:
                updated = observation["configuration_updated_at_unix"]
                require(type(updated) in (int, float) and math.isfinite(updated) and
                        intent["prepared_at_unix"] <= updated <= observed,
                        "Host configuration time does not establish a post-prepare pause", "shutdown_order_unproven")
            intent["confirmation"] = {"actor_id": actor, "confirmed_at": stamp(),
                "confirmed_revision": state["revision"] + 1, "host_observation": copy.deepcopy(observation),
                "evidence": self.refs(o.get("evidence")), "verification": "caller_supplied_evidence_not_host_authentication",
                "strict_order_proven": False}
            intent["status"] = "confirmed"
        return copy.deepcopy(intent)

    def operation(self, con, state, operation, actor, request):
        o, op = operation, operation.get("op")
        scheduler = state["scheduler"]
        if op in self.SHUTDOWN_OPS:
            return self.shutdown_operation(state, o, actor)
        if op == "coordinator.takeover":
            raise RuntimeErrorDetail("V4 uses a stable coordinator identity and fenced wakes; raw takeover is disabled", "unauthorized")
        if op == "exploration.budget.set":
            return self.exploration_operation(state, o, actor)
        if op in {"control.pause", "control.resume"}:
            control = state["control"]
            self.version(control, o, "expected_control_revision")
            if op == "control.pause":
                self.close_wake(state, "paused")
                for task in state["tasks"]:
                    if task["lease"]: self.release(state, task, "Operator paused project")
                control["status"] = "paused"
                scheduler["blocked_reason"] = "operator_paused"
            else:
                require(state["project_status"] == "active", "Cannot resume completed project", "project_inactive")
                control["status"] = "running"
                scheduler["blocked_reason"] = None
            control["reason"] = request["reason"]
            control["revision"] += 1
            scheduler["revision"] += 1
            return copy.deepcopy(control)
        if op in {"scheduler.bind", "scheduler.enable", "budget.set"}:
            if op != "budget.set":
                require(state["execution_mode"] == "background", "Foreground projects cannot bind a scheduler", "unauthorized")
                self.auth(state, "configure_future_triggers")
            self.version(scheduler, o, "expected_scheduler_revision")
            if op == "scheduler.bind":
                require(not scheduler["active_wake"], "Cannot replace scheduler while a wake is active", "wake_active")
                require(type(o.get("enabled", False)) is bool, "enabled must be boolean")
                binding = {"automation_id": text(o.get("automation_id")), "thread_id": text(o.get("thread_id")),
                    "host_id": text(o.get("host_id")), "evidence": self.refs(o.get("evidence"))}
                oid = o.get("coordinator_outbox_id")
                if oid is not None:
                    ident(oid)
                    item = self.get(state, "outbox", oid)
                    require(self.coordinator_creation_matches(item, binding),
                            "Coordinator outbox must be delivered and match the coordinator unit and exact scheduler host/thread")
                else:
                    matches = [i for i in state["outbox"] if self.coordinator_creation_matches(i, binding)]
                    # Rebinding the same carrier preserves its selected record
                    # even if an unrelated duplicate receipt was later recorded.
                    previous = scheduler["binding"] or {}
                    existing = [i for i in matches if i["id"] == previous.get("coordinator_outbox_id")]
                    require(len(matches) <= 1 or existing, "Multiple coordinator creations match; provide coordinator_outbox_id", "dispatch_ambiguous")
                    oid = (existing or matches)[0]["id"] if (existing or matches) else None
                binding["coordinator_outbox_id"] = oid
                scheduler["binding"] = binding
                scheduler["binding_revision"] = scheduler.get("binding_revision", 0) + 1
                scheduler["enabled"] = o.get("enabled", False)
                if scheduler["enabled"]: self.check_coordinator_registration(state)
            elif op == "scheduler.enable":
                require(type(o.get("enabled")) is bool, "enabled must be boolean")
                require(not o["enabled"] or scheduler["binding"], "Bind scheduler before enabling")
                if o["enabled"]: self.check_coordinator_registration(state)
                scheduler["enabled"] = o["enabled"]
                if not o["enabled"]: self.close_wake(state, "disabled")
            else:
                require(not scheduler["active_wake"] or scheduler["active_wake"]["expires_at"] <= time.time(), "Pause before changing active wake budgets", "wake_active")
                values = o.get("budgets")
                require(isinstance(values, dict) and values and not set(values) - set(self.DEFAULT_BUDGETS), "Invalid budget fields")
                require(all(type(v) is int and v >= 1 for v in values.values()), "Budgets must be positive integers")
                require(all(v <= scheduler["budget_ceiling"][k] for k, v in values.items()), "Budget exceeds authorized ceiling", "resource_limit")
                scheduler["budgets"].update(values)
            scheduler["revision"] += 1
            return copy.deepcopy(scheduler)
        if op == "wake.begin":
            self.auth(state, "configure_future_triggers")
            wid = ident(o.get("id"))
            plan = self.plan_state(state)
            if plan["decision"] != "run":
                scheduler["blocked_reason"] = plan["reason"]
                scheduler["revision"] += 1
                return {"admitted": False, "reason": plan["reason"], "plan": plan}
            ttl = o.get("lease_seconds", scheduler["budgets"]["max_wake_seconds"])
            require(type(ttl) in {int, float} and math.isfinite(ttl) and 0 < ttl <= scheduler["budgets"]["max_wake_seconds"], "Invalid wake lease duration")
            require(not any(w["id"] == wid for w in scheduler["wakes"]), "Wake ID already used", "idempotency_conflict")
            require(not scheduler["active_wake"] or scheduler["active_wake"]["id"] != wid, "Expired wake ID cannot be reused", "idempotency_conflict")
            if scheduler["active_wake"]: self.close_wake(state, "expired")
            scheduler["fencing_token"] += 1
            wake = {"id": wid, "fence": scheduler["fencing_token"], "actor_id": actor,
                    "started_at": stamp(), "expires_at": min(time.time() + ttl, scheduler["expires_at"]), "actions": 0, "status": "running"}
            scheduler["active_wake"] = wake
            scheduler["usage"]["wakes"] += 1
            scheduler["blocked_reason"] = None
            scheduler["revision"] += 1
            return {"admitted": True, "wake_id": wid, "wake_fence": wake["fence"], "expires_at": wake["expires_at"]}
        if op == "wake.end":
            outcome = o.get("outcome")
            require(outcome in {"progress", "idle", "error"}, "Invalid wake outcome")
            self.wake_live(state, request)
            if outcome == "progress":
                scheduler["idle_streak"] = scheduler["error_streak"] = 0
                delay = scheduler["cooldown_seconds"]
            else:
                key = outcome + "_streak"
                scheduler[key] += 1
                other = "idle_streak" if outcome == "error" else "error_streak"
                scheduler[other] = 0
                delay = min(scheduler["max_backoff_seconds"], max(1, scheduler["cooldown_seconds"]) * 2 ** min(scheduler[key] - 1, 30))
            self.close_wake(state, outcome)
            scheduler["next_wake_at"] = time.time() + delay
            return {"outcome": outcome, "next_wake_at": scheduler["next_wake_at"], "plan": self.plan_state(state)}
        if state["execution_mode"] == "background" and actor == state["coordinator_id"] and op not in self.RECONCILE_OPS and state["project_status"] != "completed":
            wake = self.wake_live(state, request)
            require(wake["actions"] < scheduler["budgets"]["max_actions_per_wake"], "Wake action budget exhausted", "resource_limit")
            wake["actions"] += 1
        if op == "task.claim":
            require(scheduler["usage"]["claims"] < scheduler["budgets"]["max_claims"], "Claim budget exhausted", "resource_limit")
            scheduler["usage"]["claims"] += 1
            scheduler["revision"] += 1
        if op == "outbox.begin":
            require(scheduler["usage"]["dispatches"] < scheduler["budgets"]["max_dispatches"], "Dispatch budget exhausted", "resource_limit")
            scheduler["usage"]["dispatches"] += 1
            scheduler["revision"] += 1
        if op.startswith(("question.", "candidate.", "validation.", "decision.")):
            return self.exploration_operation(state, o, actor)
        if op == "task.create" and o.get("candidate_id") is not None:
            candidate = self.exploration_get(state, "candidates", o["candidate_id"])
            require(candidate["status"] != "retired", "Cannot assign task to retired candidate")
        if op == "project.complete":
            require(state["exploration"]["status"] == "closed", "Exploration must have a final decision", "exploration_open")
            self.check_exploration_evidence(state)
        result = self.base_operation(con, state, o, actor)
        if op == "task.create":
            self.get(state, "tasks", o["id"])["candidate_id"] = o.get("candidate_id")
            result["candidate_id"] = o.get("candidate_id")
        if op == "project.complete":
            self.close_wake(state, "completed")
            scheduler["enabled"] = False
            scheduler["blocked_reason"] = "project_completed"
            scheduler["revision"] += 1
        return result

    def base_operation(self, con, state, operation, actor):
        o, op = operation, operation.get("op")
        coordinator = actor in {state["coordinator_id"], state["operator_id"]}
        require(op in self.WORKER_OPS or coordinator or op == "coordinator.takeover", "Coordinator operation required", "unauthorized")
        if op in self.WORKER_OPS:
            self.actor_unit(state, actor)
        if op == "task.cancel":
            task = self.get(state, "tasks", o.get("id"))
            self.version(task, o, "expected_task_revision")
            require(task["status"] not in self.TERMINAL, "Task already terminal")
            cancellation = {"reason": text(o.get("reason")), "evidence": self.refs(o.get("evidence")), "at": stamp()}
            if task["lease"]: self.release(state, task, "Task explicitly cancelled")
            task["status"] = "cancelled"; task["revision"] += 1; task["cancellation"] = cancellation
            return copy.deepcopy(task)
        if op == "unit.create":
            self.auth(state, "adjust_units_within_scope")
            uid = ident(o.get("id")); require(not any(u["id"] == uid for u in state["units"]), "Unit exists")
            parent = self.get(state, "units", o.get("parent_id", "coordinator"))
            require(parent["status"] == "active", "Parent unit not active")
            unit = {"id": uid, "revision": 1, "name": text(o.get("name")), "mission": text(o.get("mission")),
                    "parent_id": parent["id"], "status": "active", "creation_reason": text(o.get("creation_reason")), "exit_condition": text(o.get("exit_condition"))}
            state["units"].append(unit); return copy.deepcopy(unit)
        if op in {"unit.update", "unit.status"}:
            self.auth(state, "adjust_units_within_scope")
            unit = self.get(state, "units", o.get("id")); self.version(unit, o, "expected_unit_revision")
            require(unit["status"] != "retired", "Retired unit cannot change")
            if op == "unit.update":
                for key in ["name", "mission", "exit_condition"]:
                    if key in o: unit[key] = text(o[key], key)
            else:
                require(unit["id"] != "coordinator" and o.get("status") in {"active", "dormant", "retired"}, "Invalid unit lifecycle")
                owned = [t for t in state["tasks"] if t["owner_unit_id"] == unit["id"] and t["status"] not in self.TERMINAL]
                require(o["status"] == "active" or not any(t["status"] == "running" for t in owned), "Unit has running tasks")
                if o["status"] == "retired":
                    require(not owned and not any(c["unit_id"] == unit["id"] and c["status"] != "retired" for c in state["chats"]), "Transfer work and chats before retirement")
                unit["status"] = o["status"]
            unit["revision"] += 1; return copy.deepcopy(unit)
        if op == "unit.merge":
            self.auth(state, "adjust_units_within_scope")
            source = self.get(state, "units", o.get("source_id")); target = self.get(state, "units", o.get("target_id"))
            self.version(source, o, "expected_source_revision"); self.version(target, o, "expected_target_revision")
            require(source["id"] != "coordinator" and source["id"] != target["id"] and source["status"] != "retired" and target["status"] == "active", "Invalid merge")
            require(not any(u["parent_id"] == source["id"] and u["status"] != "retired" for u in state["units"]), "Move live children first")
            for task in state["tasks"]:
                if task["owner_unit_id"] == source["id"] and task["status"] not in self.TERMINAL:
                    require(task["status"] != "running", "Revoke running tasks before merge")
                    task["owner_unit_id"] = target["id"]; task["revision"] += 1; task["fencing_token"] += 1
            for chat in state["chats"]:
                if chat["unit_id"] == source["id"] and chat["status"] != "retired":
                    chat["unit_id"] = target["id"]; chat["revision"] += 1
            source.update(status="retired", merged_into=target["id"]); source["revision"] += 1; target["revision"] += 1
            return {"source_id": source["id"], "target_id": target["id"]}
        if op == "chat.bind":
            self.auth(state, "create_long_lived_chats")
            cid = ident(o.get("id")); require(not any(c["id"] == cid for c in state["chats"]), "Chat binding exists")
            require(cid not in {state["coordinator_id"], state["operator_id"]}, "Worker identity cannot shadow controller")
            unit = self.get(state, "units", o.get("unit_id")); require(unit["status"] == "active", "Unit not active")
            chat = {"id": cid, "revision": 1, "unit_id": unit["id"], "host_id": text(o.get("host_id")), "thread_id": text(o.get("thread_id")), "title": o.get("title", cid), "status": "active"}
            state["chats"].append(chat); return copy.deepcopy(chat)
        if op == "chat.status":
            chat = self.get(state, "chats", o.get("id")); self.version(chat, o, "expected_chat_revision")
            require(o.get("status") in {"active", "dormant", "retired"} and chat["status"] != "retired", "Invalid chat lifecycle")
            require(not any(t["lease"] and t["lease"]["actor_id"] == chat["id"] for t in state["tasks"]), "Chat owns running leases")
            require(o["status"] != "active" or self.get(state, "units", chat["unit_id"])["status"] == "active", "Unit not active")
            chat["status"] = o["status"]; chat["revision"] += 1; return copy.deepcopy(chat)
        if op == "task.create":
            tid = ident(o.get("id")); require(not any(t["id"] == tid for t in state["tasks"]), "Task exists")
            unit = self.get(state, "units", o.get("owner_unit_id")); require(unit["status"] == "active", "Owner inactive")
            task = {"id": tid, "revision": 1, "title": text(o.get("title")), "owner_unit_id": unit["id"],
                    "acceptance": list_text(o.get("acceptance")), "inputs": o.get("inputs", []), "dependencies": o.get("dependencies", []),
                    "fencing_token": 0, "lease": None, "attempts": [], "artifacts": [], "accepted_artifacts": [], "verification": [], "checkpoints": []}
            require(isinstance(task["inputs"], list) and isinstance(task["dependencies"], list), "Invalid task lists")
            task["status"] = "ready" if self.ready(state, task) else "blocked"
            state["tasks"].append(task); return copy.deepcopy(task)
        if op == "task.reassign":
            task = self.get(state, "tasks", o.get("id")); self.version(task, o, "expected_task_revision")
            require(task["status"] not in self.TERMINAL, "Terminal task cannot move")
            unit = self.get(state, "units", o.get("owner_unit_id")); require(unit["status"] == "active", "Target owner inactive")
            if task["status"] == "running":
                require(o.get("revoke") is True, "Running lease requires explicit revoke")
            self.release(state, task, "Owner reassigned with explicit fencing")
            task["owner_unit_id"] = unit["id"]; return copy.deepcopy(task)
        if op == "task.claim":
            task = self.get(state, "tasks", o.get("id")); self.version(task, o, "expected_task_revision")
            require(task["status"] in {"ready", "blocked"}, "Task not claimable", "claim_conflict")
            require(self.actor_unit(state, actor) == task["owner_unit_id"], "Task outside actor unit", "unauthorized")
            require(self.ready(state, task), "Dependencies not accepted", "dependency_not_ready")
            require(len(task["attempts"]) < state["limits"]["max_attempts_per_task"], "Attempt budget exhausted", "resource_limit")
            ttl = o.get("lease_seconds", 600)
            require(type(ttl) in {int, float} and 0 < ttl <= state["limits"]["max_lease_seconds"], "Invalid lease duration")
            inputs = self.inputs(state, task); task["fencing_token"] += 1
            aid = uuid.uuid4().hex; lid = uuid.uuid4().hex
            directory = ".adaptive-v4/attempts/{}/{}-{}".format(task["id"], task["fencing_token"], aid)
            self.path(directory).mkdir(parents=True, exist_ok=False)
            lease = {"actor_id": actor, "lease_id": lid, "fencing_token": task["fencing_token"], "expires_at": time.time() + ttl, "attempt_id": aid}
            task["lease"] = lease; task["status"] = "running"; task["revision"] += 1
            task["attempts"].append({"id": aid, "actor_id": actor, "lease_id": lid, "fencing_token": task["fencing_token"],
                                     "directory": directory, "status": "running", "started_at": stamp(), "ended_at": None,
                                     "inputs_snapshot": inputs, "checkpoints": [], "artifacts": []})
            for item in inputs:
                if "memory_id" in item:
                    memory = self.get(state, "memories", item["memory_id"])
                    if task["id"] not in memory["used_by"]: memory["used_by"].append(task["id"])
            result = copy.deepcopy(lease); result.update(task_id=task["id"], task_revision=task["revision"], attempt_dir=directory)
            return result
        if op in {"task.heartbeat", "task.checkpoint", "task.submit"}:
            task = self.get(state, "tasks", o.get("id")); lease = self.live(state, task, actor, o); attempt = self.attempt(task)
            if op == "task.heartbeat":
                ttl = o.get("lease_seconds", 600)
                require(type(ttl) in {int, float} and 0 < ttl <= state["limits"]["max_lease_seconds"], "Invalid lease duration")
                lease["expires_at"] = max(lease["expires_at"], time.time() + ttl)
                return copy.deepcopy(lease)
            if op == "task.checkpoint":
                require(isinstance(o.get("data"), dict), "Checkpoint data must be object")
                checkpoint = {"data": copy.deepcopy(o["data"]), "timestamp": stamp(), "attempt_id": attempt["id"], "actor_id": actor}
                attempt["checkpoints"].append(checkpoint); task["checkpoints"].append(checkpoint)
                return checkpoint
            self.version(task, o, "expected_task_revision")
            require(self.inputs(state, task) == attempt["inputs_snapshot"], "Task inputs changed", "stale_input")
            refs = self.refs(o.get("artifacts")); directory = self.path(attempt["directory"])
            require(all(inside(self.path(item["ref"], True), directory) for item in refs), "Artifact outside attempt directory", "path_escape")
            task["artifacts"] = refs; task["lease"] = None; task["status"] = "review"; task["revision"] += 1
            attempt.update(status="submitted", ended_at=stamp(), artifacts=refs, summary=text(o.get("summary")))
            return {"task_id": task["id"], "task_revision": task["revision"], "status": "review"}
        if op == "task.review":
            task = self.get(state, "tasks", o.get("id")); self.version(task, o, "expected_task_revision")
            require(task["status"] == "review" and o.get("verdict") in {"pass", "rework"}, "Invalid review")
            require(self.inputs(state, task) == task["attempts"][-1]["inputs_snapshot"], "Task inputs changed", "stale_input")
            self.unchanged(task["artifacts"]); evidence = self.refs(o.get("evidence"))
            task["verification"].append({"verdict": o["verdict"], "reviewer_id": actor, "method": text(o.get("method")), "evidence": evidence, "timestamp": stamp()})
            if o["verdict"] == "pass":
                task["accepted_artifacts"] = self.accepted_copy(con, task, task["artifacts"]); task["status"] = "done"
            else:
                task["status"] = "ready"; task["fencing_token"] += 1
            task["revision"] += 1; return copy.deepcopy(task)
        if op == "recover":
            recovered = []
            for task in state["tasks"]:
                if o.get("task_id") and task["id"] != o["task_id"]: continue
                if task["lease"] and task["lease"]["expires_at"] <= time.time():
                    self.release(state, task, "Lease expired; old process may still exist")
                    recovered.append(task["id"])
            return {"recovered_tasks": recovered}
        if op == "coordinator.takeover":
            require(o.get("previous_actor_id") == state["coordinator_id"] and actor != state["coordinator_id"], "Invalid takeover")
            require(not any(chat["id"] == actor for chat in state["chats"]), "Coordinator identity cannot shadow worker")
            proof = self.refs(o.get("stopped_evidence")); previous = state["coordinator_id"]
            state["coordinator_id"] = actor
            return {"previous_actor_id": previous, "actor_id": actor, "stopped_evidence": proof}
        if op in {"memory.put", "memory.propose"}:
            self.auth(state, "maintain_project_memory")
            mid = ident(o.get("id")); require(not any(m["id"] == mid for m in state["memories"]), "Memory already exists")
            require(o.get("kind") in {"constraint", "decision", "fact", "experience", "hypothesis", "question"}, "Invalid memory kind")
            memory = {"id": mid, "revision": 1, "kind": o["kind"], "statement": text(o.get("statement")),
                      "status": "candidate" if op == "memory.propose" else "active", "sources": self.refs(o.get("sources")),
                      "scope": o.get("scope", state["project_id"]), "confidence": o.get("confidence"), "used_by": [], "supersedes": [],
                      "proposed_by": actor, "valid_until": o.get("valid_until")}
            if memory["valid_until"]:
                require(datetime.fromisoformat(memory["valid_until"]).tzinfo is not None, "Memory expiry requires timezone")
            state["memories"].append(memory); return copy.deepcopy(memory)
        if op == "memory.accept":
            self.auth(state, "maintain_project_memory")
            memory = self.get(state, "memories", o.get("id")); self.version(memory, o, "expected_memory_revision")
            require(memory["status"] == "candidate", "Memory not candidate"); self.unchanged(memory["sources"])
            memory["status"] = "active"; memory["revision"] += 1; return copy.deepcopy(memory)
        if op == "memory.supersede":
            self.auth(state, "maintain_project_memory")
            old = self.get(state, "memories", o.get("old_id")); new = self.get(state, "memories", o.get("new_id"))
            self.version(old, o, "expected_old_revision"); self.version(new, o, "expected_new_revision")
            require(old["id"] != new["id"] and old["status"] in {"active", "candidate"} and new["status"] == "active", "Invalid supersession")
            self.unchanged(new["sources"])
            old["status"] = "superseded"; old["replaced_by"] = new["id"]; old["revision"] += 1
            new["supersedes"].append(old["id"]); new["revision"] += 1
            for tid in old["used_by"]:
                self.get(state, "tasks", tid).setdefault("knowledge_impacts", []).append({"memory_id": old["id"], "replacement_id": new["id"]})
            return {"old_id": old["id"], "new_id": new["id"]}
        if op == "checkpoint":
            cid = ident(o.get("id")); require(not any(c["id"] == cid for c in state["checkpoints"]), "Checkpoint exists")
            require(isinstance(o.get("data"), dict), "Checkpoint data must be object")
            cp = {"id": cid, "revision": state["revision"] + 1, "data": copy.deepcopy(o["data"]), "actor_id": actor, "timestamp": stamp()}
            state["checkpoints"].append(cp); return cp
        if op in {"artifact.export", "artifact.restore"}:
            return self.materialize(con, state, o, restore=op == "artifact.restore")
        if op == "outbox.enqueue":
            oid = ident(o.get("id")); require(not any(i["id"] == oid for i in state["outbox"]), "Outbox ID exists")
            kind = o.get("kind"); require(kind in {"create_chat", "send_message"}, "Invalid outbox kind")
            self.auth(state, "create_long_lived_chats" if kind == "create_chat" else "message_long_lived_chats")
            payload = o.get("payload"); require(isinstance(payload, dict), "Payload must be object")
            entity = self.get(state, "units", payload.get("unit_id")) if kind == "create_chat" else self.get(state, "chats", payload.get("chat_id"))
            require(entity["status"] == "active", "Activate outbox target before dispatch")
            if kind == "create_chat":
                require(sum(c["status"] == "active" for c in state["chats"]) + self.creation_reservations(state) < state["limits"]["max_active_chats"], "Chat creation reservations exceed cap", "resource_limit")
            item = {"id": oid, "revision": 1, "kind": kind, "payload": copy.deepcopy(payload), "status": "pending", "result": None}
            state["outbox"].append(item); return copy.deepcopy(item)
        if op in {"outbox.begin", "outbox.resolve", "outbox.reconcile"}:
            item = self.get(state, "outbox", o.get("id")); self.version(item, o, "expected_outbox_revision")
            if op == "outbox.begin":
                require(item["status"] == "pending", "Outbox must be reconciled before retry", "dispatch_ambiguous")
                target = self.get(state, "units", item["payload"]["unit_id"]) if item["kind"] == "create_chat" else self.get(state, "chats", item["payload"]["chat_id"])
                require(target["status"] == "active", "Outbox target no longer active")
                item["status"] = "inflight"
            elif op == "outbox.resolve":
                require(item["status"] == "inflight" and o.get("status") in {"delivered", "unknown"}, "Invalid dispatch resolution")
                result = o.get("result"); require(isinstance(result, dict), "Result must be object")
                if o["status"] == "delivered" and item["kind"] == "create_chat":
                    text(result.get("host_id")); text(result.get("thread_id"))
                item.update(status=o["status"], result=copy.deepcopy(result))
            else:
                require(item["status"] in {"inflight", "unknown"} and o.get("resolution") in {"delivered", "not_sent"}, "Invalid reconciliation")
                proof = self.refs(o.get("evidence"))
                if o["resolution"] == "delivered":
                    result = o.get("result"); require(isinstance(result, dict), "Result must be object")
                    if item["kind"] == "create_chat": text(result.get("host_id")); text(result.get("thread_id"))
                    item.update(status="delivered", result=copy.deepcopy(result))
                else:
                    item.update(status="pending", result=None)
                item.setdefault("reconciliations", []).append({"resolution": o["resolution"], "evidence": proof, "timestamp": stamp()})
            item["revision"] += 1; return copy.deepcopy(item)
        if op == "project.complete":
            require(state["tasks"] and all(t["status"] in {"done", "cancelled"} for t in state["tasks"]), "Project has unfinished tasks")
            require(all(item["status"] == "delivered" for item in state["outbox"]), "Project has unresolved dispatch intents")
            results = o.get("acceptance_results")
            require(isinstance(results, list) and [r.get("criterion") for r in results] == state["charter"]["acceptance"] and all(r.get("passed") is True for r in results), "Acceptance coverage missing")
            for task in state["tasks"]:
                if task["status"] == "done":
                    self.unchanged(task["accepted_artifacts"])
                    for review in task["verification"]: self.unchanged(review["evidence"])
            for result in results: result["evidence"] = self.refs(result.get("evidence"))
            state["completion"] = {"evidence": self.refs(o.get("evidence")), "acceptance_results": results, "timestamp": stamp()}
            state["project_status"] = "completed"; return {"status": "completed"}
        raise RuntimeErrorDetail("Unknown operation: " + str(op))

    def replay_guard(self, state, request, response):
        actor = request["actor_id"]
        self.authorize_request(state, request)
        for operation, result in zip(request["operations"], response["results"]):
            op = operation["op"]
            if op in self.SHUTDOWN_OPS:
                intent = self.shutdown_intent(state, operation["id"])
                require(self.shutdown_current(state, intent), "Shutdown replay is stale", "shutdown_stale")
                self.unchanged(state["authority_sources"])
                self.unchanged(intent["evidence"])
                if op == "shutdown.confirm": self.unchanged(intent["confirmation"]["evidence"])
            if op == "validation.start":
                validation = self.exploration_get(state, "validations", operation["id"])
                require(validation["status"] == "running", "Old validation start is no longer active", "invalid_transition")
                if validation["plan"]["inputs"]: self.unchanged(validation["plan"]["inputs"])
            if op in {"validation.finish", "decision.record"}: self.check_exploration_evidence(state)
            if op == "wake.begin" and result.get("admitted"):
                self.wake_live(state, dict(request, wake_id=result["wake_id"], wake_fence=result["wake_fence"]))
            if op == "outbox.begin":
                raise RuntimeErrorDetail("Dispatch already began; inspect or reconcile instead of replaying", "dispatch_ambiguous")
            if op == "task.claim":
                self.live(state, self.get(state, "tasks", operation["id"]), actor, result)
            elif op in {"task.heartbeat", "task.checkpoint"}:
                self.live(state, self.get(state, "tasks", operation["id"]), actor, operation)
            elif op == "task.submit":
                task = self.get(state, "tasks", operation["id"])
                require(task["status"] in {"review", "done"} and task["fencing_token"] == operation["fencing_token"] and task["attempts"][-1]["lease_id"] == operation["lease_id"] and task["attempts"][-1]["actor_id"] == actor, "Old submission replay is stale", "stale_lease")

    def transact(self, request):
        request = copy.deepcopy(request)
        require(isinstance(request, dict), "Request must be object")
        actor = text(request.get("actor_id"), "actor_id"); key = ident(request.get("idempotency_key")); text(request.get("reason"), "reason")
        ops = request.get("operations"); require(isinstance(ops, list) and ops and all(isinstance(o, dict) for o in ops), "Operations required")
        require(not any(o.get("op") in self.SHUTDOWN_OPS for o in ops) or len(ops) == 1,
                "Persist each shutdown stage in a standalone transaction", "shutdown_stage_boundary")
        stages = {}
        for o in ops:
            if o.get("op") in {"validation.plan", "validation.start", "validation.finish", "validation.cancel"}:
                vid = o.get("id")
                require(isinstance(vid, str), "Validation ID required")
                stages.setdefault(vid, []).append(o["op"])
        require(all(len(v) == 1 for v in stages.values()), "Persist each validation lifecycle stage in a separate transaction", "validation_stage_boundary")
        require(not any(o.get("op") == "coordinator.takeover" for o in ops) or len(ops) == 1, "Takeover must be standalone")
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            state = self.load(con); self.validate(state)
            previous = con.execute("SELECT hash,response FROM requests WHERE key=?", (key,)).fetchone()
            if previous:
                require(previous[0] == hash_json(request), "Idempotency key reused with different request", "idempotency_conflict")
                response = json.loads(previous[1]); self.replay_guard(state, request, response)
                response.update(state=state, revision=state["revision"], replayed=True)
                con.rollback(); return response
            if "expected_revision" in request:
                require(type(request["expected_revision"]) is int and request["expected_revision"] == state["revision"], "Global revision conflict", "stale_revision")
            self.authorize_request(state, request)
            # An operator may always pause even after an authorization file changed.
            if not (actor == state["operator_id"] and len(ops) == 1 and ops[0].get("op") == "control.pause"):
                self.unchanged(state["authority_sources"])
            results = [self.operation(con, state, op, actor, request) for op in ops]
            state["revision"] += 1; state["updated_at"] = stamp(); self.validate(state)
            event = {"id": "event-" + str(state["revision"]), "revision": state["revision"], "actor_id": actor,
                     "timestamp": state["updated_at"], "reason": request["reason"], "idempotency_key": key, "operations": copy.deepcopy(ops)}
            state["events"].append(event)
            response = {"revision": state["revision"], "results": results, "state": state, "replayed": False}
            con.execute("UPDATE project SET state=? WHERE id=1", (encode(state),))
            stored_response = {"revision": state["revision"], "results": results}
            con.execute("INSERT INTO requests VALUES (?,?,?)", (key, hash_json(request), encode(stored_response)))
            if self.allow_test_faults and os.environ.get("ADAPTIVE_V4_TEST_FAULT") == "before_commit": os._exit(91)
            con.commit(); return response
        except Exception:
            con.rollback(); raise
        finally:
            con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True); parser.add_argument("--allow-test-faults", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("plan")
    commands.add_parser("storage-recover").add_argument("--acknowledge-stopped-writers", action="store_true")
    commands.add_parser("init").add_argument("--charter", required=True)
    commands.add_parser("apply").add_argument("--request", required=True)
    for name in ["pause", "resume"]:
        command = commands.add_parser(name)
        command.add_argument("--actor", required=True)
        command.add_argument("--reason", required=True)
        command.add_argument("--key")
    args = parser.parse_args(argv)
    try:
        runtime = Runtime(args.project, args.allow_test_faults)
        if args.command == "status": result = runtime.read()
        elif args.command == "plan": result = runtime.plan()
        elif args.command == "storage-recover": result = runtime.storage_recover(args.acknowledge_stopped_writers)
        elif args.command in {"pause", "resume"}:
            state = runtime.read()
            result = runtime.transact({"actor_id": args.actor, "idempotency_key": args.key or (args.command + "-" + uuid.uuid4().hex),
                "reason": args.reason, "operations": [{"op": "control." + args.command,
                    "expected_control_revision": state["control"]["revision"]}]})
        else:
            data = json.loads(Path(args.charter if args.command == "init" else args.request).read_text(encoding="utf-8-sig"))
            result = runtime.init(data) if args.command == "init" else runtime.transact(data)
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    except (RuntimeErrorDetail, OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error), "code": getattr(error, "code", "runtime_error")}, ensure_ascii=False), file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
