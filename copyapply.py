"""Resolve 5etools `_copy` inheritance (port of DataUtil.generic.copyApplier in js/utils.js).

An entity with `_copy: {name, source, _templates, _mod, _preserve}` has no stats of its own; it
inherits from another entity, then gets template `_root` fields and `_mod` operations applied.
"""
import copy
import math
import re

PRESERVE_BASE = {"page", "otherSources", "referenceSources", "srd", "srd52", "basicRules", "basicRules2024",
                 "reprintedAs", "hasFluff", "hasFluffImages", "hasToken", "tokenCredit", "tokenCustom",
                 "foundryTokenScale", "altArt", "_versions"}
PRESERVE_MONSTER = {"legendaryGroup", "environment", "soundClip", "altArt", "variant", "dragonCastingColor", "familiar"}
ENTRY_PROPS = ["action", "bonus", "reaction", "trait", "legendary", "mythic", "variant", "spellcasting",
               "actionHeader", "bonusHeader", "reactionHeader", "legendaryHeader", "mythicHeader"]
SIZES = ["F", "D", "T", "S", "M", "L", "H", "G", "C"]
SKILL_ABIL = {"acrobatics": "dex", "animal handling": "wis", "arcana": "int", "athletics": "str",
              "deception": "cha", "history": "int", "insight": "wis", "intimidation": "cha",
              "investigation": "int", "medicine": "wis", "nature": "int", "perception": "wis",
              "performance": "cha", "persuasion": "cha", "religion": "int", "sleight of hand": "dex",
              "stealth": "dex", "survival": "wis"}
ABILS = ["str", "dex", "con", "int", "wis", "cha"]
TAG_SPLIT = re.compile(r"(\{@[^}]*\})")


def ability_mod(score):
    return math.floor((int(score) - 10) / 2)


def cr_number(cr):
    if isinstance(cr, dict):
        cr = cr.get("cr")
    if cr is None:
        return 0
    if isinstance(cr, (int, float)):
        return cr
    if "/" in str(cr):
        a, b = str(cr).split("/")
        return int(a) / int(b)
    try:
        return float(cr)
    except ValueError:
        return 0


def prof_bonus(cr):
    n = cr_number(cr)
    return 2 if n < 5 else math.ceil(n / 4) + 1


def short_name(m, title=False):
    name = m.get("name", "")
    short = m.get("shortName")
    prefix = "" if m.get("isNamedCreature") else ("The " if title else "the ")
    if short is True:
        return f"{prefix}{name}"
    if short:
        return f"{prefix}{short.title() if (not prefix and title) else short.lower()}"
    base = name.split(",")[0]
    base = re.sub(r"(?:adult|ancient|young) \w+ (dragon|dracolich)", r"\1", base, flags=re.I)
    out = base.split(" ")[0] if m.get("isNamedCreature") else base.lower()
    return f"{prefix}{out}"


def size_mult(ent):
    return {"L": 2, "H": 3, "G": 4}.get((ent.get("size") or ["M"])[0], 1)


def clean_math(expr):
    return re.sub(r"[^-+/*0-9.,]+", "", expr)


def resolve_vars(obj, ent):
    """Replace <$mode__detail$> placeholders inside any nested structure."""
    def sub(m):
        mode, _, detail = m.group(1).partition("__")
        if mode == "name":
            return ent.get("name", "")
        if mode == "short_name":
            return short_name(ent)
        if mode == "title_short_name":
            return short_name(ent, title=True)
        if mode in ("dc", "spell_dc"):
            return str(8 + ability_mod(ent.get(detail, 10)) + prof_bonus(ent.get("cr")))
        if mode == "to_hit":
            t = prof_bonus(ent.get("cr")) + ability_mod(ent.get(detail, 10))
            return f"+{t}" if t >= 0 else str(t)
        if mode == "damage_mod":
            t = ability_mod(ent.get(detail, 10))
            return "" if t == 0 else f" + {t}" if t > 0 else f" - {abs(t)}"
        if mode == "damage_avg":
            e = re.sub(r"\b(str|dex|con|int|wis|cha)\b", lambda a: str(ability_mod(ent.get(a.group(1), 10))), detail, flags=re.I)
            e = e.replace("size_mult", str(size_mult(ent)))
            return str(math.floor(eval(clean_math(e), {"__builtins__": {}})))  # noqa: S307 (sanitised)
        if mode == "size_mult":
            mult = size_mult(ent)
            return str(mult if not detail else math.floor(eval(f"{mult} * {clean_math(detail)}", {"__builtins__": {}})))
        return m.group(0)
    return walk_str(obj, lambda s: re.sub(r"<\$([^$]+)\$>", sub, s))


