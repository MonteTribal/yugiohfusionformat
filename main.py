"""
Created by MonteTribal
Posted to: https://github.com/MonteTribal/yugiohfusionformat
License: Creative Commons Zero v1.0 Universal

Reads fusion_cards.json (output of get_fusion_cards.py).
Picks 20 random cards and extracts fusion material info from the first line of each desc.

Three segment types are handled per material:
  1. EXACT NAME   — "ABC-Dragon Buster"  → record name directly
  2. ARCHETYPE    — 1 "Amazoness" monster → API: archetype=Amazoness
  3. GENERIC      — 1 DARK Dragon monster → API: attribute=DARK, race=Dragon

After the initial pass, any material card that is itself a Fusion Monster has its
own materials resolved recursively, until no unresolved fusion materials remain.
A final list with stats is printed at the end.

Verbosity is controlled by the VERBOSE_* flags at the top of main().
"""

import base64
import json
import random
import re
import time
import urllib.request
import urllib.parse
import urllib.error

FUSION_JSON = "fusion_cards.json"
API_BASE    = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://ygoprodeck.com/",
    "Origin": "https://ygoprodeck.com",
}

# ── Lookup tables ─────────────────────────────────────────────────────────────

ATTRIBUTES = {"DARK", "LIGHT", "EARTH", "WIND", "WATER", "FIRE", "DIVINE"}

RACES = {
    "Dragon", "Spellcaster", "Zombie", "Warrior", "Beast-Warrior", "Beast",
    "Winged Beast", "Fiend", "Fairy", "Insect", "Machine", "Sea Serpent",
    "Aqua", "Pyro", "Rock", "Thunder", "Plant", "Psychic", "Reptile",
    "Dinosaur", "Fish", "Cyberse", "Illusion", "Gemini", "Tuner", "Normal",
}

EXTRA_DECK_TYPES = {"Fusion", "Synchro", "Xyz", "Link", "Ritual", "Pendulum"}

# Main-deck monster types used for random type selection in generic segment searches.
# Spaces are handled by urllib.parse.urlencode (e.g. "Effect Monster" → "Effect%20Monster").
MAIN_DECK_MONSTER_TYPES = [
    "Effect Monster",
    "Flip Effect Monster",
    "Flip Tuner Effect Monster",
    "Gemini Monster",
    "Normal Monster",
    "Normal Tuner Monster",
]

QUOTED_RE = re.compile(r'"([^"]+)"')


# ── JSON loading ──────────────────────────────────────────────────────────────

def load_json_safe(path: str, verbose: bool = True) -> dict:
    """Load fusion_cards.json, repairing truncated JSON if needed."""
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().rstrip()
    for suffix in ["", "}]", "]", "]}", "}]}"]:
        try:
            result = json.loads(raw + suffix)
            if suffix and verbose:
                print(f"[info] JSON repaired (appended '{suffix}').")
            return result
        except json.JSONDecodeError:
            continue
    raise ValueError(f"Could not parse {path} even after repair attempts.")


# ── Segment classification ────────────────────────────────────────────────────

class Segment:
    KIND_EXACT     = "exact"
    KIND_ARCHETYPE = "archetype"
    KIND_GENERIC   = "generic"

    def __init__(self, raw: str):
        self.raw        = raw.strip()
        self.kind       = None
        self.name       = None
        self.archetype  = None
        self.attribute  = None
        self.race       = None
        self.extra_type = None
        self._classify()

    def _classify(self):
        quoted  = QUOTED_RE.findall(self.raw)
        outside = QUOTED_RE.sub("", self.raw).strip(" +,\r\n")

        if not quoted:
            self.kind = self.KIND_GENERIC
            self._extract_generic(self.raw)
        elif outside and re.search(r'\b(monster|card|fusion|synchro|xyz|link)\b',
                                    outside, re.IGNORECASE):
            self.kind      = self.KIND_ARCHETYPE
            self.archetype = quoted[0].strip()
            self._extract_generic(outside)
        else:
            self.kind = self.KIND_EXACT
            self.name = quoted[0].strip()

    def _extract_generic(self, text: str):
        for w in re.split(r'[\s\-/]+', text):
            w = w.strip(".,")
            if w.upper() in ATTRIBUTES:
                self.attribute = w.upper()
            if w.capitalize() in RACES:
                self.race = w.capitalize()
            if w.capitalize() in EXTRA_DECK_TYPES:
                self.extra_type = w.capitalize()

    def describe(self) -> str:
        if self.kind == self.KIND_EXACT:
            return f'exact card: "{self.name}"'
        parts = []
        if self.kind == self.KIND_ARCHETYPE:
            parts.append(f'archetype="{self.archetype}"')
        if self.attribute:
            parts.append(f"attribute={self.attribute}")
        if self.race:
            parts.append(f"race={self.race}")
        if self.extra_type:
            parts.append(f"type={self.extra_type} Monster")
        return "  |  ".join(parts) if parts else f'raw="{self.raw}"'


