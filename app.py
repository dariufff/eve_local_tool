"""Local web tool: paste a list of EVE Online character names and get a
danger-assessment report (K/D, ISK efficiency, usual ship, recent activity).

Not affiliated with or endorsed by CCP hf. Uses only the public ESI API and
zKillboard's public stats endpoint; the character list is pasted manually by
the user, never captured from the game client.
"""
from __future__ import annotations

import difflib
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, render_template, request

import cache
import eve_api

app = Flask(__name__)

# Per-character work (zKillboard stats/recent-kills/kill-quality calls) is
# independent across characters, so it's parallelized with a thread pool -
# network I/O releases the GIL while waiting, so this is a real speedup, not
# just concurrency theater. Kept modest since we're one anonymous client
# sharing zKillboard with everyone else, not trying to hammer it.
MAX_WORKERS = 8


def _dedupe(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


EMPTY_ACTIVITY = {
    "window": None,
    "recent_ships": [],
    "recent_kills": 0,
    "recent_losses": 0,
    "recent_isk_destroyed": 0,
    "recent_isk_lost": 0,
    "co_attacker_ids": [],
}


def _fetch_character_record(char_id: int, name: str, public_info: dict) -> dict:
    """Everything that needs a network call for one character. Runs inside
    the thread pool, so must not touch Flask/request state - just data in,
    dict out.
    """
    try:
        stats = eve_api.get_zkill_stats(char_id)
    except requests.RequestException:
        stats = {}

    try:
        recent = eve_api.get_recent_activity(char_id)
    except requests.RequestException:
        recent = EMPTY_ACTIVITY

    try:
        kill_quality = eve_api.get_recent_kill_quality(char_id)
    except requests.RequestException:
        kill_quality = {"days_since_last_kill": None, "activity_pct": 0.0}

    # Cyno and Black Ops/bomber risk are worth flagging even from all-time
    # history (an alt doesn't stop being one). EWAR is only checked from
    # recent activity - what matters there is what they're flying right now.
    cyno_ships = _dedupe(
        stats.get("cyno_ships_alltime", [])
        + recent.get("cyno_ships_recent", [])
        + kill_quality.get("cyno_ships_detected", [])
    )
    blops_ships = _dedupe(
        stats.get("blops_ships_alltime", [])
        + recent.get("blops_ships_recent", [])
        + kill_quality.get("blops_ships_detected", [])
    )
    ewar_ships = _dedupe(
        recent.get("ewar_ships_recent", []) + kill_quality.get("ewar_ships_detected", [])
    )

    record = {
        "character_id": char_id,
        "name": name,
        "corporation_id": public_info.get("corporation_id"),
        "corporation_name": public_info.get("corporation_name"),
        "alliance_id": public_info.get("alliance_id"),
        "alliance_name": public_info.get("alliance_name"),
        "zkb_score": stats.get("danger_ratio", 0) or 0,
        "padding_penalty_pct": eve_api.compute_padding_penalty_pct(stats),
        "structure_isk_ratio": stats.get("structure_isk_ratio", 0),
        "avg_gang_size": stats.get("avg_gang_size", 0),
        "days_since_last_kill": kill_quality.get("days_since_last_kill"),
        "recent_kill_activity_pct": kill_quality.get("activity_pct", 0.0),
        "recent_kill_structure_ratio": kill_quality.get("structure_isk_ratio"),
        "recent_kill_avg_gang_size": kill_quality.get("avg_gang_size"),
        "cyno_ships": cyno_ships,
        "blops_ships": blops_ships,
        "ewar_ships": ewar_ships,
        "ships_destroyed": stats.get("ships_destroyed", 0),
        "ships_lost": stats.get("ships_lost", 0),
        "isk_destroyed": stats.get("isk_destroyed", 0),
        "isk_lost": stats.get("isk_lost", 0),
        "favorite_ship": stats.get("favorite_ship"),
        "activity_window": recent.get("window"),
        "recent_ships": recent.get("recent_ships", []),
        "recent_kills": recent.get("recent_kills", 0),
        "recent_losses": recent.get("recent_losses", 0),
        "recent_isk_destroyed": recent.get("recent_isk_destroyed", 0),
        "recent_isk_lost": recent.get("recent_isk_lost", 0),
        "co_attacker_ids": recent.get("co_attacker_ids", []),
    }
    cache.set(char_id, name, record)
    return record


def build_report(names: list[str]) -> tuple[list[dict], list[str]]:
    resolved = eve_api.resolve_names(names)
    unresolved = [n for n in names if n.strip() and n.strip() not in resolved]

    results = []
    to_fetch: list[tuple[int, str]] = []  # (character_id, name) not in cache
    for name, info in resolved.items():
        cached = cache.get(info["id"])
        if cached is not None:
            results.append(cached)
        else:
            to_fetch.append((info["id"], name))

    if to_fetch:
        # One bulk call for corp/alliance instead of one per character.
        try:
            public_infos = eve_api.get_characters_public_info([cid for cid, _ in to_fetch])
        except requests.RequestException:
            public_infos = {}

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [
                executor.submit(_fetch_character_record, cid, name, public_infos.get(cid, {}))
                for cid, name in to_fetch
            ]
            results.extend(future.result() for future in futures)

    apply_local_danger_score(results)
    detect_multibox_groups(results)
    results.sort(key=lambda r: r["analyzed_score"], reverse=True)
    return results, unresolved


# The "analyzed" score is zKillboard's dangerRatio boosted by a percentage
# for each pasted-list-only signal detected (real-time context zKillboard's
# own all-time number can't see). A pilot with no such signals just keeps
# their zkb score; a coordinated group multiplies it, uncapped.
CORP_BOOST_PCT = 20
ALLIANCE_BOOST_PCT = 5
JOINT_KILL_BOOST_PCT = 40
LARGE_GROUP_BOOST_PCT = 20
LARGE_GROUP_THRESHOLD = 3  # a coordinated cluster bigger than this triggers the bonus


def _find_group_sizes(records: list[dict]) -> dict[int, int]:
    """Size of the connected component each character belongs to, where an
    edge means two pilots fought on the same recent killmail together."""
    ids = {r["character_id"] for r in records}
    adjacency: dict[int, set[int]] = {cid: set() for cid in ids}
    for r in records:
        cid = r["character_id"]
        for other_id in r.get("co_attacker_ids", []):
            if other_id in ids:
                adjacency[cid].add(other_id)
                adjacency[other_id].add(cid)

    sizes: dict[int, int] = {}
    visited: set[int] = set()
    for start in ids:
        if start in visited:
            continue
        component: set[int] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(adjacency[node] - component)
        for node in component:
            sizes[node] = len(component)
        visited |= component
    return sizes


def apply_local_danger_score(records: list[dict]) -> None:
    """Cross-character signals that only make sense within this pasted batch,
    computed fresh every request since they depend on who else is in the
    current list, not just on the (cached) per-character data.
    """
    id_to_name = {r["character_id"]: r["name"] for r in records}

    by_corp: dict[int, list[int]] = {}
    by_alliance: dict[int, list[int]] = {}
    for r in records:
        if r.get("corporation_id"):
            by_corp.setdefault(r["corporation_id"], []).append(r["character_id"])
        if r.get("alliance_id"):
            by_alliance.setdefault(r["alliance_id"], []).append(r["character_id"])

    group_sizes = _find_group_sizes(records)

    for r in records:
        char_id = r["character_id"]

        same_corp_ids = [
            cid for cid in by_corp.get(r.get("corporation_id"), []) if cid != char_id
        ]
        same_alliance_ids = [
            cid for cid in by_alliance.get(r.get("alliance_id"), []) if cid != char_id
        ]
        same_alliance_only_ids = [cid for cid in same_alliance_ids if cid not in same_corp_ids]

        ally_ids = {cid for cid in r.get("co_attacker_ids", []) if cid in id_to_name}
        # Symmetric: also flag it if the other pilot's recent kills saw this one.
        for other in records:
            if other["character_id"] != char_id and char_id in other.get("co_attacker_ids", []):
                ally_ids.add(other["character_id"])

        r["same_corp_count"] = len(same_corp_ids)
        r["same_alliance_count"] = len(same_alliance_only_ids)
        r["recent_allies"] = [id_to_name[cid] for cid in ally_ids]
        r["group_size"] = group_sizes.get(char_id, 1)

        boost_pct = 0
        if same_corp_ids:
            boost_pct += CORP_BOOST_PCT
        if same_alliance_only_ids:
            boost_pct += ALLIANCE_BOOST_PCT
        if ally_ids:
            boost_pct += JOINT_KILL_BOOST_PCT
        if r["group_size"] > LARGE_GROUP_THRESHOLD:
            boost_pct += LARGE_GROUP_BOOST_PCT
        r["boost_pct"] = boost_pct

        net_pct = (
            boost_pct
            - r.get("padding_penalty_pct", 0)
            + r.get("recent_kill_activity_pct", 0)
        )
        r["net_pct"] = round(net_pct, 1)
        r["analyzed_score"] = round(max(r["zkb_score"] * (1 + net_pct / 100), 0), 1)


# Multibox alts commonly share a naming pattern: same first or last name
# across the whole squad, a shared base name with a numbered/roman-numeral
# suffix ("Hauler1", "Hauler2", "Miner III"), or just near-identical
# spelling. A shared token shorter than this is too generic on its own
# (e.g. "The", "Sir") to mean anything.
MULTIBOX_TOKEN_MIN_LENGTH = 4
MULTIBOX_SIMILARITY_THRESHOLD = 0.82


def _strip_trailing_number(name: str) -> str:
    tokens = name.split()
    if not tokens:
        return name
    last = tokens[-1]
    if len(tokens) > 1 and re.fullmatch(r"[IVXLCDM]{1,5}", last, re.IGNORECASE):
        return " ".join(tokens[:-1])
    stripped_last = re.sub(r"\d+$", "", last)
    if stripped_last == last:
        return name
    tokens[-1] = stripped_last
    return " ".join(t for t in tokens if t)


def detect_multibox_groups(records: list[dict]) -> None:
    """Flags pasted-list names that look like they could be the same
    person's alts, purely from string similarity - no API calls involved.
    """
    entries = [(r["character_id"], r["name"]) for r in records]
    suspects: dict[int, set[str]] = {cid: set() for cid, _ in entries}

    def link(cid_a: int, name_a: str, cid_b: int, name_b: str) -> None:
        if cid_a != cid_b:
            suspects[cid_a].add(name_b)
            suspects[cid_b].add(name_a)

    key_fns = [
        lambda n: n.split()[0].lower() if n.split() else "",
        lambda n: n.split()[-1].lower() if n.split() else "",
        lambda n: _strip_trailing_number(n).lower(),
    ]
    for key_fn in key_fns:
        groups: dict[str, list[tuple[int, str]]] = {}
        for cid, name in entries:
            key = key_fn(name)
            if len(key) >= MULTIBOX_TOKEN_MIN_LENGTH:
                groups.setdefault(key, []).append((cid, name))
        for members in groups.values():
            if len(members) > 1:
                for cid_a, name_a in members:
                    for cid_b, name_b in members:
                        link(cid_a, name_a, cid_b, name_b)

    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            cid_a, name_a = entries[i]
            cid_b, name_b = entries[j]
            if name_b in suspects[cid_a]:
                continue
            ratio = difflib.SequenceMatcher(None, name_a.lower(), name_b.lower()).ratio()
            if ratio >= MULTIBOX_SIMILARITY_THRESHOLD:
                link(cid_a, name_a, cid_b, name_b)

    for r in records:
        r["multibox_suspects"] = sorted(suspects[r["character_id"]])


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", results=None, unresolved=None, raw_input="")


@app.route("/analyze", methods=["POST"])
def analyze():
    raw_input = request.form.get("names", "")
    names = [line.strip() for line in raw_input.splitlines() if line.strip()]
    results, unresolved = build_report(names)
    return render_template(
        "index.html", results=results, unresolved=unresolved, raw_input=raw_input
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