def walk_str(x, fn):
    if isinstance(x, str):
        return fn(x)
    if isinstance(x, list):
        return [walk_str(v, fn) for v in x]
    if isinstance(x, dict):
        return {k: walk_str(v, fn) for k, v in x.items()}
    return x


def get_path(obj, path):
    for p in path:
        if not isinstance(obj, dict) or p not in obj:
            return None
        obj = obj[p]
    return obj


def set_path(obj, path, val):
    for p in path[:-1]:
        obj = obj.setdefault(p, {})
    obj[path[-1]] = val


def as_list(x):
    return x if isinstance(x, list) else [x]


def find_index(arr, replace):
    if isinstance(replace, dict):
        if replace.get("regex"):
            re_ = re.compile(replace["regex"], re.I if "i" in replace.get("flags", "") else 0)
            return next((i for i, it in enumerate(arr) if re_.search(it.get("name", "") if isinstance(it, dict) else str(it))), -1)
        if replace.get("index") is not None:
            return replace["index"]
    return next((i for i, it in enumerate(arr) if (it.get("name") == replace if isinstance(it, dict) else it == replace)), -1)


class Failed(Exception):
    pass


def replace_text(val, mod):
    flags = re.I if "i" in mod.get("flags", "") else 0
    re_ = re.compile(mod["replace"], flags)
    repl = re.sub(r"\$(\d)", r"\\\1", mod["with"])  # JS $1 -> Python \1

    def on_str(s):
        if mod.get("tagInsensitive"):
            return re_.sub(repl, s)
        return "".join(p if p.startswith("{@") else re_.sub(repl, p) for p in TAG_SPLIT.split(s))
    return walk_str(val, on_str)