def parse_segments(first_line: str) -> list[Segment]:
    """Split a material line on '+' and return classified Segment objects."""
    segments = []
    for part in re.split(r'\s*\+\s*', first_line):
        part = part.strip()
        if not part or part.startswith("["):
            continue
        segments.append(Segment(part))
    return segments


# ── API helpers ───────────────────────────────────────────────────────────────

def api_query(params: dict, verbose: bool = True) -> list[dict]:
    """Execute a parameterised API request; return up to 3 results."""
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("data", [])[:3]
    except urllib.error.HTTPError as e:
        if e.code == 400:
            return []   # 400 = no results, normal
        if verbose:
            print(f"    [API] HTTP {e.code} | params={params}")
        return []
    except Exception as e:
        if verbose:
            print(f"    [API] Error: {e}")
        return []


def search_segment(seg: Segment, verbose: bool = True) -> list[dict]:
    """Build API params from a Segment and return matching cards."""
    if seg.kind == Segment.KIND_EXACT:
        return []

    params: dict[str, str] = {}
    if seg.kind == Segment.KIND_ARCHETYPE:
        params["archetype"] = seg.archetype
        if seg.attribute:
            params["attribute"] = seg.attribute
        if seg.race:
            params["race"] = seg.race
    elif seg.kind == Segment.KIND_GENERIC:
        if seg.attribute:
            params["attribute"] = seg.attribute
        if seg.race:
            params["race"] = seg.race
        if seg.extra_type:
            # Explicit extra-deck type from the material line (e.g. "Fusion Monster")
            params["type"] = f"{seg.extra_type} Monster"
        else:
            # No explicit type — randomly pick a main-deck monster type
            params["type"] = random.choice(MAIN_DECK_MONSTER_TYPES)
            if verbose:
                print(f"      [type] randomly selected: \"{params['type']}\"")

    if not params:
        return []

    time.sleep(0.1)
    return api_query(params, verbose=verbose)


# ── Registry helpers ──────────────────────────────────────────────────────────

def register(collected: dict, card: dict, source: str):
    """Add a card to the collected registry, tagged with its source."""
    cid = card.get("id", card.get("name", "unknown"))
    if cid not in collected:
        collected[cid] = {"card": card, "sources": set()}
    collected[cid]["sources"].add(source)


def register_exact(collected: dict, name: str):
    """Register a named material as a placeholder when no full card data is available."""
    key = f"exact::{name}"
    if key not in collected:
        collected[key] = {"card": {"name": name, "type": "—", "atk": "—", "def": "—"}, "sources": set()}
    collected[key]["sources"].add("exact material")


def is_fusion(card: dict) -> bool:
    """Return True if the card is any variety of Fusion Monster."""
    return "fusion" in card.get("type", "").lower()


# ── Material resolution ───────────────────────────────────────────────────────

def resolve_materials(card: dict, collected: dict,
                      indent: str = "    ", verbose: bool = True):
    """
    Parse the first desc line of a card, search for its materials, register them.
    Called for the initial 20 cards and recursively for any fusion materials found.
    """
    first_line = card.get("desc", "").split("\n")[0].strip()

    if verbose:
        print(f"{indent}Mat  : {first_line}")

    for seg in parse_segments(first_line):
        if verbose:
            print(f"{indent}▸ {seg.describe()}")

        if seg.kind == Segment.KIND_EXACT:
            register_exact(collected, seg.name)
            if verbose:
                print(f"{indent}  → (specific card — no lookup needed)")
            continue

        results = search_segment(seg, verbose=verbose)
        if not results:
            if verbose:
                print(f"{indent}  → no API results found")
            continue

        source = (
            f'archetype "{seg.archetype}"' if seg.kind == Segment.KIND_ARCHETYPE
            else f"generic ({seg.describe()})"
        )
        for fc in results:
            register(collected, fc, source)
            if verbose:
                print(f"{indent}  → {fc['name']}  [{fc.get('type','?')}]"
                      f"  ATK/{fc.get('atk','?')}  DEF/{fc.get('def','?')}")


