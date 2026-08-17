"""Clients for the EVE ESI API and zKillboard, used to build a character
danger-assessment report from a locally-pasted list of names.

Both APIs are public/read-only and used here exactly as CCP's third-party
policy expects (see developers.eveonline.com/license-agreement): identity
data from ESI, combat stats from zKillboard, no game-client access.
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter

import cache

ESI_BASE = "https://esi.evetech.net/latest"
ZKILL_BASE = "https://zkillboard.com/api"

# CCP and zKillboard both ask for an identifiable User-Agent with contact info.
USER_AGENT = "eve_local_tool/0.1 (contact: dariufff@gmail.com)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

REQUEST_TIMEOUT = 15

# Shared session so repeated calls to the same two hosts reuse TCP/TLS
# connections instead of renegotiating every time. Sized to comfortably
# cover the thread pool app.py runs characters through in parallel.
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
_adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)


def resolve_names(names: list[str]) -> dict[str, dict]:
    """Resolve character names to IDs via ESI. Returns {name: {"id": int}}.

    Unknown / non-character names (corps, systems, typos) are silently
    dropped from the result.
    """
    names = [n.strip() for n in names if n.strip()]
    if not names:
        return {}

    resolved: dict[str, dict] = {}
    # ESI allows up to 500 names per call.
    for i in range(0, len(names), 500):
        batch = names[i : i + 500]
        resp = SESSION.post(
            f"{ESI_BASE}/universe/ids/",
            params={"datasource": "tranquility"},
            json=batch,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        for char in data.get("characters", []) or []:
            resolved[char["name"]] = {"id": char["id"]}
    return resolved


def get_characters_public_info(character_ids: list[int]) -> dict[int, dict]:
    """Corp / alliance for many characters in as few ESI calls as possible.

    Uses the bulk affiliation endpoint (one POST for every character id)
    plus the bulk names endpoint (one POST for every distinct corp/alliance
    id) instead of the 2-3 per-character calls this used to take.
    """
    character_ids = list({cid for cid in character_ids if cid})
    if not character_ids:
        return {}

    affiliations: list[dict] = []
    for i in range(0, len(character_ids), 1000):  # ESI caps this endpoint at 1000 ids
        resp = SESSION.post(
            f"{ESI_BASE}/characters/affiliation/",
            params={"datasource": "tranquility"},
            json=character_ids[i : i + 1000],
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        affiliations.extend(resp.json() or [])

    entity_ids = {
        eid
        for a in affiliations
        for eid in (a.get("corporation_id"), a.get("alliance_id"))
        if eid
    }
    id_to_name: dict[int, str] = {}
    entity_ids_list = list(entity_ids)
    for i in range(0, len(entity_ids_list), 1000):  # ESI caps this endpoint at 1000 ids too
        resp = SESSION.post(
            f"{ESI_BASE}/universe/names/",
            params={"datasource": "tranquility"},
            json=entity_ids_list[i : i + 1000],
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok:
            for item in resp.json() or []:
                id_to_name[item["id"]] = item["name"]

    result: dict[int, dict] = {}
    for a in affiliations:
        corp_id = a.get("corporation_id")
        alliance_id = a.get("alliance_id")
        result[a["character_id"]] = {
            "corporation_id": corp_id,
            "corporation_name": id_to_name.get(corp_id) if corp_id else None,
            "alliance_id": alliance_id,
            "alliance_name": id_to_name.get(alliance_id) if alliance_id else None,
        }
    return result


def _get_type_info(type_id: int) -> dict:
    """Ship/item name, category id and group name, cached indefinitely as a
    unit (they never change). One type lookup + one group lookup the first
    time a given type id is seen; every subsequent call for it is free.
    """
    name = cache.get_type_name(type_id)
    category_id = cache.get_type_category(type_id)
    group_name = cache.get_type_group_name(type_id)
    if name is not None and category_id is not None and group_name is not None:
        return {"name": name, "category_id": category_id, "group_name": group_name}

    resp = SESSION.get(
        f"{ESI_BASE}/universe/types/{type_id}/",
        params={"datasource": "tranquility"},
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        return {"name": None, "category_id": None, "group_name": None}
    type_data = resp.json()
    name = type_data.get("name")
    if name:
        cache.set_type_name(type_id, name)

    group_id = type_data.get("group_id")
    category_id, group_name = None, None
    if group_id is not None:
        group_resp = SESSION.get(
            f"{ESI_BASE}/universe/groups/{group_id}/",
            params={"datasource": "tranquility"},
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        if group_resp.ok:
            group_data = group_resp.json()
            category_id = group_data.get("category_id")
            group_name = group_data.get("name")
            if category_id is not None:
                cache.set_type_category(type_id, category_id)
                cache.set_group_category(group_id, category_id)
            if group_name:
                cache.set_type_group_name(type_id, group_name)

    return {"name": name, "category_id": category_id, "group_name": group_name}


def resolve_type_name(type_id: int) -> str | None:
    return _get_type_info(type_id)["name"]


def resolve_type_category(type_id: int) -> int | None:
    return _get_type_info(type_id)["category_id"]


def resolve_type_group_name(type_id: int) -> str | None:
    return _get_type_info(type_id)["group_name"]


# Force Recons (Falcon, Arazu, Pilgrim, Rapier) and Covert Ops frigates
# (Anathema, Buzzard, Cheetah, Helios) are the classic covert-cyno alt hulls.
# Haulers excluded on purpose (too many legit logistics pilots, high false
# positive rate). Combat Recons are grouped under EWAR instead (see below)
# rather than double-counted here.
CYNO_CAPABLE_GROUP_NAMES = {
    "Force Recon Ship",
    "Covert Ops",
}

# Black Ops battleships (Redeemer, Sin, Panther, Widow) and the stealth
# bombers they bridge in - a sighting of either means a hotdrop/bomber run
# is a real possibility, distinct from a lone cyno alt.
BLOPS_GROUP_NAMES = {
    "Black Ops",
    "Stealth Bomber",
}

# Dedicated electronic warfare hulls: jams, damps, tracking disruption,
# webs/points from range. A pilot flying these is support, not a brawler,
# but can lock a target down for their gang to finish. Combat Recons
# (Huginn, Lachesis, Rook, Curse) live here rather than in the cyno set.
EWAR_GROUP_NAMES = {
    "Combat Recon Ship",
    "Electronic Attack Ship",
}
# T1 EWAR hulls (Blackbird, Celestis, Maulus) share a generic "Cruiser" or
# "Frigate" group with hundreds of non-EWAR hulls, so they can't be caught
# by group name alone - list them explicitly.
EWAR_HARDCODED_TYPE_IDS = {632, 633, 609}  # Blackbird, Celestis, Maulus


def is_cyno_capable_type(type_id: int | None) -> bool:
    if not type_id:
        return False
    return resolve_type_group_name(type_id) in CYNO_CAPABLE_GROUP_NAMES


def is_blops_type(type_id: int | None) -> bool:
    if not type_id:
        return False
    return resolve_type_group_name(type_id) in BLOPS_GROUP_NAMES


def is_ewar_type(type_id: int | None) -> bool:
    if not type_id:
        return False
    if type_id in EWAR_HARDCODED_TYPE_IDS:
        return True
    return resolve_type_group_name(type_id) in EWAR_GROUP_NAMES


def _matching_ship_names(type_ids: list[int], predicate) -> list[str]:
    """Distinct, resolved names of the type ids satisfying predicate - so
    alerts can say *which* ship was flown, not just that one was."""
    seen_ids: list[int] = []
    for tid in type_ids:
        if tid and tid not in seen_ids and predicate(tid):
            seen_ids.append(tid)
    names = [resolve_type_name(tid) for tid in seen_ids]
    return [n for n in names if n]


def resolve_group_category(group_id: int) -> int | None:
    """Item group -> category id from ESI, cached indefinitely."""
    cached = cache.get_group_category(group_id)
    if cached is not None:
        return cached

    resp = SESSION.get(
        f"{ESI_BASE}/universe/groups/{group_id}/",
        params={"datasource": "tranquility"},
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        return None
    category_id = resp.json().get("category_id")
    if category_id is not None:
        cache.set_group_category(group_id, category_id)
    return category_id


STRUCTURE_CATEGORY_ID = 65  # Upwell structures (Citadels, Engineering Complexes, Refineries)


def is_structure_type(type_id: int | None) -> bool:
    if not type_id:
        return False
    return resolve_type_category(type_id) == STRUCTURE_CATEGORY_ID


def _structure_isk_ratio(groups_breakdown: dict, total_isk_destroyed: float) -> float:
    """Fraction of a character's all-time ISK destroyed that came from
    unpiloted structures rather than ships - the classic "eviction fleet"
    padding pattern: one citadel kill worth billions inflates dangerRatio
    without reflecting any actual 1v1 threat.
    """
    if not total_isk_destroyed:
        return 0.0
    structure_isk = 0
    for group_id_str, group_data in groups_breakdown.items():
        if resolve_group_category(int(group_id_str)) == STRUCTURE_CATEGORY_ID:
            structure_isk += group_data.get("iskDestroyed", 0) or 0
    return structure_isk / total_isk_destroyed


def get_zkill_stats(character_id: int) -> dict:
    """All-time combat stats from zKillboard: K/D, ISK, danger ratio, favorite
    ship, and the padding signals used to discount inflated danger ratios.
    """
    resp = SESSION.get(
        f"{ZKILL_BASE}/stats/characterID/{character_id}/",
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        return {}
    data = resp.json() or {}

    # zkillboard sorts this list by kill count descending, so capping it to
    # the top N still catches any ship type flown often enough to matter -
    # scanning the full list (sometimes 30+ distinct types) against ESI for
    # cyno/EWAR detection made this call unacceptably slow.
    ALL_TIME_SHIP_SCAN_LIMIT = 15

    favorite_ship = None
    all_time_ship_ids: list[int] = []
    for group in data.get("topAllTime", []) or []:
        if group.get("type") == "ship":
            values = (group.get("data") or [])[:ALL_TIME_SHIP_SCAN_LIMIT]
            all_time_ship_ids = [v["shipTypeID"] for v in values if v.get("shipTypeID")]
            if values:
                favorite_ship = resolve_type_name(values[0]["shipTypeID"])
            break

    isk_destroyed = data.get("iskDestroyed", 0) or 0

    return {
        "ships_destroyed": data.get("shipsDestroyed", 0) or 0,
        "ships_lost": data.get("shipsLost", 0) or 0,
        "isk_destroyed": isk_destroyed,
        "isk_lost": data.get("iskLost", 0) or 0,
        "danger_ratio": data.get("dangerRatio", 0) or 0,
        "favorite_ship": favorite_ship,
        "structure_isk_ratio": _structure_isk_ratio(data.get("groups") or {}, isk_destroyed),
        "avg_gang_size": data.get("avgGangSize", 0) or 0,
        # Cyno and Black Ops risk are checked all-time (an alt only ever
        # cynos once in a while, but the risk doesn't expire); EWAR is
        # checked from recent activity only (get_recent_activity /
        # get_recent_kill_quality), since what matters there is what
        # they're flying *now*.
        "cyno_ships_alltime": _matching_ship_names(all_time_ship_ids, is_cyno_capable_type),
        "blops_ships_alltime": _matching_ship_names(all_time_ship_ids, is_blops_type),
    }


# zKillboard's pastSeconds filter caps out at 7 days - conveniently exactly
# the outer edge of the "recent activity" window we want.
RECENT_WINDOW_SHORT = 3 * 24 * 60 * 60  # 3 days
RECENT_WINDOW_LONG = 7 * 24 * 60 * 60  # 7 days


def _fetch_recent_killmails(character_id: int, kind: str) -> list[dict]:
    """kind is 'kills' or 'losses'. Server-side filtered to the last 7 days."""
    resp = SESSION.get(
        f"{ZKILL_BASE}/{kind}/characterID/{character_id}/pastSeconds/{RECENT_WINDOW_LONG}/",
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        return []
    data = resp.json()
    return data if isinstance(data, list) else []


def _parse_killmail_time(killmail_time: str) -> datetime:
    return datetime.strptime(killmail_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def get_recent_activity(character_id: int) -> dict:
    """Ships flown and combat stats from the last 3 days, falling back to 7.

    "Recent" ships/stats reflect both kills and losses (a ship counts whether
    the pilot died in it or killed in it). Also surfaces which other
    character IDs fought alongside this pilot on recent kills, so the caller
    can detect coordinated groups within a pasted list.
    """
    events = []
    for km in _fetch_recent_killmails(character_id, "kills"):
        attacker = next(
            (a for a in km.get("attackers", []) if a.get("character_id") == character_id), None
        )
        if attacker is None:
            continue
        events.append({
            "time": _parse_killmail_time(km["killmail_time"]),
            "kind": "kill",
            "ship_type_id": attacker.get("ship_type_id"),
            "isk": km.get("zkb", {}).get("totalValue", 0) or 0,
            "co_attackers": [
                a["character_id"]
                for a in km.get("attackers", [])
                if a.get("character_id") and a["character_id"] != character_id
            ],
        })
    for km in _fetch_recent_killmails(character_id, "losses"):
        events.append({
            "time": _parse_killmail_time(km["killmail_time"]),
            "kind": "loss",
            "ship_type_id": km.get("victim", {}).get("ship_type_id"),
            "isk": km.get("zkb", {}).get("totalValue", 0) or 0,
            "co_attackers": [],
        })
    events.sort(key=lambda e: e["time"], reverse=True)

    now = datetime.now(timezone.utc)
    within_3d = [e for e in events if (now - e["time"]).total_seconds() <= RECENT_WINDOW_SHORT]
    if within_3d:
        window, filtered = "3d", within_3d
    elif events:  # already capped to 7 days by the API call
        window, filtered = "7d", events
    else:
        window, filtered = None, []

    recent_ship_ids: list[int] = []
    for e in filtered:
        tid = e["ship_type_id"]
        if tid and tid not in recent_ship_ids:
            recent_ship_ids.append(tid)
        if len(recent_ship_ids) == 3:
            break

    recent_kills = [e for e in filtered if e["kind"] == "kill"]
    recent_losses = [e for e in filtered if e["kind"] == "loss"]
    co_attacker_ids = sorted({cid for e in recent_kills for cid in e["co_attackers"]})

    own_ship_ids = [e["ship_type_id"] for e in events if e["ship_type_id"]]  # kills+losses, full 7d

    return {
        "window": window,  # "3d", "7d", or None
        "recent_ships": [resolve_type_name(tid) for tid in recent_ship_ids],
        "recent_kills": len(recent_kills),
        "recent_losses": len(recent_losses),
        "recent_isk_destroyed": sum(e["isk"] for e in recent_kills),
        "recent_isk_lost": sum(e["isk"] for e in recent_losses),
        "co_attacker_ids": co_attacker_ids,
        "cyno_ships_recent": _matching_ship_names(own_ship_ids, is_cyno_capable_type),
        "blops_ships_recent": _matching_ship_names(own_ship_ids, is_blops_type),
        "ewar_ships_recent": _matching_ship_names(own_ship_ids, is_ewar_type),
    }


# Penalties for signals that inflate zKillboard's dangerRatio without
# reflecting real 1v1 threat: killing mostly unpiloted structures, and only
# ever showing up as one of many attackers in blob-sized fleets.
STRUCTURE_RATIO_THRESHOLD = 0.5  # majority of ISK destroyed must be structures to trigger
STRUCTURE_MAX_PENALTY_PCT = 60  # scales linearly from 0 at the threshold up to this at ratio=1.0
LARGE_FLEET_AVG_SIZE_THRESHOLD = 15  # average attackers per kill above this = blob-only
LARGE_FLEET_PENALTY_PCT = 20


def _padding_penalty_from_ratios(structure_isk_ratio: float, avg_gang_size: float) -> float:
    penalty = 0.0
    if structure_isk_ratio > STRUCTURE_RATIO_THRESHOLD:
        penalty += STRUCTURE_MAX_PENALTY_PCT * (
            (structure_isk_ratio - STRUCTURE_RATIO_THRESHOLD) / (1 - STRUCTURE_RATIO_THRESHOLD)
        )
    if avg_gang_size > LARGE_FLEET_AVG_SIZE_THRESHOLD:
        penalty += LARGE_FLEET_PENALTY_PCT
    return round(penalty, 1)


def compute_padding_penalty_pct(stats: dict) -> float:
    """Negative percentage to apply on top of the local group boost, per the
    "eviction fleet padder" pattern: high dangerRatio built almost entirely
    from structure kills and/or never fighting outside a large blob.
    """
    if not stats:
        return 0.0
    return _padding_penalty_from_ratios(
        stats.get("structure_isk_ratio", 0) or 0, stats.get("avg_gang_size", 0) or 0
    )


# zKillboard's pastSeconds filter caps at 7 days, but most pilots' actual
# most recent kill is older than that - a pilot with nothing in the last
# week isn't necessarily inactive, they just haven't killed *this* week.
# This looks past that cap at their most recent kills regardless of age (up
# to a point) to judge whether they're presently hunting real targets or
# just tagging structures in a blob - and rewards or penalizes accordingly.
RECENT_KILL_SAMPLE_SIZE = 20
FRESH_ACTIVITY_BOOST_TIERS = [(14, 25), (30, 15), (90, 5)]  # (days_since <=, boost_pct)

# Beyond ~3 months with no kill, a character is more likely a dormant alt,
# hauler, or industry/farming toon than a real threat - the longer the gap,
# the harder the discount. A character with zero kills on record ever gets
# hit hardest, since that's the classic signature of a non-combat alt.
STALE_PENALTY_TIERS = [(180, 15), (365, 25)]  # (days_since >, penalty_pct), highest match wins
NO_KILLS_EVER_PENALTY_PCT = 30

# Extra reward for volume: a pilot racking up several kills in the last two
# weeks is actively hunting, not just landing on one lucky killmail.
KILL_COUNT_BONUS_WINDOW_DAYS = 14
KILL_COUNT_BONUS_PCT_PER_KILL = 2
KILL_COUNT_BONUS_CAP_PCT = 20


def get_recent_kill_quality(character_id: int) -> dict:
    """Judges the pilot's most recent kills (not bound to zKillboard's 7-day
    pastSeconds cap) for genuine solo/small-gang activity vs. padding vs.
    long-term dormancy, and flags cyno/EWAR-capable hulls flown along the way.
    """
    resp = SESSION.get(
        f"{ZKILL_BASE}/kills/characterID/{character_id}/",
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    empty = {
        "days_since_last_kill": None,
        "activity_pct": -NO_KILLS_EVER_PENALTY_PCT,
        "cyno_ships_detected": [],
        "blops_ships_detected": [],
        "ewar_ships_detected": [],
    }
    if not resp.ok:
        return empty
    kills = resp.json()
    if not isinstance(kills, list) or not kills:
        return empty

    kills.sort(key=lambda km: km.get("killmail_time", ""), reverse=True)
    sample = kills[:RECENT_KILL_SAMPLE_SIZE]

    own_ship_ids = []
    for km in sample:
        attacker = next(
            (a for a in km.get("attackers", []) if a.get("character_id") == character_id), None
        )
        if attacker and attacker.get("ship_type_id"):
            own_ship_ids.append(attacker["ship_type_id"])
    cyno_ships = _matching_ship_names(own_ship_ids, is_cyno_capable_type)
    blops_ships = _matching_ship_names(own_ship_ids, is_blops_type)
    ewar_ships = _matching_ship_names(own_ship_ids, is_ewar_type)

    most_recent_time = _parse_killmail_time(sample[0]["killmail_time"])
    days_since = (datetime.now(timezone.utc) - most_recent_time).days

    stale_penalty_pct = 0
    for threshold_days, penalty_pct in STALE_PENALTY_TIERS:
        if days_since > threshold_days:
            stale_penalty_pct = penalty_pct
    if stale_penalty_pct:
        return {
            "days_since_last_kill": days_since,
            "activity_pct": -stale_penalty_pct,
            "cyno_ships_detected": cyno_ships,
            "blops_ships_detected": blops_ships,
            "ewar_ships_detected": ewar_ships,
        }

    total_isk = sum(km.get("zkb", {}).get("totalValue", 0) or 0 for km in sample)
    structure_isk = sum(
        km.get("zkb", {}).get("totalValue", 0) or 0
        for km in sample
        if is_structure_type(km.get("victim", {}).get("ship_type_id"))
    )
    structure_ratio = (structure_isk / total_isk) if total_isk else 0.0
    avg_gang_size = sum(len(km.get("attackers", []) or []) for km in sample) / len(sample)

    padding_pct = _padding_penalty_from_ratios(structure_ratio, avg_gang_size)
    if padding_pct > 0:
        activity_pct = -padding_pct
    else:
        base_boost = next(
            (boost for max_days, boost in FRESH_ACTIVITY_BOOST_TIERS if days_since <= max_days),
            0,
        )
        kills_in_window = sum(
            1
            for km in sample
            if (datetime.now(timezone.utc) - _parse_killmail_time(km["killmail_time"])).days
            <= KILL_COUNT_BONUS_WINDOW_DAYS
        )
        count_bonus = min(kills_in_window * KILL_COUNT_BONUS_PCT_PER_KILL, KILL_COUNT_BONUS_CAP_PCT)
        activity_pct = base_boost + count_bonus

    return {
        "days_since_last_kill": days_since,
        "activity_pct": round(activity_pct, 1),
        "structure_isk_ratio": round(structure_ratio, 3),
        "avg_gang_size": round(avg_gang_size, 1),
        "cyno_ships_detected": cyno_ships,
        "blops_ships_detected": blops_ships,
        "ewar_ships_detected": ewar_ships,
    }
