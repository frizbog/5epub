#!/usr/bin/env python3
"""Convert a single 5etools book or adventure into its own EPUB.

Usage:
  5etools2epub.py --list              # repo defaults to ../5etools-src
  5etools2epub.py PHB
  5etools2epub.py --repo ~/Documents/GitHub/5etools-src LMoP -o out/

Images are downloaded from the 5etools image host (--img-root) and embedded in
the EPUB. The resulting book has no remote resources required for rendering.
"""
import argparse
import glob
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent))
import copyapply  # noqa: E402
from json2epub import Book, IMAGE_CACHE_DIR, clear_image_cache, slug  # noqa: E402

SITE = "https://5e.tools/"
FONT_DIR = Path(__file__).parent / "fonts"
SIZES = {"T": "Tiny", "S": "Small", "M": "Medium", "L": "Large", "H": "Huge", "G": "Gargantuan", "V": "Varies"}
ALIGN = {"L": "lawful", "N": "neutral", "NX": "neutral", "NY": "neutral", "C": "chaotic", "G": "good",
         "E": "evil", "U": "unaligned", "A": "any alignment"}
ATK = {"mw": "Melee Weapon Attack:", "rw": "Ranged Weapon Attack:", "mw,rw": "Melee or Ranged Weapon Attack:",
       "ms": "Melee Spell Attack:", "rs": "Ranged Spell Attack:", "ms,rs": "Melee or Ranged Spell Attack:",
       "m": "Melee Attack:", "r": "Ranged Attack:", "m,r": "Melee or Ranged Attack:"}
ABIL = {"str": "Strength", "dex": "Dexterity", "con": "Constitution", "int": "Intelligence",
        "wis": "Wisdom", "cha": "Charisma"}
PAGES = {"creature": "bestiary.html", "spell": "spells.html", "item": "items.html", "condition": "conditionsdiseases.html",
         "disease": "conditionsdiseases.html", "status": "conditionsdiseases.html", "background": "backgrounds.html",
         "race": "races.html", "feat": "feats.html", "optfeature": "optionalfeatures.html", "action": "actions.html",
         "deity": "deities.html", "variantrule": "variantrules.html", "hazard": "trapshazards.html",
         "trap": "trapshazards.html", "object": "objects.html", "vehicle": "vehicles.html", "reward": "rewards.html",
         "language": "languages.html", "class": "classes.html", "recipe": "recipes.html", "table": "tables.html",
         "facility": "bastions.html", "charoption": "charcreationoptions.html", "deck": "decks.html",
         "psionic": "psionics.html", "cult": "cultsboons.html", "boon": "cultsboons.html"}
# tag -> [(file glob under data/, list key)]
LOOKUP = {
    "creature": [("bestiary/bestiary-*.json", "monster")], "spell": [("spells/spells-*.json", "spell")],
    "item": [("items.json", "item"), ("items-base.json", "baseitem")], "feat": [("feats.json", "feat")],
    "background": [("backgrounds.json", "background")], "race": [("races.json", "race")],
    "variantrule": [("variantrules.json", "variantrule")], "action": [("actions.json", "action")],
    "condition": [("conditionsdiseases.json", "condition")], "disease": [("conditionsdiseases.json", "disease")],
    "status": [("conditionsdiseases.json", "status")], "optfeature": [("optionalfeatures.json", "optionalfeature")],
    "reward": [("rewards.json", "reward")], "hazard": [("trapshazards.json", "hazard")],
    "trap": [("trapshazards.json", "trap")], "object": [("objects.json", "object")],
    "vehicle": [("vehicles.json", "vehicle")], "vehupgrade": [("vehicles.json", "vehicleUpgrade")],
    "deity": [("deities.json", "deity")], "facility": [("bastions.json", "facility")],
    "charoption": [("charcreationoptions.json", "charoption")], "language": [("languages.json", "language")],
    "sense": [("senses.json", "sense")], "recipe": [("recipes.json", "recipe")], "table": [("tables.json", "table")],
    "deck": [("decks.json", "deck")], "cult": [("cultsboons.json", "cult")], "boon": [("cultsboons.json", "boon")],
}


def esc(s):
    return html.escape(str(s), quote=False)


def sign(n):
    return f"+{n}" if int(n) >= 0 else str(n)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read 5etools data file {path}: {exc}") from exc


class Lookup:
    def __init__(self, data_dir):
        self.dir, self.cache = data_dir, {}

    def _templates(self):
        path = self.dir / "bestiary" / "template.json"
        if not path.exists():
            return []
        return read_json(path).get("monsterTemplate", [])

    def _load(self, tag):
        idx, ents = {}, []
        for pat, key in LOOKUP.get(tag, []):
            for f in sorted(glob.glob(str(self.dir / pat))):
                ents += read_json(f).get(key, [])
        raw = {(e.get("name", "").lower(), e.get("source", "").lower()): e for e in ents}
        applier = copyapply.Applier(self._templates() if tag == "creature" else [],
                                    copyapply.PRESERVE_MONSTER if tag == "creature" else ())
        done = {}

        def resolve(e, depth=0):
            k = (e.get("name", "").lower(), e.get("source", "").lower())
            if k in done:
                return done[k]
            c = e.get("_copy")
            if not c or depth > 20:
                done[k] = e
                return e
            base = raw.get((c["name"].lower(), c["source"].lower()))
            if base is None:
                done[k] = {kk: v for kk, v in e.items() if kk != "_copy"}
                return done[k]
            done[k] = applier.apply(resolve(base, depth + 1), e)
            return done[k]
        for e in ents:
            r = resolve(e)
            n = e.get("name", "").lower()
            idx[(n, e.get("source", "").lower())] = r
            idx.setdefault((n, None), r)
        self.cache[tag] = idx
        return idx

    def get(self, tag, name, source=None):
        if tag not in LOOKUP:
            return None
        idx = self.cache.get(tag) or self._load(tag)
        n = name.lower()
        return idx.get((n, source.lower())) if source else idx.get((n, None))