def run_initial_pass(sample: list[dict], collected: dict, verbose: bool = True):
    """Register and resolve materials for the randomly selected fusion cards."""
    if verbose:
        print("=" * 68)
        print(f"  {len(sample)} Random Fusion Cards — Material Analysis")
        print("=" * 68)

    for i, card in enumerate(sample, 1):
        register(collected, card, "fusion selection")
        if verbose:
            print(f"\n{i:>2}. {card.get('name','Unknown')}")
            print(f"    Type : {card.get('type','?')}  |  "
                  f"LV{card.get('level','?')}  "
                  f"ATK/{card.get('atk','?')}  DEF/{card.get('def','?')}")
        resolve_materials(card, collected, indent="    ", verbose=verbose)


def run_recursive_passes(collected: dict, resolved_ids: set,
                         verbose: bool = True) -> int:
    """
    Repeatedly find fusion monsters in the collected set that haven't had their
    materials resolved yet, resolve them, and repeat until none remain.
    Returns the number of passes performed.
    """
    iteration = 0

    while True:
        pending = [
            entry["card"]
            for entry in list(collected.values())
            if is_fusion(entry["card"])
            and entry["card"].get("id") not in resolved_ids
            and entry["card"].get("id") is not None
        ]
        if not pending:
            break

        iteration += 1
        if verbose:
            print(f"\n{'=' * 68}")
            print(f"  Fusion Materials — Pass {iteration}  ({len(pending)} nested fusion(s))")
            print(f"{'=' * 68}")

        for card in pending:
            resolved_ids.add(card.get("id"))
            if verbose:
                print(f"\n  ↳ {card.get('name','Unknown')}  [{card.get('type','?')}]"
                      f"  LV{card.get('level','?')}  "
                      f"ATK/{card.get('atk','?')}  DEF/{card.get('def','?')}")
            resolve_materials(card, collected, indent="      ", verbose=verbose)

    return iteration


def resolve_exact_ids(collected: dict, verbose: bool = True):
    """
    For any exact-name placeholder (type == "—"), query the API by exact name
    and replace the placeholder with the full card data if found.
    """
    placeholders = [
        (key, entry)
        for key, entry in list(collected.items())
        if entry["card"].get("type") == "—"
    ]

    if not placeholders:
        return

    if verbose:
        print(f"\n[Resolving {len(placeholders)} exact-name placeholder(s)...]")

    for key, entry in placeholders:
        name = entry["card"].get("name", "")
        params = {"name": name}   # exact name match
        url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results = data.get("data", [])
            if results:
                full_card = results[0]
                entry["card"] = full_card
                new_id = full_card.get("id")
                if new_id and new_id not in collected:
                    collected[new_id] = entry
                    del collected[key]
                if verbose:
                    print(f"  ✓ {name:<45} → ID {full_card.get('id', '?')}")
            else:
                if verbose:
                    print(f"  ✗ {name:<45} → not found")
        except urllib.error.HTTPError as e:
            if verbose:
                print(f"  ✗ {name:<45} → HTTP {e.code}")
        except Exception as e:
            if verbose:
                print(f"  ✗ {name:<45} → error: {e}")
        time.sleep(0.1)


