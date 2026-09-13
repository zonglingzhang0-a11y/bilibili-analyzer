import os

from report_writer import _md_cell, _md_img, _resolve_asset


def test_md_img_is_relative_to_report_and_escaped():
    out_dir = os.path.join(".", "bilibili_output", "run", "123_the real title")
    line = _md_img(os.path.join(out_dir, "charts", "a (1).png"), "图", out_dir)
    assert line == "![图](charts/a%20%281%29.png)\n"


def test_resolve_asset_prefers_file_in_report_dir(tmp_path):
    (tmp_path / "frames").mkdir()
    (tmp_path / "frames" / "frame_1m00s.jpg").write_bytes(b"x")
    stale = r".\bilibili_output\old_run\123_x\frames\frame_1m00s.jpg"
    assert _resolve_asset(stale, str(tmp_path), "frames") == \
        os.path.join(str(tmp_path), "frames", "frame_1m00s.jpg")
    assert _resolve_asset("missing.jpg", str(tmp_path)) is None


def test_md_cell_escapes_pipes_and_newlines():
    assert _md_cell("A|B\nC") == "A\\|B C"