class Renderer:
    def __init__(self, book_id, chapter_files, lookup, img_root):
        self.id, self.chapters, self.lookup, self.img_root = book_id.lower(), chapter_files, lookup, img_root
        self.headings = []  # (normalized name, anchor id, name) for the chapter being rendered
        self.counter = 0
        self.area_map = {}  # entry id -> chapter index
        self.sb_map = {}  # (tag, normalized name) -> (chapter index, anchor)
        self.sb_anchor = {}  # id(statblock node) -> anchor
        self.mentions = {}  # (tag, normalized name) -> {"name": str, "locs": {(file, anchor): label}}
        self.references = {}  # (tag, normalized name, source) -> bundled reference metadata
        self.reference_terms = {}  # (tag, normalized name) -> first bundled reference
        self.emitted = set()
        self.cur_file, self.cur_heading, self.cur_sb = "", ("", ""), None

    def prescan(self, sections):
        """Assign anchors to statblocks and map entry ids to chapters, before anything is rendered."""
        def walk(x, idx):
            if isinstance(x, dict):
                if x.get("id") is not None:
                    self.area_map.setdefault(str(x["id"]), idx)
                if x.get("type") == "statblock" and x.get("tag") and not str(x.get("prop", "")).endswith("Fluff"):
                    key = (x["tag"], self.norm(x.get("name", "")))
                    anchor = f"sb-{x['tag']}-{slug(key[1])}"
                    if key in self.sb_map:
                        anchor += f"-{len(self.sb_anchor)}"
                    else:
                        self.sb_map[key] = (idx, anchor)
                    self.sb_anchor[id(x)] = anchor
                for v in x.values():
                    walk(v, idx)
            elif isinstance(x, list):
                for v in x:
                    walk(v, idx)
        for i, s in enumerate(sections):
            walk(s, i)

    def link_target(self, tag, name):
        """Href for an in-book statblock, recording the mention for backlinks/index."""
        key = (tag, self.norm(name))
        m = self.mentions.setdefault(key, {"name": name, "locs": {}})
        if self.cur_sb == key:
            return None
        anchor, title = self.cur_heading
        hit = self.sb_map.get(key)
        if not (hit and not anchor and self.chapters[hit[0]] == self.cur_file):
            m["locs"].setdefault((self.cur_file, anchor), title)
        return f"{self.chapters[hit[0]]}.xhtml#{hit[1]}" if hit else None

    # ---- inline markup -------------------------------------------------
    def bundle_reference(self, tag, name, source=None):
        """Collect a referenced entity for the generated References appendix."""
        if tag not in LOOKUP:
            return None
        source = source or {"creature": "MM", "spell": "PHB", "item": "DMG"}.get(tag)
        key = (tag, self.norm(name), (source or "").lower())
        if key in self.references:
            return self.references[key]["anchor"]
        record = self.lookup.get(tag, name, source)
        if record is None:
            return None
        anchor = f"ref-{tag}-{slug(self.norm(name))}"
        used = {v["anchor"] for v in self.references.values()}
        if anchor in used:
            anchor += f"-{slug(source or 'default')}"
        meta = {"tag": tag, "name": record.get("name", name), "source": source,
                "record": record, "anchor": anchor, "html": None}
        self.references[key] = meta
        self.reference_terms.setdefault((tag, self.norm(name)), meta)
        return anchor

    def tag(self, t, body):
        p = [x.strip() for x in body.split("|")]
        first = p[0]
        if t in ("b", "bold"):
            return f"<b>{first}</b>"
        if t in ("i", "italic", "note", "tip", "footnote"):
            return f"<i>{first}</i>"
        if t in ("u", "underline"):
            return f"<u>{first}</u>"
        if t in ("s", "strike"):
            return f"<s>{first}</s>"
        if t == "sup":
            return f"<sup>{first}</sup>"
        if t in ("h",):
            return "<i>Hit:</i> "
        if t == "atk":
            return f"<i>{ATK.get(first, first)}</i>"
        if t == "hit":
            return sign(first) if re.fullmatch(r"-?\d+", first) else first
        if t == "dc":
            return f"DC {first}"
        if t in ("dice", "damage", "autodice"):
            return p[1] if len(p) > 1 and p[1] else first
        if t == "d20":
            return sign(first) if re.fullmatch(r"-?\d+", first) else first
        if t == "chance":
            return p[1] if len(p) > 1 and p[1] else f"{first} percent"
        if t == "recharge":
            return f"(Recharge {first}\u20136)" if first else "(Recharge 6)"
        if t in ("scaledice", "scaledamage"):
            return p[-1]
        if t == "skillCheck":
            m = first.split()
            return f"{ABIL.get(m[0], m[0])} check" if len(m) < 2 else first
        if t == "savingThrow":
            return first
        if t == "ability":
            return first
        if t in ("adventure", "book"):
            text, bid = first, (p[1] if len(p) > 1 else "").lower()
            if bid == self.id and len(p) > 2 and p[2].isdigit() and int(p[2]) < len(self.chapters):
                return f'<a href="#{self.chapters[int(p[2])]}">{text}</a>'
            if bid == self.id:
                return text
            return text
        if t == "link":
            return f'<a href="{html.escape(p[1], quote=True)}">{first}</a>' if len(p) > 1 else first
        if t in ("footnote",):
            return f"<i>{first}</i>"
        display = p[2] if len(p) > 2 and p[2] else first
        if t == "area":
            idx = self.area_map.get(p[1]) if len(p) > 1 else None
            return f'<a href="{self.chapters[idx]}.xhtml#i{slug(p[1])}">{first}</a>' if idx is not None else first
        if t in LOOKUP or t in PAGES:
            local = self.link_target(t, first)
            if local:
                return f'<a href="{local}">{display}</a>'
            if self.cur_sb == (t, self.norm(first)):
                return display
            source = p[1] if len(p) > 1 and p[1] else None
            anchor = self.bundle_reference(t, first, source)
            if anchor:
                return f'<a href="references.xhtml#{anchor}">{display}</a>'
        if t in PAGES:
            return display
        if t in ("filter", "5etools", "5etoolsImg", "area", "color", "font", "highlight", "comic", "comicH1",
                 "comicH2", "comicH3", "comicH4", "comicNote", "quickref", "skill", "sense", "classFeature",
                 "subclassFeature", "itemProperty", "itemMastery", "card", "psionic", "legroup", "hom"):
            if t.startswith("comicH"):
                return f"<b>{first}</b>"
            if t in ("skill", "sense", "quickref", "classFeature", "subclassFeature", "itemProperty", "itemMastery"):
                return display if t not in ("quickref",) else (p[3] if len(p) > 3 and p[3] else first)
            return first
        return display

    def inline(self, s):
        s = esc(s)
        pat = re.compile(r"\{@(\w+)(?: ([^{}]*))?\}")
        for _ in range(12):
            n = pat.sub(lambda m: self.tag(m.group(1), m.group(2) or ""), s)
            if n == s:
                break
            s = n
        return s

    # ---- block rendering -----------------------------------------------
    def img_url(self, href):
        if not isinstance(href, dict):
            return None
        if href.get("type") == "external":
            return href.get("url")
        path = href.get("path")
        return self.img_root + quote(path) if path else None

    def entries(self, items, depth=1):
        if items is None:
            return ""
        if isinstance(items, (str, int, float)):
            items = [items]
        return "".join(self.entry(e, depth) for e in items)

    @staticmethod
    def norm(name):
        plain = re.sub(r"\{@\w+ ([^|}]*)[^}]*\}", r"\1", name)
        return re.sub(r"[\W_]+", " ", plain).strip().lower()

    def heading(self, name, depth, ident=None):
        n = min(max(depth + 1, 2), 6)
        self.counter += 1
        anchor = f"i{slug(ident)}" if ident is not None and f"i{slug(ident)}" not in self.emitted else f"h{self.counter}"
        self.emitted.add(anchor)
        plain = re.sub(r"\{@\w+ ([^|}]*)[^}]*\}", r"\1", name)
        self.headings.append((self.norm(name), anchor, name))
        self.cur_heading = (anchor, plain)
        return f'<h{n} id="{anchor}">{self.inline(name)}</h{n}>'

    def entry(self, e, depth=1):
        out = self._entry(e, depth)
        if isinstance(e, dict) and e.get("id") is not None:
            anchor = f"i{slug(e['id'])}"
            if anchor not in self.emitted:
                self.emitted.add(anchor)
                out = f'<span id="{anchor}"></span>' + out
        return out

    def _entry(self, e, depth=1):
        if isinstance(e, (str, int, float)):
            return f"<p>{self.inline(e)}</p>"
        if not isinstance(e, dict):
            return ""
        t = e.get("type", "entries")
        name = e.get("name")
        if t in ("entries", "section", "flowBlock", "variantInner", "variant", "inlineBlock", "optfeature"):
            h = self.heading(name, depth, e.get("id")) if name else ""
            return h + self.entries(e.get("entries"), depth + 1)
        if t in ("inset", "insetReadaloud"):
            cls = "readaloud" if t == "insetReadaloud" else "inset"
            h = f"<p><b>{self.inline(name)}</b></p>" if name else ""
            return f'<aside class="{cls}">{h}{self.entries(e.get("entries"), depth + 1)}</aside>'
        if t == "quote":
            by = f"<p>\u2014 {self.inline(e['by'])}{', ' + self.inline(e['from']) if e.get('from') else ''}</p>" if e.get("by") else ""
            return f"<blockquote>{self.entries(e.get('entries'), depth + 1)}{by}</blockquote>"
        if t == "list":
            items = "".join(self.list_item(i, depth) for i in e.get("items", []))
            return f"<ul>{items}</ul>"
        if t == "table":
            return self.table(e)
        if t == "tableGroup":
            return "".join(self.entry(x, depth) for x in e.get("tables", []))
        if t == "image":
            return self.image(e)
        if t == "gallery":
            return "".join(self.entry(i, depth) for i in e.get("images", []))
        if t == "item":
            return self.list_item(e, depth)
        if t == "hr":
            return "<hr/>"
        if t == "wrapper":
            return self.entries(e.get("wrappeds"), depth)
        if t == "statblock":
            return self.statblock(e, depth)
        if t in ("abilityGeneric", "abilityDc", "abilityAttackMod"):
            body = e.get("entry") or ", ".join(ABIL.get(a, a) for a in e.get("attributes", []))
            return f"<p><b>{self.inline(name or '')}</b> {self.inline(body)}</p>"
        if t == "flowchart":
            return self.entries([b for b in e.get("blocks", [])], depth)
        if "entries" in e:
            return self.entries(e["entries"], depth + 1)
        if "entry" in e:
            return self.entry(e["entry"], depth)
        return ""

    def list_item(self, i, depth):
        if isinstance(i, str):
            return f"<li>{self.inline(i)}</li>"
        if isinstance(i, dict):
            body = i.get("entry") if "entry" in i else i.get("entries")
            inner = self.entries(body, depth + 1) if isinstance(body, list) else (
                self.inline(body) if isinstance(body, str) else "")
            nm = f"<b>{self.inline(i['name'])}</b> " if i.get("name") else ""
            if isinstance(body, str) or body is None:
                return f"<li>{nm}{inner}</li>"
            return f"<li>{nm}{inner}</li>"
        return ""

    def cell(self, c):
        if isinstance(c, dict):
            roll = c.get("roll")
            if roll:
                lo, hi = roll.get("min"), roll.get("max")
                txt = str(roll["exact"]) if "exact" in roll else (f"{lo}" if lo == hi else f"{lo}\u2013{hi}")
                return esc(txt)
            return self.entries([c["entry"]], 1).replace("<p>", "").replace("</p>", "<br/>") if "entry" in c else \
                self.entries([c], 1)
        return self.inline(c)

    def table(self, e):
        out = ["<table>"]
        if e.get("caption"):
            out.append(f"<caption>{self.inline(e['caption'])}</caption>")
        labels = e.get("colLabels")
        label_rows = e.get("colLabelRows") or ([labels] if labels else [])
        for r in label_rows:
            out.append("<thead><tr>" + "".join(f"<th>{self.inline(x)}</th>" for x in r) + "</tr></thead>")
        out.append("<tbody>")
        for r in e.get("rows", []):
            cells = r.get("row", []) if isinstance(r, dict) else r
            out.append("<tr>" + "".join(f"<td>{self.cell(c)}</td>" for c in cells) + "</tr>")
        out.append("</tbody></table>")
        for f in e.get("footnotes", []) or []:
            out.append(f"<p><i>{self.inline(f) if isinstance(f, str) else ''}</i></p>")
        return "".join(out)

    def plain(self, s):
        """Inline-rendered text with all markup stripped (for attributes)."""
        return html.unescape(re.sub(r"<[^>]+>", "", self.inline(s)))

    def image(self, e):
        u = self.img_url(e.get("href"))
        if not u:
            return ""
        alt = html.escape(self.plain(e.get("altText") or e.get("title") or ""), quote=True)
        cap = ""
        if e.get("title") or e.get("credit"):
            cap = "<figcaption>" + self.inline(e.get("title", "")) + (
                f" <i>({self.inline(e['credit'])})</i>" if e.get("credit") else "") + "</figcaption>"
        return f'<figure><img src="{html.escape(u, quote=True)}" alt="{alt}"/>{cap}</figure>'

    # ---- statblocks ------------------------------------------------------
    def statblock(self, e, depth):
        tag, name = e.get("tag"), e.get("name", "")
        if e.get("prop") and not tag:
            return ""
        if e.get("prop", "").endswith("Fluff"):
            return ""
        anchor = self.sb_anchor.get(id(e), "")
        aid = f' id="{anchor}"' if anchor and anchor not in self.emitted else ""
        self.emitted.add(anchor)
        prev, self.cur_sb = self.cur_sb, (tag, self.norm(name))
        try:
            rec = self.lookup.get(tag, name, e.get("source"))
            if not rec:
                return f'<aside class="stat"{aid}><p><b>{esc(name)}</b></p>@@BL:{tag}:{self.norm(name)}@@</aside>'
            if tag == "creature":
                body = self.creature(rec)
            elif tag == "spell":
                body = self.spell(rec)
            else:
                nm = rec.get("name", name)
                body = f"<p><b>{esc(nm)}</b></p>" + self.entries(rec.get("entries") or rec.get("entry"), depth + 1)
        finally:
            key = self.cur_sb
            self.cur_sb = prev
        return f'<aside class="stat"{aid}>{body}@@BL:{key[0]}:{key[1]}@@</aside>'

    def backlinks(self, tag, key, limit=8):
        locs = self.mentions.get((tag, key), {}).get("locs", {})
        if not locs:
            return ""
        links = [f'<a href="{f}.xhtml#{a}">{esc(title)}</a>' if a else f'<a href="{f}.xhtml">{esc(title)}</a>'
                 for (f, a), title in locs.items()]
        more = f" and {len(links) - limit} more" if len(links) > limit else ""
        return f'<p class="mentions"><i>Mentioned in:</i> {", ".join(links[:limit])}{more}</p>'

    def index_entries(self, _unused=None):
        out = []
        groups = {"creature": "Creatures", "spell": "Spells", "item": "Items"}
        for (tag, key), m in self.mentions.items():
            if tag not in groups:
                continue
            hit = self.sb_map.get((tag, key))
            ref = self.reference_terms.get((tag, key))
            targets = [{"to": f"{f}#{a}" if a else f, "label": title} for (f, a), title in m["locs"].items()][:12]
            if not targets and not hit:
                continue
            name = m["name"][:1].upper() + m["name"][1:]
            out.append({"term": name, "group": groups[tag], "targets": targets,
                        **({"term_to": f"{self.chapters[hit[0]]}#{hit[1]}"} if hit else
                           {"term_to": f"references#{ref['anchor']}"} if ref else {})})
        return out

    def references_appendix(self):
        """Render the transitive closure of bundled entity references."""
        rendered = set()
        while True:
            pending = [(key, meta) for key, meta in self.references.items() if key not in rendered]
            if not pending:
                break
            for key, meta in pending:
                rendered.add(key)
                prev_file, prev_heading, prev_sb = self.cur_file, self.cur_heading, self.cur_sb
                self.cur_file = "references"
                self.cur_heading = (meta["anchor"], meta["name"])
                self.cur_sb = (meta["tag"], self.norm(meta["name"]))
                record = meta["record"]
                try:
                    if meta["tag"] == "creature":
                        body = self.creature(record)
                    elif meta["tag"] == "spell":
                        body = self.spell(record)
                    else:
                        body = (f"<h3>{esc(meta['name'])}</h3>" +
                                self.entries(record.get("entries") or record.get("entry"), 3))
                finally:
                    self.cur_file, self.cur_heading, self.cur_sb = prev_file, prev_heading, prev_sb
                meta["html"] = (f'<section id="{meta["anchor"]}">{body}'
                                f'@@BL:{meta["tag"]}:{self.norm(meta["name"])}@@</section>')
        if not self.references:
            return None
        labels = {"creature": "Creatures", "spell": "Spells", "item": "Items"}
        groups = {}
        for meta in self.references.values():
            groups.setdefault(meta["tag"], []).append(meta)
        out = []
        for tag, metas in groups.items():
            out.append(f"<h2>{esc(labels.get(tag, tag.replace('-', ' ').title()))}</h2>")
            out.extend(meta["html"] for meta in sorted(metas, key=lambda m: m["name"].lower()))
        content = "".join(out)
        return re.sub(r"@@BL:(\w+):([^@]*)@@", lambda m: self.backlinks(m.group(1), m.group(2)), content)

    @staticmethod
    def speed(s):
        if isinstance(s, (int, str)):
            return f"{s} ft."
        parts = []
        for k, v in s.items():
            if k in ("canHover", "alternate"):
                continue
            v = v.get("number") if isinstance(v, dict) else v
            if v is True:
                continue
            parts.append(f"{v} ft." if k == "walk" else f"{k} {v} ft." + (" (hover)" if k == "fly" and s.get("canHover") else ""))
        return ", ".join(parts)

    def creature(self, m):
        t = m.get("type")
        ty = t if isinstance(t, str) else (t or {}).get("type", "")
        ty = ty if isinstance(ty, str) else "creature"
        if isinstance(t, dict) and t.get("tags"):
            ty += " (" + ", ".join(x if isinstance(x, str) else x.get("tag", "") for x in t["tags"]) + ")"
        size = " or ".join(SIZES.get(s, s) for s in m.get("size", []))
        al = m.get("alignment", [])
        al = " ".join(ALIGN.get(a, str(a)) for a in al if isinstance(a, str)) if isinstance(al, list) else ""
        ac = []
        for a in m.get("ac", []):
            if isinstance(a, dict):
                ac.append(f"{a.get('ac', '')}" + (f" ({', '.join(a['from'])})" if a.get("from") else "") +
                          (f" {a['condition']}" if a.get("condition") else "") if "ac" in a else a.get("special", ""))
            else:
                ac.append(str(a))
        hp = m.get("hp") or {}
        hps = hp.get("special") or (f"{hp['average']} ({hp['formula']})" if "average" in hp and "formula" in hp
                                     else str(hp.get("average", "")))
        stats = []
        if ac:
            stats.append(f"<b>Armor Class</b> {self.inline(', '.join(ac))}")
        if hps:
            stats.append(f"<b>Hit Points</b> {self.inline(hps)}")
        if m.get("speed"):
            stats.append(f"<b>Speed</b> {esc(self.speed(m['speed']))}")
        o = [f"<h3>{esc(m['name'])}</h3>", f"<p><i>{esc(size)} {esc(ty)}{', ' + esc(al) if al else ''}</i></p>"]
        if stats:
            o.append("<p>" + "<br/>".join(stats) + "</p>")
        abil = [k for k in ABIL if k in m]
        if abil:
            o.append("<table><thead><tr>" + "".join(f"<th>{k.upper()}</th>" for k in abil) + "</tr></thead><tbody><tr>" +
                     "".join(f"<td>{m[k]} ({sign((m[k] - 10) // 2)})</td>" if isinstance(m[k], int) else "<td>\u2014</td>"
                             for k in abil) + "</tr></tbody></table>")
        lines = []

        def kv(label, val):
            if val:
                lines.append(f"<b>{label}</b> {self.inline(val)}")
        kv("Saving Throws", ", ".join(f"{k.title()} {v}" for k, v in m.get("save", {}).items()))
        kv("Skills", ", ".join(f"{k.title()} {v}" for k, v in m.get("skill", {}).items()))
        for k, label in (("vulnerable", "Damage Vulnerabilities"), ("resist", "Damage Resistances"),
                         ("immune", "Damage Immunities"), ("conditionImmune", "Condition Immunities")):
            v = m.get(k)
            if v:
                kv(label, "; ".join(x if isinstance(x, str) else ", ".join(
                    y for y in x.get(k, x.get("immune", x.get("resist", []))) if isinstance(y, str)) for x in v))
        senses = list(m.get("senses") or []) + [f"passive Perception {m['passive']}"] if m.get("passive") else m.get("senses") or []
        kv("Senses", ", ".join(senses))
        kv("Languages", ", ".join(m.get("languages") or []) or ("\u2014" if "str" in m else ""))
        cr = m.get("cr")
        cr = cr.get("cr") if isinstance(cr, dict) else cr
        kv("Challenge", str(cr) if cr is not None else "")
        if lines:
            o.append("<p>" + "<br/>".join(lines) + "</p>")
        for key, title in (("trait", ""), ("action", "Actions"), ("bonus", "Bonus Actions"),
                           ("reaction", "Reactions"), ("legendary", "Legendary Actions")):
            blocks = m.get(key)
            if not blocks:
                continue
            if title:
                o.append(f"<h4>{title}</h4>")
            for b in blocks:
                if isinstance(b, dict):
                    body = self.entries(b.get("entries"), 5)
                    nm = f"<b><i>{self.inline(b['name'])}.</i></b> " if b.get("name") else ""
                    o.append(re.sub(r"^<p>", "<p>" + nm, body, count=1) if body.startswith("<p>") else f"<p>{nm}</p>{body}")
        return "".join(o)

    def spell(self, s):
        lvl = s.get("level", 0)
        sch = {"A": "abjuration", "C": "conjuration", "D": "divination", "E": "enchantment", "V": "evocation",
               "I": "illusion", "N": "necromancy", "T": "transmutation", "P": "psionic"}.get(s.get("school"), "")
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(lvl, "th")
        kind = f"{sch} cantrip" if lvl == 0 else f"{lvl}{suffix}-level {sch}"
        time = ", ".join(f"{t.get('number', '')} {t.get('unit', '')}".strip() for t in s.get("time", []))
        r = s.get("range", {})
        d = r.get("distance", {})
        rng = r.get("type", "") if not d else (d.get("type", "") if "amount" not in d else f"{d['amount']} {d['type']}")
        if r.get("type") == "point" and d.get("type") in ("self", "touch", "sight", "unlimited"):
            rng = d["type"].title()
        comp = s.get("components", {})
        cl = [c.upper() for c in ("v", "s") if comp.get(c)]
        if comp.get("m"):
            mm = comp["m"]
            cl.append("M (" + (mm if isinstance(mm, str) else mm.get("text", "")) + ")")
        dur = []
        for x in s.get("duration", []):
            if x.get("type") == "timed":
                dd = x.get("duration", {})
                dur.append(("Concentration, up to " if x.get("concentration") else "") + f"{dd.get('amount', '')} {dd.get('type', '')}")
            else:
                dur.append(x.get("type", "").replace("instant", "Instantaneous").title() if x.get("type") != "instant" else "Instantaneous")
        return (f"<h3>{esc(s['name'])}</h3><p><i>{esc(kind)}</i></p>"
                f"<p><b>Casting Time</b> {esc(time)}<br/><b>Range</b> {esc(rng)}<br/>"
                f"<b>Components</b> {self.inline(', '.join(cl))}<br/><b>Duration</b> {esc(', '.join(dur))}</p>"
                + self.entries(s.get("entries"), 4)
                + (f"<p><b>At Higher Levels.</b></p>{self.entries(s['entriesHigherLevel'], 4)}" if s.get("entriesHigherLevel") else ""))


