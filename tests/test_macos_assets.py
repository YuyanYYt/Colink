"""Source-level guards for the native UI and the shared smooth vector mark."""

import math
import plistlib
import re
import runpy
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
SVG = "{http://www.w3.org/2000/svg}"
SAMPLE_STEPS = 160


def connection_path(name: str) -> tuple[ET.Element, ET.Element]:
    root = ET.parse(WORKSPACE / "macos/assets" / name).getroot()
    link = root.find(f".//{SVG}path[@id='connection']")
    assert link is not None
    return root, link


def path_segments(path: str) -> list[tuple[str, list[tuple[float, float]]]]:
    tokens = re.findall(r"[MCLQ]|-?\d+(?:\.\d+)?", path)
    assert tokens.pop(0) == "M"
    point = (float(tokens.pop(0)), float(tokens.pop(0)))
    segments = []
    while tokens:
        command = tokens.pop(0)
        length = {"L": 2, "Q": 4, "C": 6}[command]
        numbers = [float(tokens.pop(0)) for _ in range(length)]
        polygon = [point] + list(zip(numbers[::2], numbers[1::2], strict=True))
        segments.append((command, polygon))
        point = polygon[-1]
    return segments


def test_connection_is_one_stroke_with_continuous_tangents():
    _, link = connection_path("logo.svg")
    tangents = []
    for _, polygon in path_segments(link.attrib["d"]):
        start, first, penultimate, end = polygon[0], polygon[1], polygon[-2], polygon[-1]
        incoming = (first[0] - start[0], first[1] - start[1])
        outgoing = (end[0] - penultimate[0], end[1] - penultimate[1])
        tangents.append((incoming, outgoing))
    assert len(tangents) == 9
    for previous, current in zip(tangents, tangents[1:], strict=False):
        left, right = previous[1], current[0]
        norm = math.hypot(*left) * math.hypot(*right)
        assert norm > 0
        assert abs(left[0] * right[1] - left[1] * right[0]) / norm < 1e-10
        assert (left[0] * right[0] + left[1] * right[1]) / norm > 0.999999


def test_menu_and_brand_share_the_same_continuous_mark():
    logo_root, logo = connection_path("logo.svg")
    menu_root, menu = connection_path("menubar.svg")
    assert logo.attrib["d"] == menu.attrib["d"]
    for root in (logo_root, menu_root):
        assert any(
            group.attrib.get("stroke-linecap") == "round"
            and group.attrib.get("stroke-linejoin") == "round"
            for group in root.iter(f"{SVG}g")
        )


def test_main_panel_keeps_controls_without_technical_metrics():
    panel = (WORKSPACE / "macos/CodeConnect/Panel.swift").read_text()
    panel = panel.split("private struct ConnectionSetupView:")[0]
    for required in ("选择文件夹", 'Label("启动"', 'Label("关闭"'):
        assert required in panel
    for removed in ("快照版本", "已收录文件", "私有隧道", "LOCAL ·", "没有后台采集"):
        assert removed not in panel
    assert "controller.phase == .failed || controller.phase == .external" in panel


def test_app_is_discoverable_but_runs_as_a_menu_bar_utility():
    builder = (WORKSPACE / "macos/build.py").read_text()
    entry = (WORKSPACE / "macos/CodeConnect/main.swift").read_text()
    assert '"LSUIElement": True' in builder
    assert '"LSApplicationCategoryType": "public.app-category.developer-tools"' in builder
    assert "LSRegisterURL(Bundle.main.bundleURL as CFURL, true)" in entry
    assert "application.setActivationPolicy(.accessory)" in entry


def test_build_reuses_one_workspace_cache_across_output_directories(tmp_path, monkeypatch):
    builder = runpy.run_path(str(WORKSPACE / "macos/build.py"))["build"]
    workspace = tmp_path / "workspace"
    assets = workspace / "macos/assets"
    assets.mkdir(parents=True)
    for name in ("logo.svg", "menubar.svg"):
        shutil.copy2(WORKSPACE / "macos/assets" / name, assets / name)
    client = workspace / ".artifacts/tools/tunnel-client-v0.0.15/extracted/tunnel-client"
    client.parent.mkdir(parents=True)
    client.touch()
    commands = []
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/uv")
    monkeypatch.setattr("subprocess.run", lambda args, **_: commands.append(args))
    for release in ("first", "second"):
        output = workspace / "dist" / release / "Colink.app"
        assert builder(workspace, output, "node", "sharp") == output
        assert not (output.parent / "swift-module-cache").exists()
        with (output / "Contents/Info.plist").open("rb") as stream:
            metadata = plistlib.load(stream)
        assert metadata["CFBundleName"] == metadata["CFBundleDisplayName"] == "Colink"
        assert metadata["CFBundleIdentifier"] == "local.codeconnect.menubar"
    compilations = [args for args in commands if args[:2] == ["xcrun", "swiftc"]]
    assert len(compilations) == 2
    for args in compilations:
        assert args[args.index("-module-cache-path") + 1] == str(workspace / "swift-module-cache")


