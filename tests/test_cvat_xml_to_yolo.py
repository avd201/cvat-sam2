"""cvat_xml_to_yolo: conversion rules, layout parity with CVAT's own export, and loud reporting of what is skipped."""
import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cli"))

import cvat_xml_to_yolo as c  # noqa: E402

# Polygons of tracks 0 frames 0 and 1, copied from a real job-12 export (1280x720).
USER_P0 = "0.00,341.00;0.00,392.00;3.00,393.00;7.00,402.00;14.00,410.00;32.00,422.00;39.00,440.00;39.00,449.00;35.00,452.00;16.00,454.00;10.00,449.00;0.00,446.00;0.00,453.00;4.00,454.00;10.00,460.00;16.00,476.00;32.00,490.00;38.00,491.00;42.00,488.00;41.00,476.00;44.00,473.00;60.00,474.00;62.00,476.00;60.00,500.00;66.00,505.00;73.00,506.00;80.00,495.00;79.00,483.00;84.00,465.00;97.00,458.00;105.00,450.00;116.00,446.00;123.00,428.00;131.00,420.00;126.00,419.00;111.00,423.00;91.00,405.00;84.00,393.00;76.00,366.00;71.00,361.00;54.00,356.00;22.00,355.00;17.00,352.00;13.00,344.00;8.00,341.00"
USER_P1 = "0.00,340.00;0.00,388.00;4.00,390.00;7.00,398.00;16.00,410.00;31.00,418.00;41.00,440.00;41.00,448.00;34.00,452.00;16.00,453.00;0.00,444.00;0.00,452.00;3.00,452.00;8.00,457.00;21.00,479.00;35.00,489.00;42.00,488.00;43.00,476.00;47.00,472.00;61.00,472.00;63.00,474.00;62.00,500.00;73.00,505.00;80.00,496.00;79.00,483.00;83.00,467.00;88.00,462.00;97.00,458.00;106.00,449.00;115.00,446.00;124.00,427.00;132.00,420.00;112.00,422.00;102.00,415.00;86.00,395.00;81.00,384.00;77.00,366.00;68.00,358.00;50.00,352.00;32.00,356.00;27.00,355.00;13.00,342.00"
# A real polygon (job 10) and the line CVAT v2.78.0's own "Ultralytics YOLO Segmentation 1.0" export wrote for it.
CVAT_GOLDEN_POINTS = "544.00,288.00;538.00,288.00;532.00,293.00;531.00,303.00;526.00,316.00;507.00,329.00;503.00,325.00;498.00,328.00;493.00,326.00;487.00,327.00;486.00,322.00;482.00,319.00;476.00,301.00;476.00,294.00;473.00,290.00;466.00,290.00;456.00,305.00;455.00,322.00;462.00,350.00;462.00,372.00;459.00,393.00;456.00,400.00;454.00,426.00;442.00,431.00;440.00,438.00;449.00,438.00;448.00,435.00;453.00,433.00;458.00,440.00;459.00,446.00;467.00,453.00;490.00,454.00;495.00,457.00;497.00,462.00;505.00,462.00;502.00,466.00;497.00,464.00;497.00,476.00;502.00,482.00;509.00,478.00;513.00,464.00;531.00,450.00;536.00,443.00;536.00,433.00;533.00,423.00;532.00,381.00;544.00,360.00;546.00,346.00;553.00,339.00;555.00,321.00;549.00,291.00"
CVAT_GOLDEN_LINE = "0 0.425000 0.400000 0.420312 0.400000 0.415625 0.406944 0.414844 0.420833 0.410938 0.438889 0.396094 0.456944 0.392969 0.451389 0.389062 0.455556 0.385156 0.452778 0.380469 0.454167 0.379688 0.447222 0.376563 0.443056 0.371875 0.418056 0.371875 0.408333 0.369531 0.402778 0.364063 0.402778 0.356250 0.423611 0.355469 0.447222 0.360938 0.486111 0.360938 0.516667 0.358594 0.545833 0.356250 0.555556 0.354687 0.591667 0.345313 0.598611 0.343750 0.608333 0.350781 0.608333 0.350000 0.604167 0.353906 0.601389 0.357812 0.611111 0.358594 0.619444 0.364844 0.629167 0.382812 0.630556 0.386719 0.634722 0.388281 0.641667 0.394531 0.641667 0.392188 0.647222 0.388281 0.644444 0.388281 0.661111 0.392188 0.669444 0.397656 0.663889 0.400781 0.644444 0.414844 0.625000 0.418750 0.615278 0.418750 0.601389 0.416406 0.587500 0.415625 0.529167 0.425000 0.500000 0.426563 0.480556 0.432031 0.470833 0.433594 0.445833 0.428906 0.404167"