CSS_EXTRA = (".mentions{font-size:.85em}"
             "aside{border:1px solid #888;padding:.3em .8em;margin:1em 0;background:#f5f1e8}"
             "aside.readaloud{background:#e8eef5}aside.stat{background:#faf3e0}"
             "table{border-collapse:collapse;margin:1em 0}td,th{border:1px solid #999;padding:.2em .5em}"
             "figure{margin:1em 0;text-align:center}figcaption{font-size:.85em}")

FONT_FILES = (
    ("Bookinsanity", "Bookinsanity.otf", "normal", "normal"),
    ("Bookinsanity", "Bookinsanity Bold.otf", "bold", "normal"),
    ("Bookinsanity", "Bookinsanity Italic.otf", "normal", "italic"),
    ("Bookinsanity", "Bookinsanity Bold Italic.otf", "bold", "italic"),
    ("Nodesto Caps Condensed", "Nodesto Caps Condensed.otf", "normal", "normal"),
    ("Nodesto Caps Condensed", "NodestoCapsCondensed-Bold.otf", "bold", "normal"),
    ("Nodesto Caps Condensed", "NodestoCapsCondensed-Italic.otf", "normal", "italic"),
    ("Nodesto Caps Condensed", "NodestoCapsCondensed-Bold Italic.otf", "bold", "italic"),
    ("Mr Eaves Small Caps", "Mr Eaves Small Caps.otf", "normal", "normal"),
    ("Scaly Sans", "Scaly Sans.otf", "normal", "normal"),
    ("Scaly Sans", "Scaly Sans Bold.otf", "bold", "normal"),
    ("Scaly Sans", "Scaly Sans Italic.otf", "normal", "italic"),
    ("Scaly Sans", "Scaly Sans Bold Italic.otf", "bold", "italic"),
    ("Scaly Sans Caps", "Scaly Sans Caps.otf", "normal", "normal"),
    ("Scaly Sans Caps", "Scaly Sans Caps Bold.otf", "bold", "normal"),
    ("Scaly Sans Caps", "Scaly Sans Caps Italic.otf", "normal", "italic"),
    ("Scaly Sans Caps", "Scaly Sans Caps Bold Italic.otf", "bold", "italic"),
    ("Zatanna Misdirection", "Zatanna Misdirection.otf", "normal", "normal"),
    ("Zatanna Misdirection", "Zatanna Misdirection Bold.otf", "bold", "normal"),
    ("Zatanna Misdirection", "Zatanna Misdirection Italic.otf", "normal", "italic"),
    ("Zatanna Misdirection", "Zatanna Misdirection Bold Italic.otf", "bold", "italic"),
)

