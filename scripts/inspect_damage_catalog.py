"""Read the installed game's numerical definitions into a reviewable catalog candidate.

Read-only, standard-library tool for the inspected client build 24243. Game
assemblies, full asset data and decompiled source are never copied into the repo.
The approved assembly hash gates the interpreter; new code needs a fresh review.
The binary layout was inspected locally, including FlatBuffer table offsets and
SoBinary primitive/object/reference tokens. See docs/damage-components.md.
"""

import argparse
import hashlib
import json
import re
import struct
from pathlib import Path


class Binary:
    def __init__(self, b):
        self.b = b
        self.p = 8

    def read(self, fmt):
        v = struct.unpack_from("<" + fmt, self.b, self.p)[0]
        self.p += struct.calcsize(fmt)
        return v

    def string(self):
        n = 0
        shift = 0
        while True:
            b = self.read("B")
            n |= (b & 127) << shift
            shift += 7
            if b < 128:
                break
        v = self.b[self.p : self.p + n].decode()
        self.p += n
        return v

    def value(self):
        t = self.read("B")
        if t == 9:
            guid = self.string()
            file = self.read("q")
            v = self.value()
            return {"_type": [guid, file], "value": v}
        if t == 1:
            ty = self.read("B")
            if ty == 18:
                return self.string()
            return self.read(
                {
                    3: "?",
                    5: "b",
                    6: "B",
                    7: "h",
                    8: "H",
                    9: "i",
                    10: "I",
                    11: "q",
                    12: "Q",
                    13: "f",
                    14: "d",
                }[ty]
            )
        if t == 2:
            return self.value()
        if t == 3:
            n = self.read("i")
            d = {}
            for _ in range(n):
                k = self.string()
                self.read("H")
                d[k] = self.value()
            assert self.read("B") == 6
            return d
        if t in (4, 5):
            v = [self.value() for _ in range(self.read("i"))]
            assert self.read("B") == 6
            return v
        if t == 7:
            return None
        if t == 8:
            return self.string()
        raise ValueError((t, self.p))


def read_assets(game_data):
    b = (
        game_data / "StreamingAssets/SharedScriptableObjects/SharedScriptableObjects.fb"
    ).read_bytes()

    def u(p):
        return struct.unpack_from("<I", b, p)[0]

    def field(p, n):
        v = p - struct.unpack_from("<i", b, p)[0]
        i = 4 + 2 * n
        z = struct.unpack_from("<H", b, v + i)[0] if i < struct.unpack_from("<H", b, v)[0] else 0
        return p + z if z else 0

    def deref(p):
        return p + u(p)

    def blob(p):
        q = deref(p)
        return b[q + 4 : q + 4 + u(q)]

    p = deref(field(u(0), 0))
    for i in range(u(p)):
        a = deref(p + 4 + i * 4)
        name = blob(field(a, 0)).decode()
        ty = b[field(a, 1)]
        c = deref(field(a, 2))
        content = blob(field(c, 0))
        yield name, ty, content


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--game-data", type=Path, required=True, help="Installed Inkbound_Data directory"
)
parser.add_argument("--output", type=Path, default=Path(".local/catalog-candidate.json"))
args = parser.parse_args()
approved = json.loads(
    (Path(__file__).resolve().parents[1] / "src/inkbound_meter/damage_catalog.json").read_text()
)
assembly = args.game_data / "Managed/Assembly-CSharp.dll"
if (
    hashlib.sha256(assembly.read_bytes()).hexdigest()
    != approved["evidence"]["Managed/Assembly-CSharp.dll"]
):
    parser.error(
        "Assembly differs from the inspected build; "
        "review the damage code before updating formulas."
    )
assets = {}
for name, kind, raw in read_assets(args.game_data):
    if kind == 2:
        if struct.unpack_from("<i", raw)[0] != 0:
            raise ValueError("Unsupported asset binary version")
        assets[name] = Binary(raw).value()
    else:
        assets[name] = {"_text": raw.decode()}
