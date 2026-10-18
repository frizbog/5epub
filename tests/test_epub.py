import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from PIL import Image

from json2epub import Book, clear_image_cache


ROOT = Path(__file__).resolve().parents[1]


def load_5etools_module():
    spec = importlib.util.spec_from_file_location("five_tools_to_epub", ROOT / "5etools2epub.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHeaders:
    def get_content_type(self):
        return "image/png"

    def get(self, name, default=None):
        return default


class FakeResponse(io.BytesIO):
    headers = FakeHeaders()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class BookTests(unittest.TestCase):
    def test_cover_is_cropped_to_opaque_portrait_artwork(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cover = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
            cover.paste((40, 80, 120, 255), (15, 0, 85, 100))
            cover.save(root / "cover.png")
            data = {"cover": "cover.png", "chapters": [{"content": '<img src="cover.png"/>'}]}
            target = root / "cropped.epub"
            Book(data, root).build(target)
            with zipfile.ZipFile(target) as epub:
                cover_name = next(n for n in epub.namelist() if n.startswith("OEBPS/images/"))
                self.assertTrue(cover_name.endswith(".jpg"))
                with Image.open(io.BytesIO(epub.read(cover_name))) as cropped:
                    self.assertEqual(cropped.size, (70, 100))

    def test_cover_crop_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cover = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
            cover.paste((40, 80, 120, 255), (15, 0, 85, 100))
            cover.save(root / "cover.png")
            data = {"cover": "cover.png", "chapters": [{"content": '<img src="cover.png"/>'}]}
            target = root / "original.epub"
            Book(data, root, crop_cover=False).build(target)
            with zipfile.ZipFile(target) as epub:
                cover_name = next(n for n in epub.namelist() if n.startswith("OEBPS/images/"))
                with Image.open(io.BytesIO(epub.read(cover_name))) as original:
                    self.assertEqual(original.size, (100, 100))

    def test_remote_images_are_packaged(self):
        data = {
            "title": "Offline",
            "chapters": [{"content": '<p><a href="https://example.com">web</a></p>'
                                      '<img src="https://example.com/map.png"/>'}],
        }
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "book.epub"
            with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"PNG")) as fetch:
                Book(data, Path(td)).build(target)

            request = fetch.call_args.args[0]
            self.assertEqual(request.full_url, "https://example.com/map.png")
            self.assertEqual(request.headers["User-agent"], "5epub/1.0")
            self.assertEqual(request.headers["Accept"], "image/*")
            self.assertEqual(request.headers["Connection"], "close")
            self.assertIsInstance(fetch.call_args.kwargs["context"], __import__("ssl").SSLContext)
            self.assertEqual(fetch.call_args.kwargs["context"].verify_mode, __import__("ssl").CERT_REQUIRED)
            with zipfile.ZipFile(target) as epub:
                chapter = epub.read("OEBPS/chapter1.xhtml").decode()
                package = epub.read("OEBPS/content.opf").decode()
                self.assertIn('href="https://example.com"', chapter)
                self.assertIn('src="images/1-map.png"', chapter)
                self.assertNotIn('src="https://', chapter)
                self.assertNotIn("remote-resources", package)
                self.assertEqual(epub.read("OEBPS/images/1-map.png"), b"PNG")

    def test_transient_image_download_failures_are_retried(self):
        data = {"chapters": [{"content": '<img src="https://example.com/map.png"/>'}]}
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "book.epub"
            responses = [OSError("temporary TLS EOF"), FakeResponse(b"PNG")]
            with mock.patch("urllib.request.urlopen", side_effect=responses) as fetch, \
                    mock.patch("json2epub.time.sleep") as sleep:
                Book(data, Path(td)).build(target)
            self.assertEqual(fetch.call_count, 2)
            sleep.assert_called_once_with(0.5)
            with zipfile.ZipFile(target) as epub:
                self.assertEqual(epub.read("OEBPS/images/1-map.png"), b"PNG")

    def test_remote_images_are_reused_from_verified_cache(self):
        data = {"chapters": [{"content": '<img src="https://example.com/map.png"/>'}]}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "cache"
            with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"PNG")) as fetch:
                Book(data, root, image_cache=cache).build(root / "first.epub")
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(len(list(cache.glob("*.bin"))), 1)
            self.assertEqual(len(list(cache.glob("*.json"))), 1)

            with mock.patch("urllib.request.urlopen") as fetch:
                Book(data, root, image_cache=cache).build(root / "second.epub")
            fetch.assert_not_called()
            with zipfile.ZipFile(root / "second.epub") as epub:
                self.assertEqual(epub.read("OEBPS/images/1-map.png"), b"PNG")

            self.assertEqual(clear_image_cache(cache), 1)
            self.assertFalse(cache.exists())

    def test_unhandled_remote_resources_are_rejected(self):
        data = {"chapters": [{"content": '<video src="https://example.com/movie.mp4"></video>'}]}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "Unpackaged resource attribute"):
                Book(data, Path(td)).build(Path(td) / "book.epub")

    def test_local_resources_that_would_be_missing_are_rejected(self):
        data = {"chapters": [{"content": '<script src="app.js"></script>'}]}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "Unpackaged resource attribute"):
                Book(data, Path(td)).build(Path(td) / "book.epub")

    def test_output_parent_directories_are_created(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "nested" / "books" / "book.epub"
            Book({"chapters": [{"content": "Hello"}]}, Path(td)).build(target)
            self.assertTrue(target.is_file())

    def test_data_uri_images_become_manifest_resources(self):
        data = {"chapters": [{"content": '<img src="data:image/png;base64,UE5H"/>'
                                         '<img src="data:image/png;base64,UE5H"/>'}]}
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "book.epub"
            Book(data, Path(td)).build(target)
            with zipfile.ZipFile(target) as epub:
                chapter = epub.read("OEBPS/chapter1.xhtml").decode()
                package = epub.read("OEBPS/content.opf").decode()
                self.assertIn('src="images/1-image.png"', chapter)
                self.assertIn('media-type="image/png"', package)
                self.assertEqual(epub.read("OEBPS/images/1-image.png"), b"PNG")
                self.assertNotIn("OEBPS/images/2-image.png", epub.namelist())


class FiveToolsTests(unittest.TestCase):
    def test_list_uses_aligned_columns_and_optional_setting(self):
        converter = load_5etools_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir()
            (root / "data" / "books.json").write_text(json.dumps({
                "book": [
                    {"id": "FR", "setting": "Forgotten Realms", "name": "Realms Guide"},
                    {"id": "X", "name": "Settingless Book"},
                ],
            }))
            (root / "data" / "adventures.json").write_text(json.dumps({
                "adventure": [{"id": "ADV", "setting": ["Greyhawk", "Planescape"],
                               "name": "Long Adventure"}],
            }))
            output = io.StringIO()
            with redirect_stdout(output):
                converter.list_all(root)

        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], "TYPE       CODE  SETTING               NAME")
        self.assertEqual(lines[1], "---------  ----  --------------------  ----------------")
        self.assertEqual(lines[2], "book       FR    Forgotten Realms      Realms Guide")
        self.assertEqual(lines[3], "book       X                           Settingless Book")
        self.assertEqual(lines[4], "adventure  ADV   Greyhawk, Planescape  Long Adventure")

    def test_list_omits_setting_column_when_unused(self):
        converter = load_5etools_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir()
            (root / "data" / "books.json").write_text(
                '{"book": [{"id": "B", "name": "A Book"}]}'
            )
            (root / "data" / "adventures.json").write_text(
                '{"adventure": [{"id": "LONG", "name": "An Adventure"}]}'
            )
            output = io.StringIO()
            with redirect_stdout(output):
                converter.list_all(root)

        self.assertEqual(output.getvalue().splitlines(), [
            "TYPE       CODE  NAME",
            "---------  ----  ------------",
            "book       B     A Book",
            "adventure  LONG  An Adventure",
        ])

    def test_list_filters_titles_case_insensitively(self):
        converter = load_5etools_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir()
            (root / "data" / "books.json").write_text(json.dumps({
                "book": [
                    {"id": "FR", "setting": "Forgotten Realms", "name": "Realms Guide"},
                    {"id": "X", "name": "Settingless Book"},
                ],
            }))
            (root / "data" / "adventures.json").write_text(json.dumps({
                "adventure": [{"id": "ADV", "name": "Adventure in the REALMS"}],
            }))
            output = io.StringIO()
            with redirect_stdout(output):
                converter.list_all(root, "realms")

        self.assertEqual(output.getvalue().splitlines(), [
            "TYPE       CODE  SETTING           NAME",
            "---------  ----  ----------------  -----------------------",
            "book       FR    Forgotten Realms  Realms Guide",
            "adventure  ADV                     Adventure in the REALMS",
        ])

    def test_external_entities_are_bundled_transitively(self):
        converter = load_5etools_module()

        class Lookup:
            records = {
                ("item", "wand", "tst"): {
                    "name": "Wand", "source": "TST",
                    "entries": ["The wand casts {@spell Spark|TST}."],
                },
                ("spell", "spark", "tst"): {
                    "name": "Spark", "source": "TST", "level": 0, "school": "V",
                    "time": [{"number": 1, "unit": "action"}],
                    "range": {"type": "point", "distance": {"type": "touch"}},
                    "components": {"v": True}, "duration": [{"type": "instant"}],
                    "entries": ["A harmless spark appears."],
                },
            }

            def get(self, tag, name, source=None):
                return self.records.get((tag, name.lower(), (source or "").lower()))

        renderer = converter.Renderer("TEST", ["chapter"], Lookup(), "https://example.com/img/")
        renderer.cur_file, renderer.cur_heading = "chapter", ("", "Chapter")
        text = renderer.inline("Use a {@item Wand|TST}. See {@book Player's Handbook|PHB}.")
        appendix = renderer.references_appendix()
        self.assertIn('href="references.xhtml#ref-item-wand"', text)
        self.assertNotIn("https://", text)
        self.assertIn("Player's Handbook", text)
        self.assertIn('id="ref-item-wand"', appendix)
        self.assertIn('href="references.xhtml#ref-spell-spark"', appendix)
        self.assertIn('id="ref-spell-spark"', appendix)
        self.assertNotIn("https://", appendix)

    def test_explicit_source_does_not_fall_back_to_wrong_record(self):
        converter = load_5etools_module()
        lookup = converter.Lookup(Path("unused"))
        default = {"name": "Shared Name", "source": "A"}
        lookup.cache["creature"] = {
            ("shared name", "a"): default,
            ("shared name", None): default,
        }
        self.assertIs(lookup.get("creature", "Shared Name"), default)
        self.assertIsNone(lookup.get("creature", "Shared Name", "B"))

    def test_sections_are_not_dropped_when_contents_metadata_is_shorter(self):
        converter = load_5etools_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "book").mkdir(parents=True)
            (root / "data" / "books.json").write_text(json.dumps({
                "book": [{"id": "T", "name": "Test Book", "contents": []}],
            }))
            (root / "data" / "adventures.json").write_text('{"adventure": []}')
            (root / "data" / "book" / "book-t.json").write_text(json.dumps({
                "bookData": [{"id": "T", "data": [
                    {"type": "section", "name": "One", "entries": ["First"]},
                    {"type": "section", "name": "Two", "entries": ["Second"]},
                ]}],
            }))
            out = root / "out"
            converter.convert(root, "T", out, "https://example.com/img/")
            with zipfile.ZipFile(out / "Test-Book.epub") as epub:
                package = epub.read("OEBPS/content.opf").decode()
                stylesheet = epub.read("OEBPS/style.css").decode()
                self.assertIn('href="00-One.xhtml"', package)
                self.assertIn('href="01-Two.xhtml"', package)
                self.assertEqual(sum(n.startswith("OEBPS/fonts/") for n in epub.namelist()), 21)
                self.assertEqual(package.count('media-type="font/otf"'), 21)
                self.assertIn('font-family:"Bookinsanity"', stylesheet)
                self.assertIn('body{font-family:"Bookinsanity",serif}', stylesheet)
                self.assertIn("p,li,dd{orphans:3;widows:3}", stylesheet)
                self.assertIn("h1,h2,h3,h4,h5,h6{margin-bottom:.25em;break-after:avoid;page-break-after:avoid}", stylesheet)
                self.assertIn("h1+*,h2+*,h3+*,h4+*,h5+*,h6+*{margin-top:0;break-before:avoid;page-break-before:avoid}", stylesheet)
                self.assertIn("h1+p,h2+p,h3+p,h4+p,h5+p,h6+p{orphans:1;widows:2}", stylesheet)
                self.assertIn("figure,.mini{break-inside:avoid;page-break-inside:avoid}", stylesheet)
                self.assertIn("OEBPS/typography.xhtml", epub.namelist())


if __name__ == "__main__":
    unittest.main()
