"""Current party state is separate from historical combat totals."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def party_catalog():
    return json.loads(files("inkbound_meter").joinpath("party_catalog.json").read_text())


class PartyState:
    """Log fallback: values remain unknown until actually observed.

    Cooldown broadcasts omit their values; never extrapolate a countdown from
    casts or turn changes. Memory supplies complete live binding state instead.
    """

    def __init__(self):
        self.players = {}
        self.resources = {}
        self.phase = ""
        self.connected = True
        self.zone = None

    def apply(self, event):
        kind, data = event.kind, event.data
        if kind in ("source_started", "connection"):
            self.__init__()
            self.connected = data.get("connected", True)
        elif kind in ("player", "class") and data["id"] >= 0:
            if kind == "class" and data["id"] not in self.players:
                return
            player = self.players.setdefault(data["id"], {"id": data["id"], "ended_turn": None})
            for key in ("name", "class_id"):
                if data.get(key):
                    player[key] = data[key]
        elif kind == "resource" and data["resource"] == "ManaPoints":
            self.resources.setdefault(data["id"], {})["will"] = data["value"]
        elif kind == "stat" and data["stat"] == "bFwnYuVx":
            self.resources.setdefault(data["id"], {})["max_will"] = max(0, data["value"])
        elif kind == "unit_removed":
            self.players.pop(data["id"], None)
            self.resources.pop(data["id"], None)
        elif kind == "combat_start":
            if self.zone != data["zone"]:
                self.zone = data["zone"]
                self.phase = ""
                for player in self.players.values():
                    player["ended_turn"] = False
        elif kind == "turn_phase" and (self.zone is None or self.zone == data["zone"]):
            self.phase = data["phase"]
            if self.phase == "StartPlayerTurn":
                for player in self.players.values():
                    player["ended_turn"] = False
        elif kind == "end_turn" and data["id"] in self.players:
            self.players[data["id"]]["ended_turn"] = True
        elif kind in ("combat_end", "run_end"):
            self.phase = ""
            self.zone = None
            for player in self.players.values():
                player["ended_turn"] = None

    def snapshot(self, health):
        from .model import CLASS_NAMES

        players = []
        for entity, player in sorted(self.players.items()) if self.connected else []:
            players.append(
                {
                    "id": entity,
                    "name": player.get("name") or f"Player {entity}",
                    "class": CLASS_NAMES.get(player.get("class_id"), player.get("class_id", "")),
                    "hp": health.get(entity, {}).get("hp"),
                    "max_hp": health.get(entity, {}).get("max_hp"),
                    "shield": health.get(entity, {}).get("shield"),
                    "will": self.resources.get(entity, {}).get("will"),
                    "max_will": self.resources.get(entity, {}).get("max_will"),
                    "ended_turn": player["ended_turn"],
                    "abilities": None,
                }
            )
        return {
            "players": players,
            "source": "log",
            "phase": self.phase,
            "turn": None,
            "status": "Log values; live cooldowns unavailable",
        }
