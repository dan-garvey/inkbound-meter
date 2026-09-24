"""Extract binding labels and class IDs from an installed build's local assets."""

import argparse
import json
import re
from pathlib import Path

import UnityPy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--bundles", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    keys, values = {}, {}
    for filename in (
        "localization-assets-shared_assets_all.bundle",
        "localization-string-tables-en-us_assets_all.bundle",
    ):
        for obj in UnityPy.load(str(args.bundles / filename)).objects:
            if obj.type.name != "MonoBehaviour":
                continue
            data = obj.read_typetree()
            if not data.get("m_Name", "").startswith("SoAbilityDataStringTable"):
                continue
            keys.update({v["m_Key"]: v["m_Id"] for v in data.get("m_Entries", [])})
            values.update({v["m_Id"]: v["m_Localized"] for v in data.get("m_TableData", [])})
    result = {"build": 24243, "abilities": {}, "classes": {}}
    for asset in json.loads(args.assets.read_text()).values():
        key = asset.get("abilityName")
        if key in keys and keys[key] in values:
            result["abilities"][asset["id"]] = {
                "name": re.sub(r"<[^>]+>", "", values[keys[key]]),
                "asset": asset["m_Name"],
            }
        if "classType" in asset and "id" in asset:
            result["classes"][asset["id"]] = f"C{asset['classType']:02}"
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"Extracted {len(result['abilities'])} binding labels")


if __name__ == "__main__":
    main()
