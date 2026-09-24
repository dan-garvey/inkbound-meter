"""Extract numerical healing recipes from locally inspected build-24243 assets.

Input is the ignored assets.json produced during local game inspection. Only
numbers, identifiers and supported formula operations enter the output catalog.
An observed heal selects its action. Repeated/branched heal nodes are supported
only when every node has the same amount, modifiers, owner, and target selectors.
"""

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--assets", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
assets = json.loads(args.assets.read_text())


class Unsupported(ValueError):
    pass


def resolve(ref):
    if "_type" in ref:
        return assets.get(f"{ref['_type'][0]}-{ref['_type'][1]}", {})
    return assets.get(f"{ref.get('guid')}-{ref.get('fileID')}", {})


def recipe(action):
    nodes = [resolve(ref) for ref in resolve(action["actionGraph"]).get("nodes", [])]
    heal_nodes = [
        n
        for n in nodes
        if n.get("m_Name")
        in ("Heal Flat Amount Action", "Heal Action", "Heal Percent Missing Health Action")
    ]
    if not heal_nodes:
        raise Unsupported()

    def port(node, name):
        p = next((p for p in node["ports"]["valuesList"] if p["_fieldName"] == name), None)
        if p is None or not p["connections"]:
            value = node.get(name, {}).get("value", {}).get("serializedValue")
            if value is None:
                # Serialized port defaults for these nodes are zero/false.
                value = False if name == "ignoreHealModifiers" else 0
            if not isinstance(value, (int, bool)):
                raise Unsupported()
            return value
        if len(p["connections"]) != 1:
            raise Unsupported()
        connection = p["connections"][0]
        n = resolve(connection["node"])
        field = connection["fieldName"]
        kind = n.get("m_Name")
        if kind == "Action Node Action Params":
            if field == "paramStatusEffectId":
                return resolve(action["paramStatusEffectData"]).get("m_Name")
            value = action.get(field)
            return resolve(value).get("id") if isinstance(value, dict) else value
        if kind == "Action Node Status Effect Params":
            return {
                "statusSourceEntityHandle": "status_source",
                "statusAttachedEntityHandle": "status_target",
                "statusEffectId": "event_effect",
            }.get(field, "unsupported")
        if kind == "Action Node Targeting":
            return {"ownerEntityHandle": "caster", "targetEntityHandle": "target"}.get(
                field, "unsupported"
            )
        if kind in ("Integer Constant", "Bool Constant"):
            return n["value"]
        if kind == "Status Effect Data Constant":
            return resolve(n["value"])["m_Name"]
        if kind == "Stat Data Constant":
            return resolve(n["value"])["id"]
        if kind == "Get Unit Status Count":
            side = port(n, "unitEntityHandle")
            effect = port(n, "statusEffectId")
            if side not in ("caster", "target", "status_source", "status_target"):
                raise Unsupported()
            if port(n, "statusEffectInstanceHandle") != 0 or not isinstance(effect, str):
                raise Unsupported()
            return {"op": "stacks", "side": side, "effect": effect}
        if kind == "Get Unit Stat":
            side, stat = port(n, "unitEntityHandle"), port(n, "statData")
            if side not in ("caster", "target", "status_source", "status_target") or not isinstance(
                stat, str
            ):
                raise Unsupported()
            return {"op": "stat", "side": side, "id": stat}
        if kind in ("Addition", "Multiplication", "Int Subtraction", "Division"):
            return {
                "op": {
                    "Addition": "add",
                    "Multiplication": "mul",
                    "Int Subtraction": "sub",
                    "Division": "div",
                }[kind],
                "args": [port(n, "in1"), port(n, "in2")],
            }
        raise Unsupported()

    recipes = []
    for heal in heal_nodes:
        kind = heal["m_Name"]
        field = {
            "Heal Flat Amount Action": "amount",
            "Heal Action": "percent1",
            "Heal Percent Missing Health Action": "percent",
        }[kind]
        amount = port(heal, field)
        if not isinstance(amount, (int, dict)):
            raise Unsupported()
        owner, target = port(heal, "ownerEntityHandle"), port(heal, "targetEntityHandle")
        if any(
            v not in ("caster", "target", "status_source", "status_target") for v in (owner, target)
        ):
            raise Unsupported()
        recipes.append(
            {
                "owner": owner,
                "target": target,
                "mode": {
                    "Heal Flat Amount Action": "flat",
                    "Heal Action": "max_hp",
                    "Heal Percent Missing Health Action": "missing_hp",
                }[kind],
                "amount": amount,
                "ignore_modifiers": kind == "Heal Percent Missing Health Action"
                or bool(port(heal, "ignoreHealModifiers")),
            }
        )
    if any(r != recipes[0] for r in recipes[1:]):
        raise Unsupported()
    return recipes[0]


result = {"build": 24243, "actions": {}, "abilities": {}}
for action in assets.values():
    if not action.get("actionGraph"):
        continue
    try:
        result["actions"][action["m_Name"]] = recipe(action)
    except (Unsupported, KeyError, TypeError):
        pass

# Only direct action wrappers provide this ownership link. A recent unrelated
# cast, status-triggered action, or chained ability is never treated as its owner.
manifest = json.loads(assets["SharedSoManifest.json"]["_text"])
for entry in manifest["entries"]:
    if not entry["className"].endswith(".AbilityData"):
        continue
    asset_id = entry["assetID"]
    ability = assets[f"{asset_id['_guid']}-{asset_id['_fileID']}"]
    actions = {}
    for wrapper in ability.get("actionDataWrappers", {}).get("entries", []):
        name = resolve(wrapper.get("actionData", {})).get("m_Name")
        spec = result["actions"].get(name)
        if spec and spec["owner"] == "caster" and spec["target"] == "target":
            actions[name] = actions.get(name, 0) + 1
    if actions:
        result["abilities"][ability["m_Name"]] = actions
used = {"pROS9yE0", "1SDlVYuI"}


def collect(value):
    if isinstance(value, dict):
        if value.get("op") == "stat":
            used.add(value["id"])
        for child in value.values():
            collect(child)
    elif isinstance(value, list):
        for child in value:
            collect(child)


collect(result["actions"])
result["defaults"] = {}
for asset in assets.values():
    stats = {
        resolve(s["entryStatData"]).get("id"): s["entryValue"]
        for s in asset.get("statEntries", {}).get("statEntries", [])
        if not s.get("sourceHasStatusEffect", {}).get("fileID")
    }
    stats = {k: v for k, v in stats.items() if k in used}
    if stats:
        key = f"C{asset['classType']:02}" if "classType" in asset else asset.get("m_Name")
        result["defaults"][key] = stats
args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
print(f"Extracted {len(result['actions'])} healing recipes")
