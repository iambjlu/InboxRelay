# relay-core/tests/test_heic_copy.py：Telegram 的 HEIC 搬到 Discord 時另附一份 JPEG
import asyncio
import io
import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as N

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ocr  # noqa: E402
import relay  # noqa: E402
from enrich import image  # noqa: E402


def make_heic(w=2400, h=1600, noisy=False) -> bytes:
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    im = Image.new("RGB", (w, h), (30, 120, 200))
    if noisy:
        px = im.load()
        random.seed(1)
        for _ in range(w * h // 3):
            px[random.randrange(w), random.randrange(h)] = (random.randrange(256), random.randrange(256), random.randrange(256))
    b = io.BytesIO()
    im.save(b, "HEIF")
    return b.getvalue()


class HeicCopyTest(unittest.TestCase):
    def test_names(self):
        self.assertTrue(image.is_heic_name("IMG_1.HEIC") and image.is_heic_name("a.heif"))
        self.assertFalse(image.is_heic_name("a.jpg"))
        self.assertTrue(image.is_converted_copy("IMG_1.HEIC.jpg") and not image.is_converted_copy("IMG_1.jpg"))

    def test_heic_to_jpeg_fits_limit(self):
        data = make_heic(noisy=True)
        big = image.heic_to_jpeg(data, 20 * 1024 * 1024)
        self.assertTrue(big.startswith(b"\xff\xd8"))
        small_limit = len(big) // 3
        small = image.heic_to_jpeg(data, small_limit)               # 要求縮到更小：降品質、再縮圖
        self.assertTrue(small is not None and len(small) <= small_limit)
        self.assertIsNone(image.heic_to_jpeg(b"not an image", 1 << 20))

    def test_plan_groups_heic_alone(self):
        def a(n, s):
            return N(file_name=n, file_size=s)
        groups = relay.plan_groups([a("1.jpg", 10), a("2.HEIC", 10), a("3.jpg", 10), a("4.jpg", 10)], 1000)
        self.assertEqual([[x.file_name for x in g] for g in groups], [["1.jpg"], ["2.HEIC"], ["3.jpg", "4.jpg"]])

    def test_add_jpeg_copy(self):
        async def go():
            r = relay.Relayer.__new__(relay.Relayer)
            data = make_heic()
            files = [("IMG_9.HEIC", data)]
            await r._add_jpeg_copy(files, "IMG_9.HEIC", data, 20 * 1024 * 1024)
            self.assertEqual([n for n, _ in files], ["IMG_9.HEIC", "IMG_9.HEIC.jpg"])
            self.assertLessEqual(len(files[0][1]) + len(files[1][1]), 20 * 1024 * 1024)
            files2 = [("a.jpg", b"x")]
            await r._add_jpeg_copy(files2, "a.jpg", b"x", 20 * 1024 * 1024)       # 不是 HEIC → 不加
            self.assertEqual(len(files2), 1)
            files3 = []
            await r._add_jpeg_copy(files3, "IMG_9.HEIC", data, len(data) + 1000)   # 沒空間 → 只留原檔
            self.assertEqual(files3, [])
        asyncio.run(go())

    def test_ocr_ignores_copy(self):
        atts = [N(filename="IMG_9.HEIC", content_type="image/heic"), N(filename="IMG_9.HEIC.jpg", content_type="image/jpeg")]
        rec = N(rec=N(key=(1, 1), tg_msg=1, filename="IMG_9.HEIC", type="file", origin="live"))
        pairs = ocr.match_attachments([rec], atts, "")
        self.assertEqual(pairs[0][1].filename, "IMG_9.HEIC")


if __name__ == "__main__":
    unittest.main()