manifest = json.loads(assets["SharedSoManifest.json"]["_text"])
entries = {f"{v['assetID']['_guid']}-{v['assetID']['_fileID']}": v for v in manifest["entries"]}
byname = {v.get("m_Name"): v for v in assets.values() if "m_Name" in v}


def resolve(ref):
    if "_type" in ref:
        return assets.get(f"{ref['_type'][0]}-{ref['_type'][1]}", {})
    return assets.get(f"{ref.get('guid')}-{ref.get('fileID')}", {})


def clean(n):
    labels = {
        "Core_AbilityPower": "Omni damage",
        "Core_MagicPower": "Magic damage",
        "Core_PhysicalPower": "Physical damage",
        "DamageMultiplier": "General damage",
        "DoT_Frostbite_Damage_Stat": "Frostbite damage",
        "DoT_Burn_Damage_Stat": "Burn damage",
        "DoT_Poison_Damage_Stat": "Poison damage",
        "DoT_Bleed_Damage_Stat": "Bleed damage",
        "Smite_Damage_Stat": "Smite damage",
        "FullCharge_StatusEffect": "Full charge",
        "AuraPower_Stat": "Aura power",
        "AuraCount": "Aura count",
        "DoT_Frostbite_DamageTo_Stat": "Damage to Frostbitten enemies",
        "DoT_Burn_DamageTo_Stat": "Damage to Burning enemies",
        "DoT_Poison_DamageTo_Stat": "Damage to Poisoned enemies",
        "DoT_Bleed_DamageTo_Stat": "Damage to Bleeding enemies",
        "Hex_DamageTo_Stat": "Damage to Hexed enemies",
        "Shattered_DamageTo_Stat": "Damage to Shattered enemies",
        "Dazed_DamageTo_Stat": "Damage to Dazed enemies",
        "Invigorate_DamageMult_Stat": "Invigorate",
        "Core_CriticalDamageMultiplier": "Critical damage",
        "Core_DamageReduction": "Damage reduction",
        "Core_UncappedDamageReduction": "Uncapped damage reduction",
        "C01_ClassPassive_Heat_DamageMult_Stat": "Heat",
        "C09_Empyrean_Velocity_Stat": "Velocity",
        "Resist_Magical": "Magic resistance",
        "Resist_Physical": "Physical resistance",
        "Weakness_Magical": "Magic weakness",
        "Weakness_Physical": "Physical weakness",
        "Weakness_Smite": "Smite weakness",
    }
    if n in labels:
        return labels[n]
    n = re.sub(r"_(?:Stat|StatusEffect)$", "", n)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", n).replace("_", " ")


class Unsupported(Exception):
    pass


