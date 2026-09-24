from inkbound_meter.parser import Event, parse_line


def broadcast(body):
    return f"0T12:00:00 01 I [EventSystem] broadcasting {body}\n"


def damage(amount=25, source=1, target=99, action="Bonk_Damage_Action", **kwargs):
    fields = {
        "TargetUnitTeam": "Enemy",
        "IsInActiveCombat": "True",
        "IsCriticalHit": "False",
        "WasDodged": "False",
        "AbilityData": "AbilityData-Bonk_AbilityData (fixture)",
        "StatusEffectData": "(none)",
        "LootableData": "(none)",
    }
    fields.update(kwargs)
    return broadcast(
        "EventOnUnitDamaged-WorldStateChangeDamageUnit-"
        f"TargetUnitHandle:(EntityHandle:{target})-SourceEntityHandle:(EntityHandle:{source})-"
        f"DamageAmount:{amount}-ActionData:ActionData-{action} (fixture)-"
        + "-".join(f"{k}:{v}" for k, v in fields.items())
    )


def start():
    return (
        broadcast("EventPartyRunCreate")
        + broadcast("EventOnRunConnected-IsOnConnected:True-RunSeed:1234")
        + "0T12:00:00 01 I Player One (EntityHandle:1) is playing ability Bonk\n"
    )


def begin(meter):
    for line in start().splitlines():
        meter.apply(parse_line(line))
    meter.apply(Event("combat_start", data={"zone": 99}))
