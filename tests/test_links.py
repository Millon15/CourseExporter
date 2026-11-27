import tempfile
import unittest
from pathlib import Path

from course_exporter import links
from course_exporter.model import Lesson, LessonRef, UnsupportedMedia

REF = LessonRef("kwiga", "c", "l1", (1, 1), "Lesson", "https://school.test/courses/c/l1")
SHEET = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit?usp=sharing"
NOTION = "https://chatter-atom-bdf.notion.site/Page-title-0123456789abcdef0123456789abcdef?pvs=4"


def material_id(url: str) -> str:
    return links.asset_id(links.classify(url))


def anchor(url: str, label: str) -> str:
    return f'<a href="{url.replace("&", "&amp;")}" rel="noreferrer">{label}</a>'


class ClassifyTest(unittest.TestCase):
    CASES = {
        "https://docs.google.com/document/d/1DocIdDocIdDocIdDocIdDocId/edit":
            ("file", "https://docs.google.com/document/d/1DocIdDocIdDocIdDocIdDocId/export?format=pdf", ".pdf"),
        SHEET: ("file", "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
                        "/export?format=xlsx", ".xlsx"),
        "https://docs.google.com/presentation/d/1SlideIdSlideIdSlideId0/edit#slide=id.p":
            ("file", "https://docs.google.com/presentation/d/1SlideIdSlideIdSlideId0/export/pdf", ".pdf"),
        "https://drive.google.com/file/d/1FileIdFileIdFileIdFileId/view?usp=drive_link":
            ("file", "https://drive.google.com/uc?export=download&id=1FileIdFileIdFileIdFileId", ""),
        "https://drive.google.com/open?id=1FileIdFileIdFileIdFileId":
            ("file", "https://drive.google.com/uc?export=download&id=1FileIdFileIdFileIdFileId", ""),
        NOTION: ("pdf", NOTION, ".pdf"),
        "https://www.notion.so/team/0123456789abcdef0123456789abcdef":
            ("pdf", "https://www.notion.so/team/0123456789abcdef0123456789abcdef", ".pdf"),
        "https://files.school.test/guide/Instruction.PDF#page=2":
            ("file", "https://files.school.test/guide/Instruction.PDF", ".pdf"),
    }
    PLAIN = ["https://www.instagram.com/someone/", "https://t.me/channel", "https://www.figma.com/file/abc/x",
             "http://docs.google.com/document/d/1DocIdDocIdDocIdDocIdDocId/edit",
             "https://drive.google.com/drive/folders/1FolderIdFolderIdFolder", "https://notion.site/no-id-here",
             "https://user:pw@files.school.test/a.pdf", "mailto:x@y.test"]

    def test_each_material_shape_maps_to_its_export(self):
        for url, (kind, source, suffix) in self.CASES.items():
            with self.subTest(url=url):
                material = links.classify(url)
                self.assertEqual((material.kind, material.source, material.suffix), (kind, source, suffix))

    def test_other_links_stay_plain(self):
        for url in self.PLAIN:
            with self.subTest(url=url):
                self.assertIsNone(links.classify(url))

    def test_one_material_linked_twice_has_one_canonical_form(self):
        self.assertEqual(links.classify(SHEET).canonical, links.classify(SHEET.replace("/edit?usp=sharing", "")).canonical)