def xml(tracks: str, start=0, stop=150, size=(1280, 720), labels=("chicken",)) -> str:
    lab = "".join(f"<label><name>{n}</name><color>#fff</color><type>any</type><attributes></attributes></label>" for n in labels)
    return (f'<?xml version="1.0" encoding="utf-8"?><annotations><version>1.1</version><meta><job><id>12</id>'
            f'<size>{stop - start + 1}</size><mode>interpolation</mode><start_frame>{start}</start_frame>'
            f'<stop_frame>{stop}</stop_frame><labels>{lab}</labels></job>'
            f'<original_size><width>{size[0]}</width><height>{size[1]}</height></original_size></meta>{tracks}</annotations>')


def poly(frame, pts, outside=0, occluded=0):
    return (f'<polygon frame="{frame}" keyframe="1" outside="{outside}" occluded="{occluded}" points="{pts}" z_order="0">'
            "</polygon>")


def run(tmp_path, text, **kw):
    root = c.ET.fromstring(text)
    out = tmp_path / "out"
    stats = c.convert(root, out, **kw)
    return out, stats


def labels(out, frame):
    return (out / "labels" / "train" / f"frame_{frame:06d}.txt").read_text().splitlines()


def test_segmentation_lines_are_normalised_polygons(tmp_path):
    out, stats = run(tmp_path, xml(f'<track id="0" label="chicken" source="semi-auto">{poly(0, USER_P0)}{poly(1, USER_P1)}</track>'))
    l0 = labels(out, 0)[0].split()
    assert l0[0] == "0" and len(l0) == 1 + 2 * 45                      # class + 45 vertices
    assert l0[1:5] == ["0.000000", f"{341 / 720:.6f}", "0.000000", f"{392 / 720:.6f}"]   # (0,341) (0,392) / (1280,720)
    assert l0[5:7] == [f"{3 / 1280:.6f}", f"{393 / 720:.6f}"]
    assert len(labels(out, 1)[0].split()) == 1 + 2 * 42
    assert stats["objects_written"] == 2 and stats["label_files"] == 2


def test_detection_is_the_bounding_box_of_the_polygon(tmp_path):
    out, _ = run(tmp_path, xml(f'<track id="0" label="chicken">{poly(0, USER_P0)}</track>'), fmt_kind="det")
    # x spans 0..131, y spans 341..506 in the job-12 polygon
    assert labels(out, 0) == [f"0 {65.5 / 1280:.6f} {(341 + 506) / 2 / 720:.6f} {131 / 1280:.6f} {165 / 720:.6f}"]


def test_matches_cvats_own_segmentation_export_byte_for_byte(tmp_path):
    out, _ = run(tmp_path, xml(f'<track id="0" label="chicken">{poly(0, CVAT_GOLDEN_POINTS)}</track>'))
    assert labels(out, 0) == [CVAT_GOLDEN_LINE]


def test_layout_mirrors_cvat_export(tmp_path):
    out, stats = run(tmp_path, xml(f'<track id="0" label="chicken">{poly(2, USER_P0)}</track>', start=0, stop=4, labels=("chicken", "body")))
    assert (out / "data.yaml").read_text() == "names:\n  0: chicken\n  1: body\npath: .\ntrain: train.txt\n"
    assert (out / "train.txt").read_text().splitlines() == [f"data/images/train/frame_{i:06d}.png" for i in range(5)]   # every frame of the job
    assert sorted(p.name for p in (out / "labels" / "train").iterdir()) == ["frame_000002.txt"]                          # only frames with objects
    assert stats["frames_in_job"] == 5


def test_class_ids_follow_label_order_and_lines_are_sorted_by_track_id(tmp_path):
    t = (f'<track id="5" label="body">{poly(0, USER_P1)}</track><track id="2" label="chicken">{poly(0, USER_P0)}</track>')
    out, _ = run(tmp_path, xml(t, labels=("chicken", "body")), track_ids=True)
    ls = labels(out, 0)
    assert [l.split()[0] for l in ls] == ["0", "1"] and [l.split()[-1] for l in ls] == ["2", "5"]   # track 2 (chicken=0) first


def test_track_id_is_appended_only_on_request_and_is_the_cvat_track_id(tmp_path):
    t = f'<track id="7" label="chicken">{poly(0, USER_P0)}</track>'
    plain, _ = run(tmp_path / "a", xml(t))
    withid, _ = run(tmp_path / "b", xml(t), track_ids=True)
    assert len(labels(plain, 0)[0].split()) % 2 == 1 and len(labels(withid, 0)[0].split()) % 2 == 0   # odd = trainable polygon
    assert labels(withid, 0)[0] == labels(plain, 0)[0] + " 7"
    det, _ = run(tmp_path / "c", xml(t), fmt_kind="det", track_ids=True)
    assert len(labels(det, 0)[0].split()) == 6


