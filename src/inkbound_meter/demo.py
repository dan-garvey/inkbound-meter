"""Clearly labelled synthetic data for checking the overlay without a game."""

from .model import Meter
from .parser import Event


def demo_meter() -> Meter:
    meter = Meter()
    meter.apply(Event("run_create"))
    meter.apply(Event("connection", data={"connected": True, "seed": 123456}))
    meter.apply(Event("combat_start", data={"zone": 1}))
    roster = [
        (1, "Ember", "C01", [("Bonk_Damage_Action", 18350), ("Burn_Damage_Action", 12780)]),
        (2, "Wisp", "C03", [("PsychicPulse_Damage_Action", 17240), ("Smite_Damage_Action", 4280)]),
        (3, "Thorn", "C02", [("Poison_Damage_Action", 9340), ("Slash_Damage_Action", 3220)]),
    ]
    for entity, name, character, sources in roster:
        meter.apply(Event("player", data={"id": entity, "name": name, "class_id": character}))
        for action, amount in sources:
            meter.apply(
                Event(
                    "damage",
                    data={
                        "source": entity,
                        "target": 99,
                        "amount": amount,
                        "action": action,
                        "ability": None,
                        "effect": None,
                        "in_combat": True,
                        "target_team": "Enemy",
                        "dodged": False,
                        "critical": False,
                    },
                )
            )
    return meter
