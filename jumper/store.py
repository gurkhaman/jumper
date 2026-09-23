"""Endpoint-scoped, atomically replaced project associations."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path


class StoreError(Exception):
    pass


class StoreDurabilityError(StoreError):
    """The association was replaced, but its durability is not confirmed."""


class Associations:
    def __init__(self, env: dict[str, str]) -> None:
        home = Path(env.get("HOME") or Path.home())
        self.path = (
            Path(env.get("XDG_STATE_HOME") or home / ".local" / "state")
            / "jumper"
            / "associations.json"
        )

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise StoreError(f"cannot read associations: {exc}") from exc
        if not isinstance(data, dict) or any(
            not isinstance(key, str)
            or not isinstance(value, dict)
            or any(
                not isinstance(path, str) or not isinstance(wid, str)
                for path, wid in value.items()
            )
            for key, value in data.items()
        ):
            raise StoreError("invalid associations file")
        return data

    def get(self, endpoint: str, project: str) -> str | None:
        return self._load().get(endpoint, {}).get(project)

    def save(self, endpoint: str, project: str, workspace_id: str) -> None:
        data = self._load()
        data.setdefault(endpoint, {})[project] = workspace_id
        tmp = None
        replaced = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.path.parent, prefix=".associations-", delete=False
            ) as file:
                tmp = file.name
                Path(tmp).chmod(0o600)
                json.dump(data, file, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            Path(tmp).replace(self.path)
            tmp = None
            replaced = True
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            if replaced:
                raise StoreDurabilityError(
                    f"association updated, but durability not confirmed: {exc}"
                ) from exc
            raise StoreError(f"cannot save association: {exc}") from exc
        finally:
            if tmp is not None:
                with suppress(OSError):
                    Path(tmp).unlink(missing_ok=True)