def print_final_list(collected: dict, iteration: int, verbose: bool = True):
    """Print the categorised final card list and summary stats."""
    all_entries = list(collected.values())

    fusion_selections  = [e for e in all_entries if "fusion selection" in e["sources"]]
    nested_fusions     = [e for e in all_entries
                          if is_fusion(e["card"]) and "fusion selection" not in e["sources"]]
    plain_materials    = [e for e in all_entries
                          if not is_fusion(e["card"])
                          and "fusion selection" not in e["sources"]
                          and e["card"].get("type") != "—"]
    exact_placeholders = [e for e in all_entries if e["card"].get("type") == "—"]

    def card_line(entry, indent="    "):
        c   = entry["card"]
        src = entry["sources"] - {"fusion selection", "exact material"}
        src_str  = f"  ← {', '.join(sorted(src))}" if src else ""
        cid      = c.get("id", "—")
        cid_str  = f"[{cid}]" if cid != "—" else "[—]"
        return (f"{indent}{cid_str:<12}  {c.get('name','Unknown'):<45}  [{c.get('type','—')}]"
                f"  LV{c.get('level','—')}  ATK/{c.get('atk','—')}  DEF/{c.get('def','—')}{src_str}")

    def print_section(title, entries):
        print(f"\n  {title} ({len(entries)})")
        print(f"  {'-' * 55}")
        for e in sorted(entries, key=lambda e: e["card"].get("name", "").lower()):
            print(card_line(e))

    if verbose:
        print(f"\n{'=' * 68}")
        print("  FINAL CARD LIST")
        print(f"{'=' * 68}")
        print_section("◆ Fusion Selection", fusion_selections)
        if nested_fusions:
            print_section("◈ Nested Fusion Materials", nested_fusions)
        print_section("◇ Non-Fusion Material Cards", plain_materials)
        if exact_placeholders:
            print_section("○ Exact-Name Materials (name only)", exact_placeholders)

    # Stats always print regardless of verbose — they are the summary output
    print(f"\n{'=' * 68}")
    print("  STATS")
    print(f"  {'-' * 55}")
    print(f"  Initial fusion cards selected    : {len(fusion_selections)}")
    print(f"  Nested fusion materials found    : {len(nested_fusions)}")
    print(f"  Recursive resolution passes      : {iteration}")
    print(f"  Non-fusion material cards        : {len(plain_materials)}")
    print(f"  Exact-name placeholders          : {len(exact_placeholders)}")
    print(f"  {'─' * 37}")
    print(f"  Total unique cards in list       : {len(all_entries)}")
    print(f"{'=' * 68}\n")


# ── Deck builder ──────────────────────────────────────────────────────────────

EXTRA_DECK_FRAME_TYPES = {"fusion", "synchro", "xyz", "link"}
STAPLES_JSON = "fusion_spell_trap_staples.json"
STAPLES_COUNT = 15


def load_staples(path: str, count: int, verbose: bool = True) -> list[dict]:
    """
    Load fusion_spell_trap_staples.json and return a random sample of `count` cards.
    Expects the same format as fusion_cards.json: {"data": [...]} or a bare list.
    Returns an empty list if the file is missing or unreadable.
    Warns if fewer cards are available than requested.
    """
    try:
        data = load_json_safe(path, verbose=verbose)
        cards = data.get("data", data) if isinstance(data, dict) else data
        available = len(cards)
        actual    = min(count, available)
        sample    = random.sample(cards, actual)
        if verbose:
            print(f"\n{'=' * 68}")
            print(f"  STAPLES")
            print(f"  {'-' * 55}")
            print(f"  File        : {path}")
            print(f"  Available   : {available}")
            print(f"  Requested   : {count}")
            if available < count:
                print(f"  ⚠  Only {available} card(s) available — "
                      f"added {actual} instead of {count}.")
            else:
                print(f"  Selected    : {actual}")
            print(f"  Cards chosen:")
            for c in sample:
                print(f"    • [{c.get('id','?')}]  {c.get('name','Unknown')}  "
                      f"[{c.get('type','?')}]")
            print(f"{'=' * 68}")
        return sample
    except FileNotFoundError:
        if verbose:
            print(f"\n[Staples] '{path}' not found — skipping staples.")
        return []
    except Exception as e:
        if verbose:
            print(f"\n[Staples] Could not load '{path}': {e}")
        return []