def test_outside_frames_are_skipped_and_reported(tmp_path):
    t = f'<track id="0" label="chicken">{poly(0, USER_P0)}{poly(1, USER_P1)}{poly(2, USER_P1, outside=1)}</track>'
    out, stats = run(tmp_path, xml(t))
    assert sorted(p.name for p in (out / "labels" / "train").iterdir()) == ["frame_000000.txt", "frame_000001.txt"]
    assert stats["outside_frames_skipped"] == 1 and stats["gap_frames_not_exported"] == 0


def test_object_can_reappear_after_an_outside_marker_without_a_gap_warning(tmp_path):
    t = f'<track id="0" label="chicken">{poly(0, USER_P0)}{poly(1, USER_P0, outside=1)}{poly(6, USER_P1)}</track>'
    out, stats = run(tmp_path, xml(t))
    assert [p.name for p in sorted((out / "labels" / "train").iterdir())] == ["frame_000000.txt", "frame_000006.txt"]
    assert stats["gap_frames_not_exported"] == 0                   # frames 2-5 are "outside", not interpolated


def test_frames_between_two_keyframes_are_reported_not_silently_dropped(tmp_path):
    t = f'<track id="0" label="chicken">{poly(0, USER_P0)}{poly(4, USER_P1)}</track>'
    out, stats = run(tmp_path, xml(t))
    assert stats["gap_frames_not_exported"] == 3                   # frames 1,2,3 would be interpolated by CVAT
    assert sorted(p.name for p in (out / "labels" / "train").iterdir()) == ["frame_000000.txt", "frame_000004.txt"]


def test_occluded_is_kept_unless_asked(tmp_path):
    t = f'<track id="0" label="chicken">{poly(0, USER_P0, occluded=1)}{poly(1, USER_P1)}</track>'
    kept, s1 = run(tmp_path / "a", xml(t))
    dropped, s2 = run(tmp_path / "b", xml(t), skip_occluded=True)
    assert s1["objects_written"] == 2 and s2["objects_written"] == 1 and s2["occluded_skipped"] == 1


def test_masks_and_other_shapes_are_counted_as_skipped(tmp_path):
    t = (f'<track id="0" label="chicken">{poly(0, USER_P0)}<mask frame="1" keyframe="1" outside="0" occluded="0" rle="1,2" left="0" top="0" width="1" height="1" z_order="0"></mask>'
         '<polyline frame="2" keyframe="1" outside="0" occluded="0" points="0,0;5,5" z_order="0"></polyline></track>')
    _, stats = run(tmp_path, xml(t))
    assert stats["skipped_mask"] == 1 and stats["skipped_polyline"] == 1 and stats["objects_written"] == 1


def test_boxes_become_polygons_or_boxes_and_rotated_boxes_are_skipped(tmp_path):
    t = ('<track id="0" label="chicken"><box frame="0" keyframe="1" outside="0" occluded="0" xtl="128" ytl="72" xbr="256" ybr="144" z_order="0"></box>'
         '<box frame="1" keyframe="1" outside="0" occluded="0" xtl="0" ytl="0" xbr="10" ybr="10" rotation="30" z_order="0"></box></track>')
    seg, s = run(tmp_path / "a", xml(t))
    assert labels(seg, 0) == ["0 0.100000 0.100000 0.200000 0.100000 0.200000 0.200000 0.100000 0.200000"] and s["skipped_rotated_box"] == 1
    det, _ = run(tmp_path / "b", xml(t), fmt_kind="det")
    assert labels(det, 0) == ["0 0.150000 0.150000 0.100000 0.100000"]


def test_coordinates_are_clamped_to_the_frame(tmp_path):
    out, _ = run(tmp_path, xml(f'<track id="0" label="chicken">{poly(0, "-5,-5;1290,-5;1290,730")}</track>'))
    assert labels(out, 0) == ["0 0.000000 0.000000 1.000000 0.000000 1.000000 1.000000"]


def test_errors_are_clear(tmp_path):
    with pytest.raises(c.ConversionError, match="not declared"):
        run(tmp_path, xml(f'<track id="0" label="cow">{poly(0, USER_P0)}</track>'))
    with pytest.raises(c.ConversionError, match="no <track>"):
        run(tmp_path, xml(""))
    with pytest.raises(c.ConversionError, match="original_size"):
        c.convert(c.ET.fromstring("<annotations><meta><job><labels/></job></meta></annotations>"), tmp_path / "x")


