"""geo 模块单元测试：等面积投影、互斥检查、洞与共边语义。"""
from __future__ import annotations

from shapely.geometry import box, shape

from app import geo
from tests.conftest import (
    feature_collection,
    rect_geojson,
    rect_with_hole,
)


def test_equal_area_projection_matches_geodetic_area():
    # 等面积投影下，投影面积应等于大地测量得到的地表真实面积
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    g1 = shape(rect_geojson("A", 100.0, 30.0, 101.0, 31.0)["geometry"])
    g2 = shape(rect_geojson("B", 102.0, 35.0, 103.0, 36.0)["geometry"])
    crs = geo.build_equal_area_crs([g1, g2])
    p1, p2 = geo.project_geoms([g1, g2], crs)
    assert crs.to_dict()["proj"] == "aea"
    assert crs.axis_info[0].unit_name == "metre"

    for geom_wgs, geom_proj in ((g1, p1), (g2, p2)):
        geod_area, _ = geod.geometry_area_perimeter(geom_wgs)
        # 相对误差 < 0.1%（Albers 在小区域内面积失真极小）
        assert abs(geom_proj.area - abs(geod_area)) / abs(geod_area) < 1e-3

    # 不同纬度的 1° 方格地表面积确实不同（高纬更小），投影如实保留该差异
    assert p2.area < p1.area


def test_find_overlapping_pairs_detects_overlap_but_ignores_edge():
    touching = [("A", box(0, 0, 1, 1)), ("B", box(1, 0, 2, 1))]
    assert geo.find_overlapping_pairs(touching) == []

    overlapping = [("A", box(0, 0, 2, 2)), ("B", box(1, 1, 3, 3))]
    pairs = geo.find_overlapping_pairs(overlapping)
    assert pairs == [("A", "B")]


def test_positive_overlaps_measured_in_projected_square_metres():
    from shapely.geometry import shape as shp

    # 两个约 1km 见方、互相侵入约 500m 的乡镇（经纬度 WGS84）
    a = shp(
        {
            "type": "Polygon",
            "coordinates": [[[110.0, 30.0], [110.01, 30.0],
                             [110.01, 30.01], [110.0, 30.01], [110.0, 30.0]]],
        }
    )
    b = shp(
        {
            "type": "Polygon",
            "coordinates": [[[110.005, 30.005], [110.015, 30.005],
                             [110.015, 30.015], [110.005, 30.015],
                             [110.005, 30.005]]],
        }
    )
    crs = geo.build_equal_area_crs([a, b])
    pa, pb = geo.project_geoms([a, b], crs)
    found = geo.find_positive_overlaps([("A", pa), ("B", pb)])
    assert len(found) == 1
    # 交叠约 500m x 500m ≈ 250,000 m2，数量级合理
    assert 2.0e5 < found[0]["overlap_area_m2"] < 3.0e5

    # 仅共边的两个面在投影下也不产生正面积交叠
    edge_b = shp(
        {
            "type": "Polygon",
            "coordinates": [[[110.01, 30.0], [110.02, 30.0],
                             [110.02, 30.01], [110.01, 30.01],
                             [110.01, 30.0]]],
        }
    )
    (peb,) = geo.project_geoms([edge_b], crs)
    assert geo.find_positive_overlaps([("A", pa), ("B", peb)]) == []


def test_hole_polygon_area_excludes_hole():
    parsed = geo.parse_feature_collection(
        feature_collection(rect_with_hole("H1", 0, 0, 4, 4, 1, 1, 2, 2))
    )
    tid, geom = parsed[0]
    assert tid == "H1"
    # 外环 16 - 洞 1 = 15（经纬度平面面积）
    assert abs(geom.area - 15.0) < 1e-9


def test_parse_rejects_duplicate_and_invalid():
    fc = feature_collection(
        rect_geojson("T1", 0, 0, 1, 1),
        rect_geojson("T1", 1, 0, 2, 1),
    )
    try:
        geo.parse_feature_collection(fc)
        raise AssertionError("应拒绝重复乡镇号")
    except geo.GeoValidationError as exc:
        assert "重复" in str(exc)


def test_compute_weights_ratios_sum_and_uncovered():
    old = [("O1", box(0, 0, 4, 4))]
    new = [("N1", box(0, 0, 2, 4)), ("N2", box(2, 0, 4, 4))]
    weights = geo.compute_weights(old, new)
    w = weights[0]
    assert len(w["overlaps"]) == 2
    assert abs(sum(o["ratio_of_old"] for o in w["overlaps"]) - 1.0) < 1e-9
    assert w["uncovered_area_m2"] == 0.0

    # 部分覆盖场景
    new_partial = [("N1", box(0, 0, 2, 2))]
    w2 = geo.compute_weights(old, new_partial)[0]
    assert abs(w2["overlaps"][0]["ratio_of_old"] - 0.25) < 1e-9
    assert abs(w2["uncovered_area_m2"] - 12.0) < 1e-9