def test_colink_branding_preserves_the_connection_and_preferences_identity():
    panel = (WORKSPACE / "macos/CodeConnect/Panel.swift").read_text()
    entry = (WORKSPACE / "macos/CodeConnect/main.swift").read_text()
    runtime = (WORKSPACE / "macos/CodeConnect/Runtime.swift").read_text()
    assert 'Text("Colink")' in panel
    assert 'accessibilityLabel("Colink SVG 标志")' in panel
    assert '"退出 Colink"' in entry
    assert '["run", "--locked", "--no-sync", "colink"]' in runtime
    assert 'string(forKey: "SelectedFolder")' in runtime


def test_complete_mark_is_center_symmetric_about_the_canvas_center():
    for name in ("logo.svg", "menubar.svg"):
        root, link = connection_path(name)
        x, y, width, height = map(float, root.attrib["viewBox"].split())
        assert (x + width / 2, y + height / 2) == (128, 128)
        segments = path_segments(link.attrib["d"])
        for (kind, polygon), (other_kind, other) in zip(segments, reversed(segments), strict=True):
            assert kind == other_kind
            for point, opposite in zip(polygon, reversed(other), strict=True):
                assert math.isclose(point[0] + opposite[0], 256, abs_tol=1e-8)
                assert math.isclose(point[1] + opposite[1], 256, abs_tol=1e-8)
        right = root.find(f".//{SVG}use[@id='bracket-right']")
        assert right is not None
        assert right.attrib["href"] == "#bracket-left"
        assert right.attrib["transform"] == "rotate(180 128 128)"
        for tile in root.iter(f"{SVG}rect"):
            assert 2 * float(tile.attrib["x"]) + float(tile.attrib["width"]) == 256
            assert 2 * float(tile.attrib["y"]) + float(tile.attrib["height"]) == 256


def sampled_points(path: str) -> list[tuple[float, float]]:
    samples = []
    for _, polygon in path_segments(path):
        degree = len(polygon) - 1
        for step in range(SAMPLE_STEPS + 1):
            t = step / SAMPLE_STEPS
            weights = [
                math.comb(degree, i) * (1 - t) ** (degree - i) * t**i for i in range(degree + 1)
            ]
            samples.append(
                tuple(
                    sum(w * point[axis] for w, point in zip(weights, polygon, strict=True))
                    for axis in (0, 1)
                )
            )
    return samples


def sampling_error_bound(path: str) -> float:
    # A Bezier derivative is a convex combination of degree * control edges.
    # Each curve point is at most half a sample step away in parameter space.
    max_speed = max(
        (len(polygon) - 1) * math.hypot(b[0] - a[0], b[1] - a[1])
        for _, polygon in path_segments(path)
        for a, b in zip(polygon, polygon[1:], strict=False)
    )
    return max_speed / (2 * SAMPLE_STEPS)


def test_brackets_have_equal_clearance_without_touching_the_link():
    for name in ("logo.svg", "menubar.svg"):
        root, link = connection_path(name)
        bracket = root.find(f".//{SVG}path[@id='bracket-left']")
        group = root.find(f"{SVG}g")
        assert bracket is not None and group is not None
        curve = sampled_points(link.attrib["d"])
        left = sampled_points(bracket.attrib["d"])
        right = [(256 - x, 256 - y) for x, y in left]
        distances = [
            min(math.hypot(a[0] - b[0], a[1] - b[1]) for a in side for b in curve)
            for side in (left, right)
        ]
        stroke_radius = (
            float(group.attrib["stroke-width"])
            + float(link.attrib.get("stroke-width", group.attrib["stroke-width"]))
        ) / 2
        assert math.isclose(*distances, abs_tol=1e-8)
        error = sampling_error_bound(link.attrib["d"]) + sampling_error_bound(bracket.attrib["d"])
        # Subtract the sampling uncertainty to bound the actual curve clearance.
        assert min(distances) - stroke_radius - error > 6


def test_brand_colors_are_also_center_symmetric():
    root, _ = connection_path("logo.svg")
    tile = root.find(f".//{SVG}radialGradient[@id='tile']")
    link = root.find(f".//{SVG}linearGradient[@id='link']")
    assert tile is not None and link is not None
    assert (tile.attrib["cx"], tile.attrib["cy"]) == ("128", "128")
    assert float(link.attrib["x1"]) + float(link.attrib["x2"]) == 256
    assert float(link.attrib["y1"]) + float(link.attrib["y2"]) == 256
    stops = list(link)
    assert len(stops) == 3
    assert stops[0].attrib["stop-color"] == stops[-1].attrib["stop-color"]
    assert stops[1].attrib["offset"] == ".5"