def compile_action(a):
    graph = resolve(a["actionGraph"])
    nodes = [resolve(r) for r in graph.get("nodes", [])]

    def reaches_entry(n, seen=()):
        if n["m_Name"] == "Action Node Entry":
            return True
        if id(n) in seen:
            raise Unsupported()
        p = next((p for p in n["ports"]["valuesList"] if p["_fieldName"] == "enter"), {})
        connections = p.get("connections", [])
        if not connections:
            return False
        if len(connections) != 1:
            raise Unsupported()
        parent = resolve(connections[0]["node"])
        if not reaches_entry(parent, (*seen, id(n))):
            return False
        # These nodes only gate, repeat, or redirect the recorded hit. Any other
        # control flow still needs inspection before its formula is admitted.
        if parent["m_Name"] not in {
            "Action Node Entry",
            "Bool Flow Control",
            "Damage Flat Amount Action",
            "Teleport Unit Action",
            "Get Ability Target",
        }:
            raise Unsupported()
        return True

    damage = [n for n in nodes if "Damage" in n.get("m_Name", "") and reaches_entry(n)]
    if not damage:
        raise Unsupported()

    def port(n, f):
        p = next((p for p in n["ports"]["valuesList"] if p["_fieldName"] == f), {})
        c = p.get("connections", [])
        if len(c) > 1:
            raise Unsupported()
        if not c:
            if f in n:
                raise Unsupported()
            return 0
        return output(resolve(c[0]["node"]), c[0]["fieldName"])

    def output(n, f):
        typ = n["m_Name"]
        if typ == "Action Node Action Params":
            if f == "paramActionTags":
                return [resolve(v)["id"] for v in a["actionTags"]]
            if f == "paramStatusEffectId":
                return resolve(a["paramStatusEffectData"]).get("m_Name")
            if f == "paramStatData":
                return resolve(a["paramStatData"]).get("id")
            if f in ["paramInt", "paramInt2", "paramBool"]:
                return a[f]
        if typ in ["Integer Constant", "Bool Constant"]:
            return n["value"]
        if typ == "Stat Data Constant":
            return resolve(n["value"])["id"]
        if typ == "Status Effect Data Constant":
            return resolve(n["value"])["m_Name"]
        if typ == "Action Node Targeting":
            if f == "ownerEntityHandle":
                return "source"
            if f == "targetEntityHandle":
                return "target"
        if typ == "Action Node Status Effect Params":
            if a["m_Name"] == "Spiked_Action":
                if f == "statusAttachedEntityHandle":
                    return "source"
                if f == "contextTargetEntityHandle":
                    return "target"
                if f == "statusEffectStacks":
                    return {
                        "effect": "Spiked_StatusEffect",
                        "side": "source",
                        "caster_filter": False,
                    }
            if f == "statusSourceEntityHandle":
                return "source"
            if f == "statusAttachedEntityHandle":
                return "target"
        if typ == "Get Ability Target" and a["m_Name"] == "InfusedFist_Teleport_ActionData":
            # Both collision victims receive identical formulas. The broadcast
            # supplies the actual victim, including its own reduction stats.
            if f == "targetEntityHandle":
                return "target"
        if typ == "Get Unit Stat":
            side = port(n, "unitEntityHandle")
            stat = port(n, "statData")
            if side not in ["source", "target"] or not isinstance(stat, str):
                raise Unsupported()
            return {"op": "stat", "side": side, "id": stat}
        if typ == "Get Unit Status Count":
            if port(n, "statusEffectInstanceHandle") != 0:
                raise Unsupported()
            return {
                "op": "stacks",
                "side": port(n, "unitEntityHandle"),
                "id": port(n, "statusEffectId"),
            }
        if typ in ["Addition", "Multiplication"]:
            return {
                "op": {"Addition": "add", "Multiplication": "mul"}[typ],
                "args": [port(n, "in1"), port(n, "in2")],
            }
        if typ == "Divide By 100":
            return {"op": "div100", "args": [port(n, "input")]}
        if typ == "Get Distance Between Two Entities":
            sides = [port(n, "unitA"), port(n, "unitB")]
            if set(sides) != {"source", "target"}:
                raise Unsupported()
            return {"op": "distance"}
        if typ == "Fix Floor" and port(n, "input") == {"op": "distance"}:
            return {"op": "distance_floor", "name": "Distance bonus"}
        if typ == "Get Unit Has Status":
            side = port(n, "unitEntityHandle")
            source = port(n, "sourceEntityHandleNullable")
            if side not in ("source", "target") or source not in (0, "source", "target"):
                raise Unsupported()
            return {"side": side, "effect": port(n, "statusEffectId"), "source": source}
        if typ == "Int Selector":
            condition = port(n, "input")
            if (
                not isinstance(condition, dict)
                or "effect" not in condition
                or port(n, "falseValue") != 0
            ):
                raise Unsupported()
            return {
                "op": "when",
                **condition,
                "name": "Combo bonus"
                if condition["effect"] == "C08_Combo_StatusEffect"
                else clean(condition["effect"]) + " bonus",
                "args": [port(n, "trueValue")],
            }
        raise Unsupported()

    specs = []
    for node in damage:
        if node["m_Name"] not in (
            "Damage Flat Amount Action",
            "Indirect Damage Flat Amount Action",
        ):
            raise Unsupported()
        if (
            port(node, "ownerEntityHandle") != "source"
            or port(node, "targetEntityHandle") != "target"
        ):
            raise Unsupported()
        if port(node, "dealLiteralFlatDamage") or port(node, "ignoreShields"):
            raise Unsupported()
        specs.append(
            {
                "amount": port(node, "amount"),
                "tags": port(node, "actionTags"),
                "direct": node["m_Name"] == "Damage Flat Amount Action",
                "crit": port(node, "additionalCritDamage"),
                "stacks": port(node, "numStacks"),
            }
        )
    # Each repeated strike is already a separate damage broadcast. Never multiply
    # its amount by numberOfTimesToHit. Branches with different formulas are unknown.
    if any(spec != specs[0] for spec in specs[1:]):
        raise Unsupported()
    return specs[0]