FONT_CSS = (
    'body{font-family:"Bookinsanity",serif}'
    'h1{font-family:"Nodesto Caps Condensed",serif;font-weight:bold}'
    'h2,h3,h4,h5,h6{font-family:"Mr Eaves Small Caps",serif}'
    'table{font-family:"Scaly Sans",sans-serif}'
    'caption,thead{font-family:"Scaly Sans Caps",sans-serif}'
    'blockquote{font-family:"Zatanna Misdirection",serif}'
)


def find_book(repo, bid):
    for fname, key, folder, dkey in (("books.json", "book", "book", "bookData"),
                                      ("adventures.json", "adventure", "adventure", "adventureData")):
        for meta in read_json(repo / "data" / fname).get(key, []):
            if meta["id"].lower() == bid.lower():
                path = repo / "data" / folder / f"{folder}-{meta['id'].lower()}.json"
                if not path.exists():
                    raise SystemExit(f"Content file not found: {path}")
                for b in read_json(path)[dkey]:
                    if b["id"].lower() == bid.lower():
                        return meta, b["data"], key
    raise SystemExit(f"No book or adventure with id '{bid}'. Use --list.")


def list_all(repo, title_filter=None):
    rows = []
    title_filter = title_filter.casefold() if title_filter else None
    for fname, key in (("books.json", "book"), ("adventures.json", "adventure")):
        for m in read_json(repo / "data" / fname).get(key, []):
            name = str(m["name"])
            if title_filter is not None and title_filter not in name.casefold():
                continue
            setting = m.get("setting", "")
            if isinstance(setting, (list, tuple)):
                setting = ", ".join(str(value) for value in setting)
            elif setting is None:
                setting = ""
            else:
                setting = str(setting)
            rows.append((key, str(m["id"]), setting, name))

    include_setting = any(row[2] for row in rows)
    headers = ("TYPE", "CODE", "SETTING", "NAME") if include_setting else ("TYPE", "CODE", "NAME")
    display_rows = [
        row if include_setting else (row[0], row[1], row[3])
        for row in rows
    ]
    widths = [
        max([len(header)] + [len(row[index]) for row in display_rows])
        for index, header in enumerate(headers)
    ]

    def format_row(row):
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()

    print(format_row(headers))
    print("  ".join("-" * width for width in widths))
    for row in display_rows:
        print(format_row(row))


