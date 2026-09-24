"""Parse one complete log record. Only EventSystem broadcasts produce damage.

The game also logs simulations, client HP updates and subscribers handling the
same event. None of those are additional damage events. Never deduplicate by
timestamp or text: consecutive legitimate procs can have identical log lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    kind: str
    timestamp: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class ParseError(ValueError):
    """A recognized statistics record has an unsupported or incomplete shape."""


_ACTOR = re.compile(r"\bI (.+?) \(EntityHandle:(-?\d+)\) is playing ability ")
_BROADCAST = "[EventSystem] broadcasting "
PARSER_VERSION = 10
_DAMAGE_HEALTH = re.compile(
    r"Client unit state damaging unit \(EntityHandle:(-?\d+)\)\. "
    r"Attacker-\(EntityHandle:(-?\d+)\) : Damage Amount-(\d+) : .* New hp: (\d+)\s*$"
)


def _field(text: str, name: str) -> str:
    match = re.search(r"(?:^|-)" + re.escape(name) + r":([^\s-][^\s]*?)(?=-[A-Za-z]|$)", text)
    if not match:
        raise ParseError(f"Missing or invalid {name}")
    return match[1]


def _number(text: str, name: str) -> int:
    match = re.search(r"(?:^|-)" + re.escape(name) + r":(-?\d+)(?=-|$|\s)", text)
    if not match:
        raise ParseError(f"Missing or invalid {name}")
    return int(match[1])


def _handle(text: str, name: str) -> int:
    match = re.search(re.escape(name) + r":\(EntityHandle:(-?\d+)\)", text)
    if not match:
        raise ParseError(f"Missing or invalid {name}")
    return int(match[1])


def _boolean(text: str, name: str) -> bool:
    value = _field(text, name)
    if value not in ("True", "False"):
        raise ParseError(f"Invalid boolean {name}")
    return value == "True"


def _asset(text: str, name: str) -> str | None:
    match = re.search(re.escape(name) + r":(?:" + re.escape(name) + r"-)?([^\s(]+)", text)
    return match[1] if match else None


def _health(text: str) -> dict:
    match = re.search(r"\bhp:(\d+)-energyShield:(\d+)-maxHp:(\d+)", text)
    return (
        dict(zip(("hp", "shield", "max_hp"), map(int, match.groups()), strict=True))
        if match
        else {}
    )


def _position(text: str, name: str) -> list[int] | None:
    match = re.search(re.escape(name) + r":\((-?\d+(?:\.\d+)?), (-?\d+(?:\.\d+)?)\)", text)
    # Positions are printed to six decimals. Recover the exact 1/4096 grid;
    # keeping raw integers also reproduces the game's distance rounding.
    return [round(float(v) * 4096) for v in match.groups()] if match else None


def _fixed_raw(text: str, name: str) -> int | None:
    """Parse a printed fixed-point scalar back onto Inkbound's 1/4096 grid."""
    match = re.search(re.escape(name) + r":(-?\d+(?:\.\d+)?)\b", text)
    return round(float(match[1]) * 4096) if match else None