def test_cli_reads_a_zip_and_reports_skips_on_stderr(tmp_path, capsys):
    t = f'<track id="0" label="chicken">{poly(0, USER_P0)}{poly(1, USER_P1, outside=1)}</track>'
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("annotations.xml", xml(t))
    assert c.main([str(z), "-o", str(tmp_path / "o")]) == 0
    cap = capsys.readouterr()
    assert "Wrote 1 objects from 1 tracks into 1 label files (151 frames in the job)" in cap.out
    assert "outside_frames_skipped: 1" in cap.err


def test_cli_missing_or_broken_input_is_a_clean_error_not_a_traceback(tmp_path, capsys):
    assert c.main([str(tmp_path / "missing.xml"), "-o", str(tmp_path / "o")]) == 1
    assert "cannot read" in capsys.readouterr().err
    bad = tmp_path / "bad.xml"
    bad.write_text("<annotations>")
    assert c.main([str(bad), "-o", str(tmp_path / "o")]) == 1
    assert "not valid XML" in capsys.readouterr().err
    notzip = tmp_path / "x.zip"
    notzip.write_text("nope")
    assert c.main([str(notzip), "-o", str(tmp_path / "o")]) == 1
    assert "not a valid zip" in capsys.readouterr().err


# ---- region of interest + sidecar ---------------------------------------------------------------------------------
import json  # noqa: E402


def polyline(frame, pts, outside=0):
    return (f'<polyline frame="{frame}" keyframe="1" outside="{outside}" occluded="0" points="{pts}" z_order="0"></polyline>')


ROI_TRACK = f'<track id="9" label="roi_line">{polyline(0, "640.00,0.00;640.00,720.00;100.00,300.00")}</track>'


def test_roi_lines_are_not_objects_and_are_written_to_roi_json(tmp_path):
    t = f'<track id="0" label="chicken">{poly(0, USER_P0)}</track>' + ROI_TRACK
    out, stats = run(tmp_path, xml(t, labels=("roi_line", "chicken")))
    assert (out / "data.yaml").read_text() == "names:\n  0: chicken\npath: .\ntrain: train.txt\n"        # roi_line is not a class
    assert labels(out, 0)[0].split()[0] == "0"                                                           # chicken renumbered to 0
    assert stats["objects_written"] == 1 and stats["roi_lines"] == 1 and stats["tracks"] == 1
    roi = json.loads((out / "roi.json").read_text())
    assert roi["lines"] == [[640.0, 0.0, 640.0, 720.0, 100.0, 300.0]] and roi["width"] == 1280 and roi["height"] == 720


def test_no_roi_json_without_roi_lines(tmp_path):
    out, stats = run(tmp_path, xml(f'<track id="0" label="chicken">{poly(0, USER_P0)}</track>'))
    assert not (out / "roi.json").exists() and stats["roi_lines"] == 0


def test_roi_line_that_moves_is_reported_and_the_first_position_is_used(tmp_path):
    moving = f'<track id="9" label="roi_line">{polyline(0, "0,0;10,0")}{polyline(5, "0,1;10,1")}</track>'
    out, stats = run(tmp_path, xml(f'<track id="0" label="chicken">{poly(0, USER_P0)}</track>' + moving, labels=("chicken", "roi_line")))
    assert json.loads((out / "roi.json").read_text())["lines"] == [[0.0, 0.0, 10.0, 0.0]]
    assert stats["roi_line_moves_between_keyframes"] == 1


def test_a_roi_line_with_the_wrong_number_of_points_is_an_error(tmp_path):
    bad = f'<track id="9" label="roi_line">{polyline(0, "0,0;10,0;5,5;1,1")}</track>'
    with pytest.raises(c.ConversionError, match="needs 2 or 3"):
        run(tmp_path, xml(f'<track id="0" label="chicken">{poly(0, USER_P0)}</track>' + bad, labels=("chicken", "roi_line")))


def test_sidecar_is_line_aligned_with_the_label_file_and_label_files_stay_trainable(tmp_path):
    t = (f'<track id="5" label="chicken">{poly(0, USER_P1, occluded=1)}</track>'
         f'<track id="2" label="chicken">{poly(0, USER_P0)}{poly(1, USER_P0)}</track>')
    out, _ = run(tmp_path, xml(t), sidecar=True)
    side = (out / "tracks" / "train" / "frame_000000.txt").read_text().splitlines()
    assert side == ["2 0", "5 1"]                                         # sorted by track id, "track_id occluded"
    assert len(labels(out, 0)) == len(side) and (out / "tracks" / "train" / "frame_000001.txt").read_text() == "2 0\n"
    assert all(len(l.split()) % 2 == 1 for l in labels(out, 0))            # still odd: plain Ultralytics polygons, no id inside
    plain, _ = run(tmp_path / "p", xml(t))
    assert not (plain / "tracks").exists() and labels(plain, 0) == labels(out, 0)