def build_toc(contents_entry, headings):
    """Map the book's curated header list onto rendered heading anchors, in order."""
    toc = []
    for h in contents_entry.get("headers", []):
        name, depth, nth = (h, 0, 0) if isinstance(h, str) else (h["header"], h.get("depth", 0), h.get("index", 0))
        key = Renderer.norm(name)
        matches = [x for x in headings if x[0] == key]
        anchor = matches[nth][1] if nth < len(matches) else ""
        toc.append((anchor, re.sub(r"\{@\w+ ([^|}]*)[^}]*\}", r"\1", name), depth))
    return toc


def convert(repo, bid, out_dir, img_root, crop_cover=True, image_cache=IMAGE_CACHE_DIR):
    meta, sections, kind = find_book(repo, bid)
    ids = []
    for i, s in enumerate(sections):
        ids.append(slug(f"{i:02d}-{s.get('name', 'section')}"))
    r = Renderer(meta["id"], ids, Lookup(repo / "data"), img_root)
    r.prescan(sections)
    chapters = []
    cover_url = r.img_url(meta.get("cover"))
    if cover_url:
        chapters.append({"id": "cover", "title": "Cover",
                         "content": f'<figure><img src="{html.escape(cover_url, quote=True)}" alt="Cover"/></figure>'
                                    f'<p><b>{esc(meta["name"])}</b></p>' + (
                                        f'<p>{esc(meta["author"])}</p>' if meta.get("author") else "")})
    contents = meta.get("contents", [])
    for i, (sid, s) in enumerate(zip(ids, sections)):
        if isinstance(s, dict):
            c = contents[i] if i < len(contents) else {}
            title = re.sub(r"\{@\w+ ([^|}]*)[^}]*\}", r"\1", s.get("name", "Section"))
            r.headings, r.counter = [], 0
            r.cur_file, r.cur_heading = sid, ("", title)
            content = r.entries(s.get("entries"), 1) if s.get("type") in ("section", "entries", None) else r.entry(s, 1)
            if s.get("id") is not None and f"i{slug(s['id'])}" not in r.emitted:
                r.emitted.add(f"i{slug(s['id'])}")
                content = f'<span id="i{slug(s["id"])}"></span>' + content
            toc = build_toc(c, r.headings)
            linked = [(a, n, d) for a, n, d in toc if a]
            if len(linked) >= 2:
                content = ('<ul class="mini">' + "".join(
                    f'<li class="d{min(d, 1)}"><a href="#{a}">{esc(n)}</a></li>' for a, n, d in linked) + "</ul>") + content
            chapters.append({"id": sid, "title": title, "content": content, "toc": toc})
    references = r.references_appendix()
    for ch in chapters:
        ch["content"] = re.sub(r"@@BL:(\w+):([^@]*)@@", lambda m: r.backlinks(m.group(1), m.group(2)), ch["content"])
    appendices = ([{"id": "references", "title": "References", "content": references}]
                  if references else [])
    appendices.append({
        "id": "typography",
        "title": "Typography",
        "content": (
            "<p>This EPUB embeds Solbera's D&amp;D 5e font collection: Bookinsanity, "
            "Nodesto Caps Condensed, Mr Eaves Small Caps, Scaly Sans, Scaly Sans Caps, "
            "and Zatanna Misdirection.</p>"
            '<p>The fonts are distributed under the <a href="https://creativecommons.org/licenses/by-sa/4.0/">'
            "Creative Commons Attribution-ShareAlike 4.0 International license</a>. "
            '<a href="https://benlk.github.io/solbera-dnd-fonts/">Font source and specimens</a>.</p>'
        ),
    })
    fonts = [{"family": family, "src": str(FONT_DIR / filename), "weight": weight, "style": style}
             for family, filename, weight, style in FONT_FILES]
    data = {"title": meta["name"], "author": meta.get("author"), "language": "en",
            "identifier": f"urn:5etools:{meta['id']}", "chapters": chapters,
            "appendices": appendices, "index": r.index_entries(None), "fonts": fonts,
            "css": CSS_EXTRA + FONT_CSS}
    book = Book(data, repo, crop_cover=crop_cover, image_cache=image_cache)
    if cover_url:
        data["cover"] = cover_url
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{slug(meta['name'])}.epub"
    book.build(str(target))
    print(f"Wrote {target}")


