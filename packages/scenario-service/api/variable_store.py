"""Scoped variable storage + merge.

Variables can be defined at four scopes; at run time a scenario sees them merged
with more specific scopes overriding more general ones:

    global  <  project  <  set(s)  <  scenario        (scenario wins)

Scenario-scoped variables live on the scenario document (``ScenarioDraft.variables``).
The other three scopes live here, in a single JSON document (``VARIABLES_FILE``;
``:memory:`` keeps them in-process for tests)::

    {"global": {NAME: VALUE},
     "project": {project_id: {NAME: VALUE}},
     "set": {set_id: {NAME: VALUE}}}
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .crypto import fingerprint


def merge_variables(
    global_v: dict | None,
    project_v: dict | None,
    set_vs: list[dict] | None,
    scenario_v: dict | None,
) -> dict[str, Any]:
    """Merge the scopes, lowest precedence first (scenario overrides all)."""
    out: dict[str, Any] = {}
    out.update(global_v or {})
    out.update(project_v or {})
    for sv in set_vs or []:
        out.update(sv or {})
    out.update(scenario_v or {})
    return out


class VariableStore:
    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._doc: dict[str, Any] = {
            "global": {}, "project": {}, "set": {},
            "secret_names": {"global": [], "project": {}, "set": {}},
        }
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                data = json.loads(self._path.read_text())
                sn = data.get("secret_names") or {}
                self._doc = {
                    "global": dict(data.get("global") or {}),
                    "project": dict(data.get("project") or {}),
                    "set": dict(data.get("set") or {}),
                    "secret_names": {
                        "global": list(sn.get("global") or []),
                        "project": dict(sn.get("project") or {}),
                        "set": dict(sn.get("set") or {}),
                    },
                }
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._doc, indent=2))
        tmp.replace(self._path)

    # -- accessors -----------------------------------------------------------

    def get_global(self) -> dict:
        return dict(self._doc["global"])

    def get_global_secrets(self) -> list[str]:
        return list(self._doc["secret_names"]["global"])

    def set_global(self, variables: dict, secrets: list[str] | None = None) -> dict:
        with self._lock:
            self._doc["global"] = dict(variables or {})
            self._doc["secret_names"]["global"] = list(secrets or [])
            self._save()
        return self.get_global()

    def get_project(self, project_id: str) -> dict:
        return dict(self._doc["project"].get(project_id) or {})

    def get_project_secrets(self, project_id: str) -> list[str]:
        return list(self._doc["secret_names"]["project"].get(project_id) or [])

    def set_project(self, project_id: str, variables: dict,
                    secrets: list[str] | None = None) -> dict:
        with self._lock:
            if variables:
                self._doc["project"][project_id] = dict(variables)
            else:
                self._doc["project"].pop(project_id, None)
            if secrets:
                self._doc["secret_names"]["project"][project_id] = list(secrets)
            else:
                self._doc["secret_names"]["project"].pop(project_id, None)
            self._save()
        return self.get_project(project_id)

    def get_set(self, set_id: str) -> dict:
        return dict(self._doc["set"].get(set_id) or {})

    def get_set_secrets(self, set_id: str) -> list[str]:
        return list(self._doc["secret_names"]["set"].get(set_id) or [])

    def set_set(self, set_id: str, variables: dict,
                secrets: list[str] | None = None) -> dict:
        with self._lock:
            if variables:
                self._doc["set"][set_id] = dict(variables)
            else:
                self._doc["set"].pop(set_id, None)
            if secrets:
                self._doc["secret_names"]["set"][set_id] = list(secrets)
            else:
                self._doc["secret_names"]["set"].pop(set_id, None)
            self._save()
        return self.get_set(set_id)

    # -- merge ---------------------------------------------------------------

    def effective(
        self,
        project_id: str | None,
        set_ids: list[str] | None,
        scenario_v: dict | None,
    ) -> dict[str, Any]:
        return merge_variables(
            self.get_global(),
            self.get_project(project_id) if project_id else {},
            [self.get_set(sid) for sid in (set_ids or [])],
            scenario_v or {},
        )

    def secret_names(
        self, project_id: str | None, set_ids: list[str] | None,
        scenario_secrets: list[str] | None,
    ) -> set[str]:
        names: set[str] = set(self.get_global_secrets())
        if project_id:
            names |= set(self.get_project_secrets(project_id))
        for sid in (set_ids or []):
            names |= set(self.get_set_secrets(sid))
        names |= set(scenario_secrets or [])
        return names

    def secret_values(
        self, project_id: str | None, set_ids: list[str] | None,
        scenario_v: dict | None, scenario_secrets: list[str] | None,
    ) -> list[str]:
        """The effective *values* of secret-marked variables (for redaction)."""
        eff = self.effective(project_id, set_ids, scenario_v)
        names = self.secret_names(project_id, set_ids, scenario_secrets)
        return [str(eff[n]) for n in names if eff.get(n) not in (None, "")]

    def breakdown(
        self,
        project_id: str | None,
        set_ids: list[str] | None,
        scenario_v: dict | None,
    ) -> dict:
        return {
            "global": self.get_global(),
            "project": self.get_project(project_id) if project_id else {},
            "sets": {sid: self.get_set(sid) for sid in (set_ids or [])},
            "scenario": scenario_v or {},
        }

    # -- secrets inventory ---------------------------------------------------

    def secret_refs(self) -> list[dict]:
        """Masked references to every secret-marked variable across all scopes.

        Returns ``{scope, scope_id, name, fingerprint}`` per secret — never the
        value. Used by the Secrets Vault inventory.
        """
        sn = self._doc["secret_names"]
        refs: list[dict] = []
        for name in sorted(sn.get("global") or []):
            val = str(self._doc["global"].get(name, ""))
            refs.append({"scope": "global", "scope_id": "", "name": name,
                         "fingerprint": fingerprint(val)})
        for scope in ("project", "set"):
            for scope_id in sorted(sn.get(scope) or {}):
                vals = self._doc[scope].get(scope_id) or {}
                for name in sorted(sn[scope].get(scope_id) or []):
                    refs.append({"scope": scope, "scope_id": scope_id, "name": name,
                                 "fingerprint": fingerprint(str(vals.get(name, "")))})
        return refs