catalog = {"build": 24243, "actions": {}, "stats": {}, "statuses": {}}
for a in assets.values():
    if not a.get("actionGraph") or any(
        t in a["m_Name"].lower()
        for t in ["preview", "fake", "rankeddifficulty", "texreplace", "textreplace"]
    ):
        continue
    try:
        catalog["actions"][a["m_Name"]] = compile_action(a)
    except (Unsupported, KeyError, StopIteration):
        pass

# These graphs use mutually exclusive status branches and stack-instance lookup.
for name in [
    "FrostBite_Damage_StatusEffect_Action",
    "Burn_Damage_Action",
    "Poison_Damage_Action",
    "Bleed_Damage_Action",
]:
    a = byname[name]
    amount = {
        "op": "add",
        "args": [
            a["paramInt"],
            {"op": "stat", "side": "source", "id": resolve(a["paramStatData"])["id"]},
        ],
    }
    if "FrostBite" in name or name == "Burn_Damage_Action":
        frost = "FrostBite" in name
        extra = {
            "op": "stat",
            "side": "source",
            "id": byname["DoT_Burn_Damage_Stat" if frost else "DoT_Frostbite_Damage_Stat"]["id"],
        }
        amount = {
            "op": "add",
            "args": [
                amount,
                {
                    "op": "when",
                    "effect": "VestigeAll_Rare_InspirationOfFlame_StatusEffect"
                    if frost
                    else "VestigeAll_Rare_FirstHitFrostbite_StatusEffect",
                    "name": "Burn crossover" if frost else "Frostbite crossover",
                    "args": [extra],
                },
            ],
        }
    catalog["actions"][name] = {
        "amount": amount,
        "tags": [resolve(t)["id"] for t in a["actionTags"]],
        "direct": False,
        "crit": 0,
        "stacks": 0
        if "FrostBite" in name
        else {"effect": resolve(a["paramStatusEffectData"])["m_Name"]},
    }

# Divine Touch's on-hit proc uses the status holder for its Smite stat while
# damaging the proc context target. That ownership differs from normal DoTs,
# so its graph cannot safely share their generic status-target mapping.
a = byname["Smite_Damage_StatusEffect_Action"]
catalog["actions"][a["m_Name"]] = {
    "amount": {
        "op": "add",
        "args": [
            a["paramInt"],
            {"op": "stat", "side": "source", "id": resolve(a["paramStatData"])["id"]},
        ],
    },
    "tags": [resolve(t)["id"] for t in a["actionTags"]],
    "direct": False,
    "crit": 0,
    "stacks": 0,
}

# Legendary Smite Again is an equipment-triggered repeat. Its graph adds the
# owner's Smite stat to the item's flat 50, then applies only its Magic tag.
a = byname["VestigeAll_Legendary_SmiteAgain_Action"]
catalog["actions"][a["m_Name"]] = {
    "amount": {
        "op": "add",
        "args": [
            a["paramInt"],
            {"op": "stat", "side": "source", "id": resolve(a["paramStatData"])["id"]},
        ],
    },
    "tags": [resolve(t)["id"] for t in a["actionTags"]],
    "direct": False,
    "crit": 0,
    "stacks": 0,
}

