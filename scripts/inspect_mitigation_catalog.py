"""Extract inspected incoming damage and defense recipes for client build 24243.

Requires the ignored assets.json from local inspection and the matching installed
game. Only numerical recipes and a handful of display names enter the catalog.
UnityPy is used for the local English item names, never by the overlay at runtime.
"""

import argparse
import hashlib
import json
from pathlib import Path

import UnityPy

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--assets", type=Path, required=True)
parser.add_argument("--game-data", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
approved = json.loads(
    (Path(__file__).resolve().parents[1] / "src/inkbound_meter/damage_catalog.json").read_text()
)
for path, digest in approved["evidence"].items():
    if hashlib.sha256((args.game_data / path).read_bytes()).hexdigest() != digest:
        parser.error("Game files differ from the inspected build; review before extracting.")
assets = json.loads(args.assets.read_text())
byname = {a["m_Name"]: a for a in assets.values() if "m_Name" in a}


def resolve(ref):
    if "_type" in ref:
        return assets.get(f"{ref['_type'][0]}-{ref['_type'][1]}", {})
    return assets.get(f"{ref.get('guid')}-{ref.get('fileID')}", {})


def connection(node, field):
    ports = [p for p in node["ports"]["valuesList"] if p["_fieldName"] == field]
    links = ports[0]["connections"] if ports else []
    assert len(links) <= 1
    assert field not in node, "Uninspected serialized default"
    return (resolve(links[0]["node"])["m_Name"], links[0]["fieldName"]) if links else None


def damage_node(graph, kind):
    nodes = [resolve(r) for r in byname[graph]["nodes"]]
    damage = [n for n in nodes if "Damage" in n["m_Name"]]
    assert len(damage) == 1 and damage[0]["m_Name"] == kind
    return damage[0]


# Inspect the actual connected ports, not similarly named action parameters.
# Both roll endpoints use paramPercent1, so these attacks have no random range.
node = damage_node("DamageDirectScalingActionGraph", "Damage Action")
for field, expected in {
    "enter": ("Action Node Entry", "exit"),
    "percent1": ("Action Node Action Params", "paramPercent1"),
    "percent2": ("Action Node Action Params", "paramPercent1"),
    "statData": ("Action Node Action Params", "paramStatData"),
    "ownerEntityHandle": ("Action Node Targeting", "ownerEntityHandle"),
    "targetEntityHandle": ("Action Node Targeting", "targetEntityHandle"),
    "actionTags": ("Action Node Action Params", "paramActionTags"),
    "ignoreShields": None,
}.items():
    assert connection(node, field) == expected
node = damage_node(
    "StatusEffect_DamageFlatActionAttachedEntityGraph", "Indirect Damage Flat Amount Action"
)
for field, expected in {
    "enter": ("Action Node Entry", "exit"),
    "amount": ("Action Node Action Params", "paramInt"),
    "ownerEntityHandle": ("Action Node Status Effect Params", "statusAttachedEntityHandle"),
    "targetEntityHandle": ("Action Node Status Effect Params", "statusAttachedEntityHandle"),
    "actionTags": ("Action Node Action Params", "paramActionTags"),
    "ignoreShields": None,
    "numStacks": None,
    "dealLiteralFlatDamage": None,
}.items():
    assert connection(node, field) == expected

result = {"build": approved["build"], "evidence": approved["evidence"], "actions": {}}
for name, action in byname.items():
    graph = resolve(action.get("actionGraph", {})).get("m_Name")
    if graph == "DamageDirectScalingActionGraph":
        amount = {
            "op": "scaled_stat",
            "id": resolve(action["paramStatData"])["id"],
            "percent": action["paramPercent1"],
        }
        direct = True
    elif graph == "StatusEffect_DamageFlatActionAttachedEntityGraph":
        amount, direct = action["paramInt"], False
    else:
        continue
    result["actions"][name] = {
        "amount": amount,
        "direct": direct,
        "crit": 0,
        "stacks": 0,
        "tags": [resolve(t)["id"] for t in action["actionTags"]],
        "self": not direct,
    }

# This graph's two branches both explicitly bypass all damage modifiers.
# Its book-dependent raw amount is unnecessary to prove zero pre-shield prevention.
graph = byname["StatusEffect_DamageIndirectContextBookScalingCappedLiteralFlatActionGraph"]
for n in [resolve(r) for r in graph["nodes"] if "Damage" in resolve(r)["m_Name"]]:
    assert n["m_Name"] == "Indirect Damage Flat Amount Action"
    link = next(p for p in n["ports"]["valuesList"] if p["_fieldName"] == "dealLiteralFlatDamage")
    assert len(link["connections"]) == 1
    constant = resolve(link["connections"][0]["node"])
    assert constant["m_Name"] == "Bool Constant" and constant["value"] is True
result["literal_actions"] = [
    name
    for name, a in byname.items()
    if resolve(a.get("actionGraph", {})).get("m_Name") == graph["m_Name"]
]

# Read only the English equipment-name table; ship names for identified effects.
bundle_root = args.game_data / "StreamingAssets/aa/StandaloneWindows64"
tables = []
for bundle in (
    "localization-assets-shared_assets_all.bundle",
    "localization-string-tables-en-us_assets_all.bundle",
):
    env = UnityPy.load(str(bundle_root / bundle))
    tables.append([obj.read_typetree() for obj in env.objects if obj.type.name == "MonoBehaviour"])
keys = next(t for t in tables[0] if t["m_Name"] == "SoEquipmentDataStringTable Shared Data")
values = next(t for t in tables[1] if t["m_Name"] == "SoEquipmentDataStringTable_en-US")
key_ids = {v["m_Key"]: v["m_Id"] for v in keys["m_Entries"]}
strings = {v["m_Id"]: v["m_Localized"] for v in values["m_TableData"]}


def item_label(name):
    return strings[key_ids[byname[name]["equipmentName"]]]


effect_items = {
    "VestigeC04_Uncommon_ThreadShield_Shielding_StatusEffect": (
        "VestigeC04_Uncommon_ThreadShield_Equip"
    ),
    "Vestige_DamageResistPerSpiked_Hidden_StatusEffect": "Vestige_DamageResistPerSpiked_Equip",
    "Vestige_Legendary_DoubleBlurNoShield_StatusEffect": (
        "Vestige_Legendary_DoubleBlurNoShield_Equip"
    ),
}
labels = {
    "Blur_StatusEffect": "Blur",
    "Resistant_StatusEffect": "Resistant",
    **{effect: "Vestige: " + item_label(item) for effect, item in effect_items.items()},
    **{
        f"{name}_Set_{tier}{suffix}_StatusEffect": f"{name} set ({tier})"
        for name, tiers, suffix in [
            ("Thaumaturge", [2, 4], "_StatBoost"),
            ("Warden", [2, 5, 8], ""),
        ]
        for tier in tiers
    },
}
result["defenses"] = {}
for effect, label in labels.items():
    status = byname[effect]
    assert status["statAdjustmentMode"] in (0, 1)
    entries = []
    for v in status["statAdjustments"]["statEntries"]:
        stat = resolve(v["entryStatData"])["id"]
        if stat not in ("3TCx0XPg", "pq6hlsOe", "TWM45OpA", "0QnywPkl"):
            continue
        condition = resolve(v["sourceHasStatusEffect"]).get("m_Name")
        assert condition is None or condition in labels
        entries.append(
            {
                "stat": stat,
                "value": v["entryValue"],
                "source_has": condition,
                "label": labels.get(condition, label),
            }
        )
    if entries:
        result["defenses"][effect] = {
            "per_stack": status["statAdjustmentMode"] == 0,
            "entries": entries,
        }

# These equipped items have unique, persistent status markers. Static stats from
# markerless items cannot be attributed to an item by looking at their value.
for name in ("VestigeAll_Uncommon_Teleport_Equip", "VestigeAll_Rare_SpikedPerDodge_Equip"):
    item = byname[name]
    statuses = item["statusEffectEntries"]["statusEffectEntries"]
    assert len(statuses) == 1 and statuses[0]["stackCount"] == 1
    effect = resolve(statuses[0]["statusEffect"])
    assert effect["createNewInstanceOnAdd"] and effect["isRunPersistent"]
    result["defenses"][effect["m_Name"]] = {
        "per_stack": False,
        "entries": [
            {
                "stat": "3TCx0XPg",
                "value": s["entryValue"],
                "source_has": None,
                "label": "Vestige: " + item_label(name),
            }
            for s in item["statEntries"]["statEntries"]
            if resolve(s["entryStatData"])["id"] == "3TCx0XPg"
        ],
    }

result["direct_attack_raw"] = {
    str(v["playerCount"]): v["difficultyBalanceMultipliers"]["enemyDirectAttackMultiplier"][
        "fixedRaw"
    ]
    for v in byname["PlayerCountBalanceData_Base"]["balanceAdjustmentData"]
}
result["easy_mode_status"] = "ChallengeRunData_EasyMode_StatusEffect"
result["easy_mode_raw"] = 3277
# All other inspected rank-balance definitions leave this factor at one.
rank_values = {
    a["enemyDirectAttackMultiplier"]["fixedRaw"]
    for a in byname.values()
    if "enemyDirectAttackMultiplier" in a
}
assert rank_values <= {4096, result["easy_mode_raw"]}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
print(f"{len(result['actions'])} incoming recipes, {len(result['defenses'])} named defenses")