class Applier:
    def __init__(self, templates, preserve_extra=()):
        self.templates = templates
        self.preserve = PRESERVE_BASE | set(preserve_extra)

    # ---- individual mods -------------------------------------------------
    def mod(self, to, info, path, root):
        if isinstance(info, str):
            if info == "remove":
                if path and len(path) == 1:
                    to.pop(path[0], None)
                return
            raise Failed(info)
        mode = info["mode"]
        cur = get_path(to, path) if path else None
        if mode == "appendStr":
            set_path(to, path, f"{cur}{info.get('joiner', '')}{info['str']}" if cur else info["str"])
        elif mode == "replaceName":
            for ent in cur or []:
                if isinstance(ent, dict) and ent.get("name"):
                    ent["name"] = replace_text(ent["name"], info)
        elif mode == "replaceTxt":
            if not cur:
                return
            props = info.get("props", [None, "entries", "headerEntries", "footerEntries"])
            if None in props:
                cur[:] = [replace_text(it, info) if isinstance(it, str) else it for it in cur]
            for ent in cur:
                if isinstance(ent, dict):
                    for p in props:
                        if p is not None and p in ent:
                            ent[p] = replace_text(ent[p], info)
        elif mode == "prependArr":
            set_path(to, path, as_list(info["items"]) + (cur or []))
        elif mode == "appendArr":
            set_path(to, path, (cur or []) + as_list(info["items"]))
        elif mode == "appendIfNotExistsArr":
            items = as_list(info["items"])
            set_path(to, path, items if cur is None else cur + [i for i in items if i not in cur])
        elif mode in ("replaceArr", "replaceOrAppendArr"):
            if cur is None:
                if mode == "replaceOrAppendArr":
                    return set_path(to, path, as_list(info["items"]))
                raise Failed("no array to replace in")
            ix = find_index(cur, info["replace"])
            if ix >= 0:
                cur[ix:ix + 1] = as_list(info["items"])
            elif mode == "replaceOrAppendArr":
                cur.extend(as_list(info["items"]))
            else:
                raise Failed(f"could not find {info['replace']!r}")
        elif mode == "insertArr":
            if cur is None:
                raise Failed("no array to insert into")
            ix = info.get("index", -1)
            cur[(ix if ix >= 0 else len(cur)):(ix if ix >= 0 else len(cur))] = as_list(info["items"])
        elif mode == "removeArr":
            if cur is None:
                raise Failed("no array to remove from")
            for n in as_list(info.get("names", [])):
                ix = next((i for i, it in enumerate(cur) if isinstance(it, dict) and it.get("name") == n), -1)
                if ix >= 0:
                    del cur[ix]
                elif not info.get("force"):
                    raise Failed(f"could not find {n!r} to remove")
            for it in as_list(info.get("items", [])):
                if it in cur:
                    cur.remove(it)
        elif mode == "renameArr":
            for r in as_list(info["renames"]):
                for ent in cur or []:
                    if isinstance(ent, dict) and ent.get("name") == r["rename"]:
                        ent["name"] = r["with"]
        elif mode == "setProp":
            p = (info["prop"].split(".") if info.get("prop") else [])
            if path and path != ["*"]:
                p = list(path) + p
            set_path(to, p, copy.deepcopy(info["value"]))
        elif mode == "prefixSuffixStringProp":
            p = (info["prop"].split(".") if info.get("prop") else [])
            if path and path != ["*"]:
                p = list(path) + p
            s = get_path(to, p)
            if isinstance(s, str):
                set_path(to, p, f"{info.get('prefix', '')}{s}{info.get('suffix', '')}")
        elif mode in ("scalarAddProp", "scalarMultProp"):
            if not isinstance(cur, dict):
                return
            for k in (list(cur) if info["prop"] == "*" else [info["prop"]]):
                if k not in cur:
                    continue
                v = float(cur[k]) + info["scalar"] if mode == "scalarAddProp" else float(cur[k]) * info["scalar"]
                if info.get("floor"):
                    v = math.floor(v)
                v = int(v) if v == int(v) else v
                cur[k] = (f"+{v}" if v >= 0 else str(v)) if isinstance(cur[k], str) else v
        elif mode == "scalarAddHit":
            if cur:
                set_path(to, path, walk_str(cur, lambda s: re.sub(
                    r"\{@hit ([-+]?\d+)\}", lambda m: f"{{@hit {int(m.group(1)) + info['scalar']}}}", s)))
        elif mode == "scalarAddDc":
            if cur:
                set_path(to, path, walk_str(cur, lambda s: re.sub(
                    r"\{@dc (\d+)(?:\|[^}]+)?\}", lambda m: f"{{@dc {int(m.group(1)) + info['scalar']}}}", s)))
        elif mode == "calculateProp":
            tgt = to
            for p in path:
                tgt = tgt.setdefault(p, {})
            expr = info["formula"].replace("<$prof_bonus$>", str(prof_bonus(to.get("cr")))) \
                .replace("<$dex_mod$>", str(ability_mod(to.get("dex", 10))))
            tgt[info["prop"]] = math.floor(eval(clean_math(expr), {"__builtins__": {}}))  # noqa: S307 (sanitised)
        elif mode == "addSenses":
            senses = to.setdefault("senses", [])
            for s in as_list(info["senses"]):
                for i, ex in enumerate(senses):
                    m = re.search(rf"{s['type']} (\d+)", ex, re.I)
                    if m:
                        if int(m.group(1)) < s["range"]:
                            senses[i] = f"{s['type']} {s['range']} ft."
                        break
                else:
                    senses.append(f"{s['type']} {s['range']} ft.")
        elif mode in ("addSaves", "addSkills", "addAllSaves", "addAllSkills"):
            skills = mode.endswith("Skills")
            key, store = ("skill", "skill") if skills else ("save", "save")
            wanted = info.get("skills" if skills else "saves")
            if mode.startswith("addAll"):
                wanted = {k: wanted for k in (SKILL_ABIL if skills else ABILS)}
            tgt = to.setdefault(store, {})
            for k, level in wanted.items():
                abil = SKILL_ABIL[k] if skills else k
                total = level * prof_bonus(to.get("cr")) + ability_mod(to.get(abil, 10))
                txt = f"+{total}" if total >= 0 else str(total)
                if k not in tgt or int(str(tgt[k]).replace("+", "")) < total:
                    tgt[k] = txt
        elif mode == "maxSize":
            cur_ix = [SIZES.index(s) for s in to.get("size", [])]
            mx = SIZES.index(info["max"])
            nxt = [i for i in sorted(cur_ix) if i <= mx] or [mx]
            to["size"] = [SIZES[i] for i in nxt]
        elif mode == "scalarMultXp":
            pass  # xp isn't rendered
        elif mode in ("addSpells", "replaceSpells", "removeSpells"):
            self.spell_mod(to, info, mode)
        else:
            raise Failed(f"unhandled mode {mode}")

    @staticmethod
    def spell_mod(to, info, mode):
        sc_list = to.get("spellcasting")
        if not sc_list:
            raise Failed("no spellcasting")
        sc = next((s for s in sc_list if s.get("name") == info["name"]), None) if info.get("name") else sc_list[0]
        if sc is None:
            raise Failed("spellcasting trait not found")
        slots = sc.setdefault("spells", {})
        if mode == "addSpells":
            for k, v in (info.get("spells") or {}).items():
                if k not in slots:
                    slots[k] = v
                else:
                    for kk, vv in v.items():
                        if kk not in slots[k]:
                            slots[k][kk] = vv
                        elif isinstance(slots[k][kk], list):
                            slots[k][kk] = sorted(slots[k][kk] + vv, key=str.lower)
                        else:
                            slots[k][kk] = vv
            for p in ("constant", "will", "ritual"):
                for sp in info.get(p, []):
                    sc.setdefault(p, []).append(sp)
            for p in ("recharge", "legendary", "charges", "rest", "restLong", "daily", "weekly", "monthly", "yearly"):
                for k, spells in (info.get(p) or {}).items():
                    for sp in spells:
                        sc.setdefault(p, {}).setdefault(k, []).append(sp)
        elif mode == "replaceSpells":
            def rep(lst, meta):
                w = as_list(meta["with"])
                if meta["replace"] not in lst:
                    raise Failed(f"spell {meta['replace']!r} not found")
                i = lst.index(meta["replace"])
                lst[i:i + 1] = w
                lst.sort(key=str.lower)
            for k, metas in (info.get("spells") or {}).items():
                if k in slots:
                    for meta in metas:
                        rep(slots[k]["spells"], meta)
            for k, metas in (info.get("daily") or {}).items():
                for meta in metas:
                    rep(sc["daily"][k], meta)
        else:
            for k, rm in (info.get("spells") or {}).items():
                if slots.get(k, {}).get("spells"):
                    slots[k]["spells"] = [s for s in slots[k]["spells"] if s not in rm]
            for p in ("recharge", "legendary", "charges", "rest", "restLong", "daily", "weekly", "monthly", "yearly"):
                for k, rm in (info.get(p) or {}).items():
                    if sc.get(p, {}).get(k):
                        sc[p][k] = [s for s in sc[p][k] if s not in rm]

    # ---- copy ------------------------------------------------------------
    def apply(self, base, ent):
        """Return `ent` with `_copy` resolved against `base` (a fully resolved entity)."""
        to = copy.deepcopy(ent)
        meta = to.pop("_copy", {})
        mods = {k: as_list(v) for k, v in (meta.get("_mod") or {}).items()}
        tpls = []
        for t in meta.get("_templates") or []:
            tpl = next((x for x in self.templates if x["name"].lower().strip() == t["name"].lower().strip()
                        and x["source"].lower().strip() == t["source"].lower().strip()), None)
            if tpl is None:
                continue
            tpl = copy.deepcopy(tpl)
            tpls.append(tpl)
            for k, v in (tpl.get("apply", {}).get("_mod") or {}).items():
                mods.setdefault(k, []).extend(as_list(v))
        root_keys = set(to)
        pres = meta.get("_preserve") or {}
        for k, v in base.items():
            if to.get(k, ...) is None:
                del to[k]
            elif k not in to:
                if k in self.preserve:
                    if pres.get("*") or pres.get(k):
                        to[k] = copy.deepcopy(v)
                else:
                    to[k] = copy.deepcopy(v)
        for tpl in tpls:
            for k, v in (tpl.get("apply", {}).get("_root") or {}).items():
                if k not in root_keys:
                    to[k] = copy.deepcopy(v)
        mods = {k: resolve_vars(v, to) for k, v in mods.items()}
        for prop in sorted(mods, key=lambda p: (p in ("_", "*"), p == "*")):
            props = ENTRY_PROPS if prop == "*" else [None] if prop == "_" else [prop]
            for p in props:
                path = p.split(".") if p else None
                for info in mods[prop]:
                    try:
                        self.mod(to, info, path, to)
                    except (Failed, KeyError, TypeError, ValueError, IndexError):
                        pass  # best effort: keep whatever could be applied
        return to
