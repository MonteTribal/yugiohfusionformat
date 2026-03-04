# Created by MonteTribal
# Posted to: https://github.com/MonteTribal/yugiohfusionformat
# License: Creative Commons Zero v1.0 Universal

"""
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
    "Pendulum Effect Monster",
    "Pendulum Effect Ritual Monster",
    "Pendulum Flip Effect Monster",
    "Pendulum Normal Monster",
    "Pendulum Tuner Effect Monster",
    "Ritual Effect Monster",
    "Ritual Monster",
    "Spirit Monster",
    "Toon Monster",
    "Tuner Monster",
    "Union Effect Monster",
]

# Full main-deck type pool including Spell/Trap — used for GENERIC segments only
MAIN_DECK_ALL_TYPES = MAIN_DECK_MONSTER_TYPES + ["Spell Card", "Trap Card"]

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
        self.level      = None   # exact level (when no range qualifier)
        self.level_min  = None   # minimum level (e.g. "Level 5 or higher")
        self.level_max  = None   # maximum level (e.g. "Level 4 or lower")
        self.quantity   = 1
        self._parse_quantity()
        self._classify()

    def _parse_quantity(self):
        """Extract leading integer quantity, e.g. '3 Level 10 monsters' → quantity=3."""
        m = re.match(r'^(\d+)\s+', self.raw)
        if m:
            self.quantity = int(m.group(1))

    def _classify(self):
        quoted  = QUOTED_RE.findall(self.raw)
        outside = QUOTED_RE.sub("", self.raw).strip(" +,\r\n")

        if not quoted:
            self.kind = self.KIND_GENERIC
            self._extract_generic(self.raw)
        elif outside and re.search(r'\b(monsters?|cards?|fusion|synchro|xyz|link)\b',
                                    outside, re.IGNORECASE):
            self.kind      = self.KIND_ARCHETYPE
            self.archetype = quoted[0].strip()
            self._extract_generic(outside)
        else:
            self.kind = self.KIND_EXACT
            self.name = quoted[0].strip()

    def _extract_generic(self, text: str):
        self.level = None
        # Check for explicit type phrases BEFORE word-by-word extraction,
        # so "Normal Monster" sets extra_type rather than race=Normal.
        for t in EXTRA_DECK_TYPES | {"Normal", "Tuner", "Gemini", "Spirit", "Toon", "Union"}:
            pattern = re.compile(rf'\b{re.escape(t)}\s+Monster\b', re.IGNORECASE)
            if pattern.search(text):
                self.extra_type = t.capitalize()
                break
        for w in re.split(r'[\s\-/]+', text):
            w = w.strip(".,")
            if w.upper() in ATTRIBUTES:
                self.attribute = w.upper()
            # Don't set race to a word that was already consumed as extra_type
            if w.capitalize() in RACES and w.capitalize() != self.extra_type:
                self.race = w.capitalize()
            # Only set extra_type from EXTRA_DECK_TYPES if not already set above
            if self.extra_type is None and w.capitalize() in EXTRA_DECK_TYPES:
                self.extra_type = w.capitalize()
        # Extract level with optional range qualifier:
        # "Level 5 or higher" → level_min=5
        # "Level 4 or lower"  → level_max=4
        # "Level 5 or 6"      → pick randomly between 5 and 6 (treat as exact)
        # "Level 10"          → level=10 (exact)
        level_range = re.search(
            r'\bLevel\s+(\d+)(?:\s+or\s+(higher|lower|(\d+)))?',
            text, re.IGNORECASE
        )
        if level_range:
            base = int(level_range.group(1))
            qualifier = (level_range.group(2) or "").lower()
            alt_level  = level_range.group(3)
            if qualifier == "higher":
                self.level_min = base
            elif qualifier == "lower":
                self.level_max = base
            elif alt_level:
                # "Level 5 or 6" — pick randomly at resolve time
                self.level = random.choice([base, int(alt_level)])
            else:
                self.level = base

    def describe(self) -> str:
        qty = f"x{self.quantity} " if self.quantity > 1 else ""
        if self.kind == self.KIND_EXACT:
            return f'{qty}exact card: "{self.name}"'
        parts = []
        if self.kind == self.KIND_ARCHETYPE:
            parts.append(f'archetype="{self.archetype}"')
        if self.attribute:
            parts.append(f"attribute={self.attribute}")
        if self.race:
            parts.append(f"race={self.race}")
        if self.extra_type:
            parts.append(f"type={self.extra_type} Monster")
        if self.level:
            parts.append(f"level={self.level}")
        elif self.level_min is not None:
            parts.append(f"level>={self.level_min}")
        elif self.level_max is not None:
            parts.append(f"level<={self.level_max}")
        desc = "  |  ".join(parts) if parts else f'raw="{self.raw}"'
        return f"{qty}{desc}"


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

def api_query(params: dict, count: int = 3, verbose: bool = True) -> list[dict]:
    """Execute a parameterised API request; return a random sample of `count` results."""
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            all_results = data.get("data", [])
            return random.sample(all_results, min(count, len(all_results)))
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


def _resolve_level(seg: Segment) -> str | None:
    """Return a concrete level string for the API, handling exact / min / max."""
    if seg.level is not None:
        return str(seg.level)
    if seg.level_min is not None:
        return str(random.randint(seg.level_min, 12))
    if seg.level_max is not None:
        return str(random.randint(1, seg.level_max))
    return None


def search_segment(seg: Segment, verbose: bool = True) -> list[dict]:
    """Build API params from a Segment and return matching cards."""
    if seg.kind == Segment.KIND_EXACT:
        return []

    params: dict[str, str] = {}
    level_val = _resolve_level(seg)

    if seg.kind == Segment.KIND_ARCHETYPE:
        params["archetype"] = seg.archetype
        if seg.attribute:
            params["attribute"] = seg.attribute
        if seg.race:
            params["race"] = seg.race
        if level_val:
            params["level"] = level_val
            if verbose and (seg.level_min is not None or seg.level_max is not None):
                print(f"      [level] range resolved to level={level_val}")
        if seg.extra_type:
            params["type"] = f"{seg.extra_type} Monster"
        else:
            params["type"] = random.choice(MAIN_DECK_MONSTER_TYPES)
            if verbose:
                print(f"      [type] randomly selected: \"{params['type']}\"")
    elif seg.kind == Segment.KIND_GENERIC:
        if seg.attribute:
            params["attribute"] = seg.attribute
        if seg.race:
            params["race"] = seg.race
        if level_val:
            params["level"] = level_val
            if verbose and (seg.level_min is not None or seg.level_max is not None):
                print(f"      [level] range resolved to level={level_val}")
        if seg.extra_type:
            params["type"] = f"{seg.extra_type} Monster"
        else:
            params["type"] = random.choice(MAIN_DECK_ALL_TYPES)
            if verbose:
                print(f"      [type] randomly selected: \"{params['type']}\"")

    if not params:
        return []

    time.sleep(0.1)
    results = api_query(params, count=seg.quantity, verbose=verbose)

    # Fallback for ARCHETYPE queries that return nothing.
    if not results and seg.kind == Segment.KIND_ARCHETYPE:

        # Step 1: Retry with no type filter — the random type may have been too
        # restrictive for this archetype (e.g. no Toon Metalfoes exist).
        # Filter the results to monsters only so we never pick a Spell or Trap.
        retry_params = {k: v for k, v in params.items() if k != "type"}
        if retry_params:
            if verbose:
                print(f"      [archetype retry] retrying without type filter")
            time.sleep(0.1)
            # Fetch unfiltered, then keep only monsters before sampling
            url = f"{API_BASE}?{urllib.parse.urlencode(retry_params)}"
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = json.loads(resp.read().decode("utf-8")).get("data", [])
                monsters = [c for c in raw
                            if "monster" in c.get("type", "").lower()
                            and "token" not in c.get("type", "").lower()
                            and "skill" not in c.get("type", "").lower()]
                results = random.sample(monsters, min(seg.quantity, len(monsters))) if monsters else []
            except Exception:
                results = []

        # Step 2: If still nothing, the quoted name may be a name fragment rather
        # than a registered archetype (e.g. "Wingman"). Retry with fname= instead.
        if not results:
            fallback_params: dict[str, str] = {"fname": seg.archetype}
            if seg.extra_type:
                fallback_params["type"] = f"{seg.extra_type} Monster"
            if seg.attribute:
                fallback_params["attribute"] = seg.attribute
            if seg.race:
                fallback_params["race"] = seg.race
            if seg.level:
                fallback_params["level"] = str(seg.level)
            if verbose:
                print(f"      [fname fallback] retrying with fname=\"{seg.archetype}\"")
            time.sleep(0.1)
            results = api_query(fallback_params, count=seg.quantity, verbose=verbose)

    return results


# ── Registry helpers ──────────────────────────────────────────────────────────

def register(collected: dict, card: dict, source: str, qty: int = 1):
    """Add a card to the collected registry, tagged with its source.
    Extra calls for the same card increment its qty (copies needed in deck)."""
    cid = card.get("id", card.get("name", "unknown"))
    if cid not in collected:
        collected[cid] = {"card": card, "sources": set(), "qty": 0}
    collected[cid]["sources"].add(source)
    collected[cid]["qty"] += qty


def register_exact(collected: dict, name: str):
    """Register a named material placeholder. Each call increments qty by 1."""
    key = f"exact::{name}"
    if key not in collected:
        collected[key] = {"card": {"name": name, "type": "—", "atk": "—", "def": "—"}, "sources": set(), "qty": 0}
    collected[key]["sources"].add("exact material")
    collected[key]["qty"] += 1


def is_fusion(card: dict) -> bool:
    """Return True if the card is any variety of Fusion Monster."""
    return "fusion" in card.get("type", "").lower()


# ── Material resolution ───────────────────────────────────────────────────────

def resolve_materials(card: dict, collected: dict,
                      indent: str = "    ", verbose: bool = True):
    """
    Parse the first desc line of a card, search for its materials, register them.
    Also checks the full desc for 'Must be Special Summoned with' sentences and
    registers any quoted card names found there too.
    Called for the initial 20 cards and recursively for any fusion materials found.
    """
    desc       = card.get("desc", "")
    first_line = desc.split("\n")[0].strip()

    if verbose:
        print(f"{indent}Mat  : {first_line}")

    for seg in parse_segments(first_line):
        if verbose:
            print(f"{indent}▸ {seg.describe()}")

        if seg.kind == Segment.KIND_EXACT:
            for _ in range(seg.quantity):
                register_exact(collected, seg.name)
            if verbose:
                qty_str = f" x{seg.quantity}" if seg.quantity > 1 else ""
                print(f"{indent}  → (specific card{qty_str} — no lookup needed)")
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

    # ── 'Must be Special Summoned with' check ────────────────────────────────
    SUMMON_WITH_RE = re.compile(
        r'must (?:first )?be special summoned with (.+?)(?:\.|$)',
        re.IGNORECASE
    )
    for match in SUMMON_WITH_RE.finditer(desc):
        snippet  = match.group(0)
        names    = QUOTED_RE.findall(snippet)
        for name in names:
            if len(name) < 2 or ". " in name:
                continue
            register_exact(collected, name)
            if verbose:
                print(f"{indent}▸ summon-with card: \"{name}\"  → (registered)")


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


def resolve_exact_ids(collected: dict, verbose: bool = True) -> set:
    """
    For any exact-name placeholder (type == "—"), query the API by exact name
    and replace the placeholder with the full card data if found.
    Returns the set of newly-resolved IDs so the caller can decide whether
    to run another recursive pass.
    """
    placeholders = [
        (key, entry)
        for key, entry in list(collected.items())
        if entry["card"].get("type") == "—"
    ]

    if not placeholders:
        return set()

    if verbose:
        print(f"\n[Resolving {len(placeholders)} exact-name placeholder(s)...]")

    newly_resolved: set = set()

    for key, entry in placeholders:
        name = entry["card"].get("name", "")
        params = {"name": name}
        url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results = data.get("data", [])
            if results:
                full_card = results[0]
                new_id    = full_card.get("id")
                entry["card"] = full_card
                # Remove the string-keyed placeholder
                del collected[key]
                if new_id:
                    if new_id in collected:
                        # Card already in collected — merge sources and add qty
                        collected[new_id]["sources"].update(entry["sources"])
                        collected[new_id]["qty"] += entry.get("qty", 1)
                    else:
                        collected[new_id] = entry
                    newly_resolved.add(new_id)
                if verbose:
                    print(f"  ✓ {name:<45} → ID {new_id or '?'}")
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

    return newly_resolved


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
        qty      = entry.get("qty", 1)
        qty_str  = f" x{qty}" if qty > 1 else ""
        return (f"{indent}{cid_str:<12}  {c.get('name','Unknown'):<45}{qty_str}  [{c.get('type','—')}]"
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

        qty = entry.get("qty", 1)

        if any(t in frame for t in EXTRA_DECK_FRAME_TYPES) or \
           any(t in ctype  for t in EXTRA_DECK_FRAME_TYPES):
            # Extra deck: 1 copy per unique card regardless of how many times needed
            extra_deck.append(cid)
        else:
            # Main deck: repeat by qty so material requirements stack
            main_deck.extend([cid] * qty)

    # Deduplicate extra deck only; main deck keeps intentional repeats
    extra_deck = list(dict.fromkeys(extra_deck))

    # Append staple IDs (skip any already present in main deck)
    staple_ids = []
    for card in staples:
        cid = card.get("id")
        if cid and isinstance(cid, int) and cid not in main_deck:
            staple_ids.append(cid)

    main_deck.extend(staple_ids)

    # Always append 3 copies of Polymerization
    POLYMERIZATION       = {"id": 24094653, "name": "Polymerization"}
    FUTURE_FUSION        = {"id": 77565204, "name": "Future Fusion"}
    FUSION_CONSCRIPTION  = {"id": 17194258, "name": "Fusion Conscription"}
    FUSION_DEPLOYMENT    = {"id":  6498706, "name": "Fusion Deployment"}
    main_deck.extend([POLYMERIZATION["id"]]      * 3)
    main_deck.extend([FUTURE_FUSION["id"]]       * 3)
    main_deck.append(FUSION_CONSCRIPTION["id"])
    main_deck.append(FUSION_DEPLOYMENT["id"])

    if verbose_staples and staple_ids:
        print(f"\n  Staples added to main deck ({len(staple_ids)}):")
        for card in staples:
            if card.get("id") in staple_ids:
                print(f"    • [{card.get('id','?')}]  {card.get('name','Unknown')}")

    if verbose_staples:
        print(f"\n  Polymerization      x3  [{POLYMERIZATION['id']}] added.")
        print(f"  Future Fusion       x3  [{FUTURE_FUSION['id']}] added.")
        print(f"  Fusion Conscription x1  [{FUSION_CONSCRIPTION['id']}] added.")
        print(f"  Fusion Deployment   x1  [{FUSION_DEPLOYMENT['id']}] added.")

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

    # Interleaved loop: resolve exact-name placeholders → recursive pass → repeat.
    # Each cycle may expose new fusions (whose materials need resolving) or new
    # exact-name placeholders (whose IDs need looking up). We stop when a full
    # cycle produces no new resolutions and no new recursive passes.
    while True:
        newly_resolved = resolve_exact_ids(collected, verbose=VERBOSE_LOAD)
        if not newly_resolved:
            break

        # Allow re-resolution of newly-promoted fusions
        resolved_ids -= newly_resolved
        extra = run_recursive_passes(
            collected, resolved_ids, verbose=VERBOSE_RECURSIVE
        )
        iteration += extra

        if extra == 0:
            # No new fusions to expand — but there may still be new exact::
            # placeholders added by the recursive pass above, so loop once more
            # to catch them.  If resolve_exact_ids returns empty next time, we stop.
            pass

    print_final_list(collected, iteration, verbose=VERBOSE_FINAL_LIST)
    staples = load_staples(STAPLES_JSON, STAPLES_COUNT, verbose=VERBOSE_STAPLES)
    build_deck_url(collected, staples, verbose_deck=VERBOSE_DECK, verbose_staples=VERBOSE_STAPLES)


if __name__ == "__main__":
    main()