def main():
    ap = argparse.ArgumentParser(description="Convert one 5etools book/adventure to EPUB.")
    ap.add_argument("id", nargs="?", help="book/adventure id, e.g. PHB or LMoP")
    ap.add_argument("--repo", default="../5etools-src", help="path to 5etools-src (default: ../5etools-src)")
    ap.add_argument("-o", "--out", default="out", help="output directory (default: out)")
    ap.add_argument(
        "--list", nargs="?", const="", metavar="TITLE_SUBSTRING",
        help="list titles, optionally filtering by a title substring",
    )
    ap.add_argument("--img-root", default=SITE + "img/")
    ap.add_argument("--no-crop-cover", action="store_true", help="preserve the original cover canvas")
    ap.add_argument("--clear-cache", action="store_true", help="clear cached web images before continuing")
    a = ap.parse_args()
    repo = Path(a.repo).expanduser()
    if a.clear_cache:
        count = clear_image_cache(IMAGE_CACHE_DIR)
        print(f"Cleared {count} cached image{'s' if count != 1 else ''} from {IMAGE_CACHE_DIR}")
    if a.list is not None:
        return list_all(repo, a.list)
    if not a.id:
        if a.clear_cache:
            return
        ap.error("id required (or use --list/--clear-cache)")
    convert(repo, a.id, a.out, a.img_root, crop_cover=not a.no_crop_cover)


if __name__ == "__main__":
    main()