def build_deck_url(collected: dict, staples: list[dict],
                   verbose_deck: bool = True, verbose_staples: bool = True) -> str:
    """
    Split all collected cards into MAIN_DECK and EXTRA_DECK by card type,
    append `staples` IDs to MAIN_DECK, base64-encode each list, and return
    a YGOPRODeck deckbuilder URL.
    """
    main_deck  = []
    extra_deck = []

    for entry in collected.values():
        card = entry["card"]
        cid  = card.get("id")
        if not cid or not isinstance(cid, int):
            continue

        frame = card.get("frameType", "").lower()
        ctype = card.get("type", "").lower()

        # Skip Skill Cards and Tokens — not legal in main or extra deck
        if "skill" in ctype or "skill" in frame:
            continue
        if "token" in ctype or "token" in frame:
            continue

        if any(t in frame for t in EXTRA_DECK_FRAME_TYPES) or \
           any(t in ctype  for t in EXTRA_DECK_FRAME_TYPES):
            extra_deck.append(cid)
        else:
            main_deck.append(cid)

    main_deck  = list(dict.fromkeys(main_deck))
    extra_deck = list(dict.fromkeys(extra_deck))

    # Append staple IDs (skip any already present in main deck)
    staple_ids = []
    for card in staples:
        cid = card.get("id")
        if cid and isinstance(cid, int) and cid not in main_deck:
            staple_ids.append(cid)

    main_deck.extend(staple_ids)

    # Always append 3 copies of Polymerization
    POLYMERIZATION = {"id": 24094653, "name": "Polymerization"}
    main_deck.extend([POLYMERIZATION["id"]] * 3)

    if verbose_staples and staple_ids:
        print(f"\n  Staples added to main deck ({len(staple_ids)}):")
        for card in staples:
            if card.get("id") in staple_ids:
                print(f"    • [{card.get('id','?')}]  {card.get('name','Unknown')}")

    if verbose_staples:
        print(f"\n  Polymerization x3  [{POLYMERIZATION['id']}] added.")

    if verbose_deck:
        print(f"\n{'=' * 68}")
        print("  DECK LISTS")
        print(f"  {'-' * 55}")
        print(f"  Main  deck ({len(main_deck):>3} cards) : {main_deck}")
        print(f"  Extra deck ({len(extra_deck):>3} cards) : {extra_deck}")

    # Encode as compact JSON → base64 → URL-safe
    def encode(lst: list) -> str:
        raw     = json.dumps(lst, separators=(",", ":"))
        encoded = base64.b64encode(raw.encode()).decode()
        return urllib.parse.quote(encoded, safe="")

    url = (
        f"https://ygoprodeck.com/deckbuilder/"
        f"?main_deck={encode(main_deck)}"
        f"&extra_deck={encode(extra_deck)}"
    )

    if verbose_deck:
        print(f"\n  Deck Builder URL:")
        print(f"  {url}")
        print(f"{'=' * 68}\n")

    return url


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # ── Controls ──────────────────────────────────────────────────────────────
    SAMPLE_SIZE        = 20     # Number of random fusion cards to select
    STAPLES_COUNT      = 15     # Number of staple cards to add to main deck
    VERBOSE_LOAD       = False  # JSON loading / repair messages
    VERBOSE_INITIAL    = False  # Initial sample cards + their material lookups
    VERBOSE_RECURSIVE  = False  # Nested fusion resolution passes
    VERBOSE_FINAL_LIST = False  # Full categorised card list at the end
    VERBOSE_DECK       = True   # Deck lists and deckbuilder URL
    VERBOSE_STAPLES    = False  # Staple loading and sampling details
    # Note: STATS are always printed regardless of the above flags
    # ─────────────────────────────────────────────────────────────────────────

    print("Running...")

    data  = load_json_safe(FUSION_JSON, verbose=VERBOSE_LOAD)
    cards = data.get("data", data) if isinstance(data, dict) else data

    if VERBOSE_LOAD:
        print(f"Total cards loaded: {len(cards)}\n")

    sample = random.sample(cards, min(SAMPLE_SIZE, len(cards)))
    collected: dict = {}

    run_initial_pass(sample, collected, verbose=VERBOSE_INITIAL)

    resolved_ids: set = {
        entry["card"].get("id")
        for entry in collected.values()
        if "fusion selection" in entry["sources"]
    }

    iteration = run_recursive_passes(
        collected, resolved_ids, verbose=VERBOSE_RECURSIVE
    )

    resolve_exact_ids(collected, verbose=VERBOSE_LOAD)
    print_final_list(collected, iteration, verbose=VERBOSE_FINAL_LIST)
    staples = load_staples(STAPLES_JSON, STAPLES_COUNT, verbose=VERBOSE_STAPLES)
    build_deck_url(collected, staples, verbose_deck=VERBOSE_DECK, verbose_staples=VERBOSE_STAPLES)


if __name__ == "__main__":
    main()