stat_tags = byname["StatTagData"]
for key in [
    "statTagEntries",
    "bindingOnlyMultiplierStats",
    "weaknessTagEntries",
    "resistanceTagEntries",
]:
    catalog[key] = []
    for entry in stat_tags[key]:
        v = {"stat": resolve(entry["statData"])["id"]}
        if "actionTag" in entry:
            v["tag"] = resolve(entry["actionTag"])["id"]
        if "damageMultiplier" in entry:
            v["multiplier_raw"] = entry["damageMultiplier"]["fixedRaw"]
        catalog[key].append(v)
catalog["status_bonuses"] = [
    {
        "stat": resolve(v["statData"])["id"],
        "effect": resolve(v["requiredStatusEffectData"])["m_Name"],
    }
    for v in byname["GlobalStatusDamageBonusesData"]["entries"]
]
catalog["abilities"] = {}
for k, e in entries.items():
    if e["className"].endswith(".AbilityData"):
        a = assets[k]
        t = a["targetInfo"]
        catalog["abilities"][a["m_Name"]] = {
            # TargetInfo derives this from ScaledRadialDistance rather than
            # serializing a separate useDistanceDamageScaling field.
            "distance": t.get("rangeMode") == 21,
            "range_mode": t.get("rangeMode"),
            "distance_scaling": (
                {
                    "width_raw": t["width"]["fixedRaw"],
                    "min_multiplier_raw": t["scaledMinMultiplier"]["fixedRaw"],
                    "max_multiplier_raw": t["scaledMaxMultiplier"]["fixedRaw"],
                    "segments": t["numSegments"],
                    "custom_segments": t["customSegments"] if t.get("useCustomSegments") else None,
                }
                if t.get("rangeMode") == 21
                else None
            ),
            "status_bonuses": [
                {
                    "stat": resolve(v["statData"])["id"],
                    "effect": resolve(v["requiredStatusEffectData"])["m_Name"],
                }
                for v in a.get("statusDamageBonuses", [])
            ],
        }
catalog["units"] = {}
catalog["classes"] = {}
for k, e in entries.items():
    if e["className"].endswith((".UnitData", ".CharacterClassData")):
        a = assets[k]
        stats = {
            resolve(v["entryStatData"])["id"]: v["entryValue"]
            for v in a.get("statEntries", {}).get("statEntries", [])
            if not v.get("sourceHasStatusEffect", {}).get("fileID")
        }
        if e["className"].endswith(".UnitData"):
            catalog["units"][a["m_Name"]] = stats
        else:
            catalog["classes"][f"C{a['classType']:02}"] = stats
for k, e in entries.items():
    v = assets[k]
    if e["className"].endswith(".StatData"):
        catalog["stats"][v["id"]] = clean(v["m_Name"])
    if e["className"].endswith(".StatusEffectData"):
        catalog["statuses"][v["m_Name"]] = clean(v["m_Name"])
# Retain only facts referenced by formulas/defaults; do not ship game assets or source.
used = set()


def collect(v):
    if isinstance(v, str):
        used.add(v)
    elif isinstance(v, dict):
        used.update(v)
        for a in v.values():
            collect(a)
    elif isinstance(v, list):
        for a in v:
            collect(a)


collect({k: v for k, v in catalog.items() if k not in ["stats", "statuses"]})
used.update(["pvw9uUnH", "3TCx0XPg", "pq6hlsOe", "0QnywPkl", "3FXVvW6T", "FtBty9sm", "TWM45OpA"])
catalog["stats"] = {k: v for k, v in catalog["stats"].items() if k in used}
catalog["statuses"] = {k: v for k, v in catalog["statuses"].items() if k in used}
root = args.game_data
catalog["evidence"] = {
    p: hashlib.sha256((root / p).read_bytes()).hexdigest()
    for p in [
        "Managed/Assembly-CSharp.dll",
        "StreamingAssets/SharedScriptableObjects/SharedScriptableObjects.fb",
    ]
}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n")
print("actions", len(catalog["actions"]), "size", args.output.stat().st_size)
