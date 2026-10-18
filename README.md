# 5epub

`5epub` is a small Python toolkit for creating EPUB 3
files. It includes a generic JSON-to-EPUB builder and a converter for books and
adventures from a local
[`5etools-src`](https://github.com/5etools-mirror-3/5etools-src) checkout.

Python 3.8 or newer is required. Install the preferred image-processing
dependency with:

```sh
python3 -m pip install -r requirements.txt
```

On macOS, cover cropping falls back to the native `sips` utility if Pillow is
missing or cannot load for the current CPU architecture.

| Script | Purpose |
|---|---|
| `json2epub.py` | Build an EPUB from the JSON format described below. |
| `5etools2epub.py` | Convert one 5etools book or adventure using `json2epub.py`. |
| `copyapply.py` | Resolve the subset of 5etools `_copy` inheritance needed by the converter. |

## Convert a 5etools book or adventure

By default, the converter expects `5etools-src` to be a sibling of this
repository:

```text
parent-directory/
├── 5epub/
└── 5etools-src/
```

Run these commands from the `5epub` directory:

```sh
# List the available book and adventure IDs.
python3 5etools2epub.py --list

# List only titles containing a substring (case-insensitive).
python3 5etools2epub.py --list handbook
python3 5etools2epub.py --list "forgotten realms"

# Convert one title. IDs are case-insensitive.
python3 5etools2epub.py LMoP
python3 5etools2epub.py PHB -o out/

# Point to a checkout in a different location.
python3 5etools2epub.py --repo /path/to/5etools-src PHB -o out/
```

The output directory defaults to `out/` and is created when necessary. The
EPUB filename is a filesystem-safe version of the source title; for example,
*Lost Mine of Phandelver* becomes `Lost-Mine-of-Phandelver.epub`.

### Options

| Option | Description |
|---|---|
| `id` | A book or adventure ID reported by `--list`, such as `PHB` or `LMoP`. |
| `--repo PATH` | Path to `5etools-src` (default: `../5etools-src`, relative to the current working directory). |
| `-o DIR`, `--out DIR` | Output directory (default: `out`). |
| `--list [TITLE_SUBSTRING]` | Print available titles in aligned type, code, optional setting, and name columns. When supplied, filter titles by a case-insensitive substring. |
| `--img-root URL` | Base URL used to download non-external 5etools images (default: `https://5e.tools/img/`). |
| `--no-crop-cover` | Preserve the source cover canvas instead of trimming vertical padding. |
| `--clear-cache` | Remove cached web images. With an ID, clearing happens before conversion; without an ID, the command exits after clearing. |

Remote images are downloaded once and reused from `.cache/images/` on later
builds. Cache entries are keyed by URL and verified against stored byte counts
and SHA-256 digests before use. Conversion therefore requires network access
only for uncached remote images, and fails if a required image is unavailable.
Every image is still embedded in the finished EPUB, which does not need a
network connection to render its content. HTTPS downloads use certificate
verification with Python's default trust store augmented by common
operating-system CA bundles. Transient connection and TLS failures are retried
four times with exponential backoff; empty or truncated responses are rejected
instead of being embedded as broken images. Run
`python3 5etools2epub.py --clear-cache` to remove the cache.

By default, transparent or uniform vertical padding is removed from cover
images when the result has a plausible portrait-book aspect ratio. Cropped
covers are stored as JPEG or PNG for reader compatibility. Cropping does not
upscale the source image. Use `--no-crop-cover` to retain the original canvas.

### Generated content

For source data the renderer understands, an EPUB can include:

- A cover chapter when the 5etools metadata supplies a cover.
- One chapter for each top-level data section. Metadata contents entries add
  navigation information when present.
- Nested navigation entries derived from source headers that can be matched to
  rendered headings.
- A mini-contents list at the start of a chapter when at least two headings are
  matched.
- Common 5etools inline tags, lists, tables, quotes, insets, read-aloud blocks,
  images, and galleries.
- Detailed creature and spell statblocks when their records can be found. Other
  supported statblock types contain their name and descriptive entries.
- Local links to statblocks and areas, backlinks from statblocks, and an index
  of mentioned creatures, spells, and items.
- A generated References appendix containing resolvable creatures, spells,
  items, and other entities referenced by the book but not already included in
  it. References discovered inside those entries are bundled transitively.
- Plain text, rather than an external link, for cross-book citations and
  references whose source record cannot be found.
- Embedded typography from the local `fonts/` directory: Bookinsanity for body
  copy, Nodesto Caps Condensed for major titles, Mr Eaves Small Caps for
  headings, Scaly Sans and Scaly Sans Caps for tables, and Zatanna Misdirection
  for quotations. A Typography appendix records the attribution and CC BY-SA
  4.0 license.

### Limitations

- The 5etools renderer covers common source structures, not every entry type or
  inline tag in the 5etools schema. Unknown block types may be reduced to their
  nested `entries`/`entry` content or omitted.
- `_copy` handling is a best-effort partial port of the 5etools copy applier.
  Unsupported or invalid modification operations are skipped, so a derived
  statblock can be incomplete.
- A statblock whose source record cannot be found is rendered as a name-only
  placeholder.
- Embedded `.webp` images are not supported by every EPUB reader.
- Output has not been validated with `epubcheck` or tested across physical
  e-readers.

## Build an EPUB from JSON

```sh
python3 json2epub.py book.json
python3 json2epub.py book.json -o custom-name.epub
python3 json2epub.py book.json --no-crop-cover
python3 json2epub.py --clear-cache
```

Without `-o`, the output path is the input path with its extension changed to
`.epub`. Missing parent directories in the output path are created. See
[`example.json`](example.json) for a complete small example.
The JSON converter shares the same verified `.cache/images/` web-image cache
and `--clear-cache` behavior as the 5etools converter.

The input object supports these top-level fields:

| Field | Required | Description |
|---|---|---|
| `title` | No | Book title; defaults to `Untitled`. |
| `author` | No | Creator name stored in EPUB metadata. |
| `language` | No | Language code; defaults to `en`. |
| `identifier` | No | EPUB identifier; a UUID URN is generated when omitted. |
| `cover` | No | Local path or URL for the EPUB cover-image manifest item. It does not create a visible cover page by itself. |
| `chapters` | No | Ordered chapter objects. |
| `appendices` | No | Ordered appendix objects, placed after the chapters. |
| `index` | No | Index entries; when non-empty, an Index page is added. |

A minimal input looks like this:

```json
{
  "title": "Sample Book",
  "author": "A. Writer",
  "language": "en",
  "chapters": [
    {
      "id": "intro",
      "title": "Introduction",
      "content": "<p id=\"whale\">A whale.</p>"
    }
  ],
  "appendices": [
    {
      "id": "notes",
      "title": "Notes",
      "content": "Back to the notes."
    }
  ],
  "index": [
    {"term": "Whale", "targets": ["intro#whale"]}
  ]
}
```

### Section objects

Each item in `chapters` or `appendices` supports:

- `id`: Optional section ID. Defaults to `chapterN` or `appendixN` and is
  converted to a safe filename. Duplicate IDs gain trailing underscores.
- `title`: Optional display title. Defaults to `Chapter N` or `Appendix N`.
- `content`: HTML, or plain text split into paragraphs at blank lines. This is
  parsed as an HTML fragment and serialized as XHTML.
- `toc`: Optional arrays of `[anchor, title, depth]` used to create nested
  navigation entries beneath the section.

### Images and links

- A local `<img src>` path is resolved relative to the input JSON file and
  embedded in the EPUB.
- An HTTP or HTTPS image is downloaded and embedded during conversion.
- An image data URI is decoded and stored as a regular EPUB image resource.
- Other resource-loading HTML such as external stylesheets, scripts, frames,
  media, `srcset`, and CSS `url()` declarations is rejected rather than
  producing an incomplete or network-dependent EPUB.
- Normal `<a href>` links are preserved.
- `href="#section-id"` and `href="#section-id#anchor"` are rewritten to point
  to the corresponding XHTML section. Other fragment-only links remain within
  the current page.

### Index entries

Each index entry requires `term` and may contain:

- `targets`: Section IDs or `section-id#anchor` values. A target may instead be
  an object such as `{"to": "intro#whale", "label": "Introduction"}`.
- `group`: A heading used to divide the index into groups.
- `term_to`: A section or anchored section that makes the term itself a link.

Entries are sorted within each group and divided by their initial letter.

## Pagination behavior

The stylesheet asks paginated readers to keep figures with their captions,
headings with the content that follows, table rows intact, and chapter
mini-contents on one page. Paragraphs, list items, and definition descriptions
request at least three lines on each side of a page break. Both modern
`break-*` properties and legacy `page-break-*` aliases are included. These are
pagination preferences: a reader may relax them when an element cannot fit on
one page. CSS cannot request a break based on an exact amount of remaining page
space; the paired heading/following-content rules provide the closest portable
behavior. The first paragraph following a heading deliberately permits one
line at the bottom of a page, prioritizing the heading/text connection over the
general orphan rule. Heading bottom margins are kept to a quarter em, and the
following element's top margin is removed, so headings sit close to the content
they introduce without affecting spacing between ordinary paragraphs.