def parse_line(line: str) -> Event | None:
    """Return a normalized event, None for irrelevant lines, or raise ParseError."""
    timestamp = line.split(" I ", 1)[0].strip()
    build = re.search(r"\bClient build number: (\d+)\s*$", line)
    if build:
        return Event("game_build", timestamp, {"build": int(build[1])})
    if "is playing ability" in line:
        match = _ACTOR.search(line)
        if match:
            return Event("player", timestamp, {"id": int(match[2]), "name": match[1]})
    if "Setting unit class for animation-" in line:
        return Event(
            "class",
            timestamp,
            {"id": _handle(line, "UnitEntityHandle"), "class_id": _field(line, "classType")},
        )
    if match := _DAMAGE_HEALTH.search(line):
        # Context only: the following authoritative broadcast counts the hit.
        target, source, amount, hp = map(int, match.groups())
        return Event(
            "damage_health",
            timestamp,
            {"target": target, "source": source, "amount": amount, "hp": hp},
        )
    if _BROADCAST not in line:
        return None
    body = line.split(_BROADCAST, 1)[1].strip()
    name = body.split("-", 1)[0]
    if name in ("EventPartyRunCreate", "EventOnNewRunVoteFinished"):
        return Event("run_create", timestamp)
    if name == "EventOnRunConnected":
        return Event(
            "connection",
            timestamp,
            {
                "connected": _boolean(body, "IsOnConnected"),
                "seed": _number(body, "RunSeed"),
                "support_version": 3,
            },
        )
    if name == "EventOnRunEnd":
        return Event("run_end", timestamp, {"won": _boolean(body, "DidWin")})
    if name == "EventOnCombatStarted":
        return Event("combat_start", timestamp, {"zone": _handle(body, "CombatZoneHandle")})
    if name in (
        "EventOnUnitEnterCombat",
        "EventOnUnitEnterCombatWorldSync",
        "EventOnUnitExitCombat",
    ):
        return Event(
            "combat_zone",
            timestamp,
            {
                "id": _handle(body, "UnitEntityHandle"),
                "zone": -1
                if name == "EventOnUnitExitCombat"
                else _handle(body, "CombatZoneEntityHandle"),
            },
        )
    if name == "EventOnCombatEndSequenceEnded":
        return Event("combat_end", timestamp)
    if name == "EventOnItemDrafted":
        item = _asset(body, "ItemData")
        if not item:
            raise ParseError("Missing drafted item")
        return Event(
            "loadout",
            timestamp,
            {
                "id": _handle(body, "PlayerUnitHandle"),
                "kind": "gear",
                "item": item.removeprefix("EquipmentData-"),
            },
        )
    if name == "EventOnAbilityDrafted":
        ability = _asset(body, "AbilityData")
        if not ability:
            raise ParseError("Missing drafted ability")
        return Event(
            "loadout",
            timestamp,
            {"id": _handle(body, "PlayerUnitHandle"), "kind": "ability", "item": ability},
        )
    if name == "EventOnAbilityUpgraded":
        upgrade = _asset(body, "AbilityUpgradeData")
        ability = _asset(body, "AbilityData")
        if not upgrade:
            raise ParseError("Missing ability upgrade")
        return Event(
            "loadout",
            timestamp,
            {
                "id": _handle(body, "SourceEntityHandle"),
                "kind": "aspect",
                "item": upgrade,
                "ability": ability,
            },
        )
    if name in ("EventOnUnitMoved", "EventOnUnitTeleported", "EventOnUnitPlayedAbility"):
        field = {
            "EventOnUnitMoved": "EndPosition",
            "EventOnUnitTeleported": "MoveToPosition",
            "EventOnUnitPlayedAbility": "FromWorldPosition",
        }[name]
        position = _position(body, field)
        if position is None:
            return None
        entity = re.search(r"(?:UnitEntityHandle|UnitHandle)[:-]\(EntityHandle:(-?\d+)\)", body)
        if not entity:
            raise ParseError("Missing position entity")
        data = {"id": int(entity[1]), "position": position}
        if name == "EventOnUnitPlayedAbility":
            data["ability"] = _asset(body, "AbilityData")
        return Event("position", timestamp, data)
    if name == "EventOnUnitResourceModified":
        return Event(
            "resource",
            timestamp,
            {
                "id": _handle(body, "UnitEntityHandle"),
                "resource": _field(body, "ResourceType"),
                "previous": _number(body, "PreviousValue"),
                "value": _number(body, "NewValue"),
            },
        )
    if name == "EventOnTurnPhaseAdvanced":
        return Event(
            "turn_phase",
            timestamp,
            {
                "zone": _handle(body, "CombatZoneEntityHandle"),
                "phase": _field(body, "NewTurnPhase"),
            },
        )
    if name == "EventOnEndTurnRequestSucceeded":
        return Event("end_turn", timestamp, {"id": _handle(body, "UnitEntityHandle")})
    if name == "EventOnUnitRemoved":
        return Event("unit_removed", timestamp, {"id": _handle(body, "EntityHandle")})
    if name == "EventOnPlayerRecordUpdated":
        match = re.search(
            r"WorldStateChangePlayerRecordUpdated \(EntityHandle:(-?\d+)\) \[([^]]*)\]", body
        )
        if match and match[2] in ("EDclDmMI", "KuFLDDDD", "hkIlI6PO", "SYuK7HPf", "sI7wIL3j"):
            return Event("support_record", timestamp, {"id": int(match[1]), "record": match[2]})
        return None
    if name == "EventOnUnitHpEnergyShieldStatChanged":
        health = _health(body)
        if not health:
            raise ParseError("Missing health and shield state")
        return Event("health", timestamp, {"id": _handle(body, "UnitEntityHandle"), **health})
    if name in ("EventOnUnitHealed", "EventOnUnitGainedEnergyShield"):
        heal = name == "EventOnUnitHealed"
        if heal:
            flow = body.rsplit("-", 1)[-1]
            if flow in ("Prediction", "Misprediction"):
                return None
            if flow not in ("RealNotPredicted", "RealAfterPrediction", "RealAfterMisprediction"):
                raise ParseError("Unsupported healing prediction state")
        amount = _number(body, "HealAmount" if heal else "Value")
        health = _health(body)
        if amount < 0 or not health:
            raise ParseError("Invalid healing or shield amount/state")
        return Event(
            "heal" if heal else "shield",
            timestamp,
            {
                "target": _handle(body, "TargetUnitHandle" if heal else "UnitHandle"),
                "target_team": _field(body, "TargetUnitTeam" if heal else "UnitTeam"),
                "amount": amount,
                "in_combat": _boolean(body, "IsInActiveCombat"),
                "action": _asset(body, "ActionData"),
                "ability": _asset(body, "AbilityData"),
                "effect": _asset(body, "StatusEffectData"),
                **health,
            },
        )
    if name == "EventOnUnitStatModified":
        return Event(
            "stat",
            timestamp,
            {
                "id": _handle(body, "UnitEntityHandle"),
                "stat": _field(body, "StatDataGuid"),
                "value": _number(body, "NewValue"),
            },
        )
    if name == "EventOnUnitStatusEffectAction":
        effect = _field(body, "StatusEffect")
        proc = _field(body, "ProcType")
        # Only the two inspected Burn proc shapes are needed for reconstruction.
        effect = effect.rsplit("-", 1)[0]
        if (effect, proc) not in (
            ("Burn_StatusEffect", "OnTurnEnd"),
            ("Burn_StatusEffect", "OnBeingHit"),
            ("VestigeAll_Epic_BurnDamageOnBurn_StatusEffect", "OnEnemyDied"),
        ):
            return None
        return Event(
            "status_proc",
            timestamp,
            {
                "id": _handle(body, "UnitEntityHandle"),
                "target": _handle(body, "ContextTargetUnitEntityHandle"),
                "effect": effect,
                "proc": proc,
            },
        )
    if name in ("EventOnUnitStatusEffectStacksAdded", "EventOnUnitStatusEffectStacksRemoved"):
        instance = re.search(r"StatusEffectInstanceHandle:\(Handle:(-?\d+)\)", body)
        effect = _asset(body, "StatusEffectData")
        if not instance or not effect:
            raise ParseError("Missing status effect identity")
        return Event(
            "status",
            timestamp,
            {
                "target": _handle(body, "TargetUnitEntityHandle"),
                "source": _handle(body, "CasterUnitEntityHandle"),
                "instance": int(instance[1]),
                "effect": effect,
                "stacks": _number(body, "NewStacksValue"),
                "added" if name.endswith("Added") else "removed": _number(
                    body, "StacksAdded" if name.endswith("Added") else "StacksRemoved"
                ),
            },
        )
    unit_context = {}
    if name == "EventOnUnitAdded":
        unit_context = {"unit_data": _asset(body, "UnitData"), **_health(body)}
        if "-CombatZoneHandle:" in body:
            unit_context["zone"] = _handle(body, "CombatZoneHandle")
        if (position := _position(body, "WorldPosition")) is not None:
            unit_context["position"] = position
        if (hitbox_radius := _fixed_raw(body, "HitboxRadius")) is not None:
            unit_context["hitbox_radius"] = hitbox_radius
        if "-IsResumingRun:" in body:
            unit_context["resuming"] = _boolean(body, "IsResumingRun")
    if name == "EventOnUnitAdded" and "UnitData:UnitData-BasePlayerData " in body:
        return Event(
            "player",
            timestamp,
            {
                "id": _handle(body, "UnitHandle"),
                "class_id": _field(body, "CharacterClassType"),
                **unit_context,
            },
        )
    if name == "EventOnUnitAdded":
        return Event(
            "unit",
            timestamp,
            {"id": _handle(body, "UnitHandle"), "team": _field(body, "Team"), **unit_context},
        )
    if name != "EventOnUnitDamaged":
        return None
    amount = _number(body, "DamageAmount")
    if amount < 0:
        raise ParseError("Negative DamageAmount")
    return Event(
        "damage",
        timestamp,
        {
            "source": _handle(body, "SourceEntityHandle"),
            "target": _handle(body, "TargetUnitHandle"),
            "amount": amount,
            "target_team": _field(body, "TargetUnitTeam"),
            "in_combat": _boolean(body, "IsInActiveCombat"),
            "critical": _boolean(body, "IsCriticalHit"),
            "dodged": _boolean(body, "WasDodged"),
            "action": _asset(body, "ActionData"),
            "ability": _asset(body, "AbilityData"),
            "effect": _asset(body, "StatusEffectData"),
            "loot": _asset(body, "LootableData"),
        },
    )


def parse_safe(line: str) -> Event | None:
    """Preserve a diagnostic without storing raw log lines or account data."""
    try:
        return parse_line(line.rstrip("\r\n"))
    except ParseError as exc:
        return Event("parse_error", data={"message": str(exc)})