class AddMaterialsTest(unittest.TestCase):
    def test_adds_materials_in_document_order_and_links_every_mention(self):
        html = (f"<p>{anchor(NOTION, 'Правки <b>клієнта</b>')}</p><p>{anchor('https://t.me/x', 'chat')}</p>"
                f"<p>{anchor(SHEET, 'Таблиця')}</p><p>{anchor(NOTION, 'again')}</p>")
        lesson = links.add_materials(Lesson(REF, "available", html, ()))
        notion, sheet = material_id(NOTION), material_id(SHEET)
        self.assertEqual([(a.asset_id, a.kind, a.title) for a in lesson.assets],
                         [(notion, "file", "Правки клієнта.pdf"), (sheet, "file", "Таблиця.xlsx")])
        self.assertEqual(lesson.html.count(f'<a href="asset:{notion}">saved copy</a>'), 2)
        self.assertEqual(lesson.html.count(f'<a href="asset:{sheet}">saved copy</a>'), 1)
        self.assertIn(anchor("https://t.me/x", "chat") + "</p>", lesson.html)

    def test_a_material_keeps_its_id_when_another_link_is_inserted_before_it(self):
        first = links.add_materials(Lesson(REF, "available", anchor(SHEET, "a"), ()))
        later = links.add_materials(Lesson(REF, "available", anchor(NOTION, "b") + anchor(SHEET, "a"), ()))
        self.assertEqual(first.assets[0].asset_id, later.assets[1].asset_id)
        self.assertRegex(first.assets[0].asset_id, r"\Alink-[0-9a-f]{12}\Z")

    def test_the_asset_ref_keeps_the_url_the_lesson_links(self):
        lesson = links.add_materials(Lesson(REF, "available", anchor(NOTION + "#frag", "x"), ()))
        self.assertEqual(lesson.assets[0].ref, "link:" + NOTION)
        self.assertEqual(links.resolve(lesson.assets[0]).url, NOTION)
        self.assertTrue(links.is_material(lesson.assets[0]))

    def test_an_escaped_query_is_unescaped_before_classifying(self):
        url = "https://drive.google.com/open?id=1FileIdFileIdFileIdFileId&usp=x"
        lesson = links.add_materials(Lesson(REF, "available", anchor(url, "Файл.pdf"), ()))
        self.assertEqual(lesson.assets[0].title, "Файл.pdf")
        self.assertEqual(links.resolve(lesson.assets[0]).url,
                         "https://drive.google.com/uc?export=download&id=1FileIdFileIdFileIdFileId")

    def test_a_lesson_without_materials_is_returned_unchanged(self):
        lesson = Lesson(REF, "available", anchor("https://t.me/x", "chat"), ())
        self.assertIs(links.add_materials(lesson), lesson)


class RefuseHtmlTest(unittest.TestCase):
    def test_a_web_page_in_place_of_a_file_is_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, sheet = Path(tmp, "a.xlsx"), Path(tmp, "b.xlsx")
            page.write_bytes(b"\n  <!DOCTYPE html><html><title>Sign in</title>")
            sheet.write_bytes(b"PK\x03\x04 sheet")
            with self.assertRaises(UnsupportedMedia):
                links.refuse_html(page)
            page.write_bytes(b"\xef\xbb\xbf<html><title>Sign in</title>")
            with self.assertRaises(UnsupportedMedia):
                links.refuse_html(page)
            links.refuse_html(sheet)


class NameByContentTest(unittest.TestCase):
    CASES = [(b"%PDF-1.7 x", ".pdf"), (b"\x89PNG\r\n\x1a\n...", ".png"), (b"\xff\xd8\xff\xe0", ".jpg"),
             (b"PK\x03\x04[Content_Types].xml word/document.xml", ".docx"),
             (b"PK\x03\x04[Content_Types].xml xl/workbook.xml", ".xlsx"), (b"PK\x03\x04 data.csv", ".zip"),
             (b"PK\x03\x04 word/project-notes.txt", ".zip"), (b"PK\x03\x04[Content_Types].xml word/notes.txt", ".zip"),
             (b"plain text", ".bin")]

    def test_a_bin_download_takes_the_suffix_its_bytes_declare(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index, (data, suffix) in enumerate(self.CASES):
                with self.subTest(suffix=suffix):
                    path = Path(tmp, f"link-{index}-Файл.bin")
                    path.write_bytes(data)
                    self.assertEqual(links.name_by_content(path), path.with_suffix(suffix))
                    self.assertTrue(path.with_suffix(suffix).is_file())

    def test_a_named_file_keeps_its_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "link-1-x.xlsx")
            path.write_bytes(b"%PDF-")
            self.assertEqual(links.name_by_content(path), path)


if __name__ == "__main__":
    unittest.main()
