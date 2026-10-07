"""Image files (png/jpg) are read with the configured OCR engine; without OCR or text they fail clearly. Images inside
mails (signature logos) are deliberately NOT processed. The web upload accepts them only when the bytes are an image."""
import pytest

from llmwiki.ingest.pdf import FakeOcrEngine
from llmwiki.pipeline import notes
from llmwiki.pipeline.ocr_command import PNG_1X1
from llmwiki.pipeline.run import SUPPORTED
from test_pipeline_support import make_env, make_eml
from test_web_support import make_web

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


@pytest.mark.parametrize("name,data", [("영수증.png", PNG_1X1), ("scan.jpg", JPEG), ("scan.JPEG", JPEG)])
def test_image_is_read_with_ocr(env, name, data):
    assert "." + name.lower().rsplit(".", 1)[1] in SUPPORTED
    env.drop("dept-a", name, data)
    ocr = FakeOcrEngine({0: "청구서 합계 4,500원"})
    res = env.run_all(ocr=ocr)
    assert res[0].status == "done" and ocr.calls == [0]
    raw = (env.settings.data_dir / res[0].raw_paths[0]).read_text(encoding="utf-8")
    assert "청구서 합계 4,500원" in raw


def test_image_without_ocr_fails_clearly(env):
    f = env.drop("dept-a", "scan.png", PNG_1X1)
    res = env.process(f, "dept-a")
    assert res.status == "failed" and notes.UNREADABLE in res.error


def test_image_with_no_text_fails_instead_of_creating_an_empty_article(env):
    f = env.drop("dept-a", "blank.png", PNG_1X1)
    res = env.process(f, "dept-a", ocr=FakeOcrEngine(default="   "))
    assert res.status == "failed" and notes.UNREADABLE in res.error
    assert env.store.article_paths() == []


def test_file_that_is_not_an_image_is_corrupt_not_ocr_d(env):
    f = env.drop("dept-a", "fake.png", b"this is plain text, not a png")
    ocr = FakeOcrEngine({0: "x"})
    res = env.process(f, "dept-a", ocr=ocr)
    assert res.status == "failed" and notes.CORRUPT in res.error and ocr.calls == []


def test_images_inside_a_mail_are_not_processed(env):
    eml = make_eml("로고 포함 메일", "본문 매출 100", [("logo.png", PNG_1X1), ("detail.txt", "# 상세\n\n내용 45".encode())])
    env.drop("dept-a", "mail.eml", eml)
    ocr = FakeOcrEngine({0: "로고 글자"})
    res = env.run_all(ocr=ocr)
    assert res[0].status == "done" and ocr.calls == []  # the logo never reached OCR
    assert len(res[0].raw_paths) == 2  # mail + the text attachment only


def test_web_upload_accepts_real_images_only(tmp_path):
    w = make_web(tmp_path)
    c = w.client()
    c.login("ua")
    h = {"X-CSRF-Token": c.csrf}
    ok = c.request("POST", "/api/upload", query="space=dept-a&filename=scan.png", body=PNG_1X1, headers=h)
    ok2 = c.request("POST", "/api/upload", query="space=dept-a&filename=scan.jpg", body=JPEG, headers=h)
    bad = c.request("POST", "/api/upload", query="space=dept-a&filename=evil.png", body=b"<html>not an image</html>", headers=h)
    assert ok.status == 201 and ok2.status == 201
    assert bad.status == 400 and bad.json() == {"error": "bad_content"}
