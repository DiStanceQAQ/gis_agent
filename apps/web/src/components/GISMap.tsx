import { useEffect, useRef, useState } from "react";
import Map from "ol/Map.js";
import View from "ol/View.js";
import { FullScreen, ScaleLine, defaults as defaultControls } from "ol/control.js";
import Feature from "ol/Feature.js";
import Polygon from "ol/geom/Polygon.js";
import TileLayer from "ol/layer/Tile.js";
import VectorLayer from "ol/layer/Vector.js";
import WebGLTileLayer from "ol/layer/WebGLTile.js";
import { fromLonLat, toLonLat, transformExtent } from "ol/proj.js";
import OSM from "ol/source/OSM.js";
import VectorSource from "ol/source/Vector.js";
import GeoTIFF from "ol/source/GeoTIFF.js";
import { Fill, Stroke, Style } from "ol/style.js";
import type { TaskDetail } from "../types";

type LayerControlState = {
  visible: boolean;
  opacity: number;
};

type GISMapProps = {
  task: TaskDetail | null;
  geotiffUrl?: string | null;
  layerControls: {
    basemap: LayerControlState;
    aoi: LayerControlState;
    ndvi: LayerControlState;
  };
  theme: "light" | "dark";
};

const DEFAULT_CENTER: [number, number] = [116.3913, 39.9075];

const AOI_STYLE = new Style({
  stroke: new Stroke({
    color: "#4fd1a3",
    width: 2,
    lineDash: [8, 6],
  }),
  fill: new Fill({
    color: "rgba(79, 209, 163, 0.08)",
  }),
});

function bboxToPolygon(bounds: number[]): Polygon {
  const [minX, minY, maxX, maxY] = bounds;
  return new Polygon([
    [
      fromLonLat([minX, minY]),
      fromLonLat([maxX, minY]),
      fromLonLat([maxX, maxY]),
      fromLonLat([minX, maxY]),
      fromLonLat([minX, minY]),
    ],
  ]);
}

function formatCoordinate(value?: [number, number] | null): string {
  if (!value) {
    return "Lon ---, Lat ---";
  }
  return `Lon ${value[0].toFixed(4)}, Lat ${value[1].toFixed(4)}`;
}

function formatMapDataset(task: TaskDetail | null): string {
  if (!task?.recommendation?.primary_dataset) {
    return "等待任务";
  }
  switch (task.recommendation.primary_dataset) {
    case "sentinel2":
      return "Sentinel-2";
    case "landsat89":
      return "Landsat 8/9";
    case "hls":
      return "HLS";
    default:
      return task.recommendation.primary_dataset;
  }
}

export default function GISMap({ task, geotiffUrl, layerControls, theme }: GISMapProps) {
  const mapElementRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<Map | null>(null);
  const baseLayerRef = useRef<TileLayer<OSM> | null>(null);
  const aoiLayerRef = useRef<VectorLayer<VectorSource> | null>(null);
  const rasterLayerRef = useRef<WebGLTileLayer | null>(null);

  const [cursorCoordinate, setCursorCoordinate] = useState<[number, number] | null>(null);
  const [zoomLevel, setZoomLevel] = useState<number>(8);

  useEffect(() => {
    if (!mapElementRef.current || mapRef.current) {
      return;
    }

    const baseLayer = new TileLayer({
      source: new OSM(),
      opacity: layerControls.basemap.opacity,
      visible: layerControls.basemap.visible,
    });
    const aoiLayer = new VectorLayer({
      source: new VectorSource(),
      style: AOI_STYLE,
      opacity: layerControls.aoi.opacity,
      visible: layerControls.aoi.visible,
    });

    const map = new Map({
      target: mapElementRef.current,
      layers: [baseLayer, aoiLayer],
      controls: defaultControls({ attribution: false }).extend([
        new FullScreen(),
        new ScaleLine({ bar: true, steps: 4, text: true, minWidth: 120 }),
      ]),
      view: new View({
        center: fromLonLat(DEFAULT_CENTER),
        zoom: 8,
        minZoom: 2,
        maxZoom: 18,
      }),
    });

    map.on("pointermove", (event) => {
      const coordinate = toLonLat(event.coordinate);
      setCursorCoordinate([coordinate[0], coordinate[1]]);
    });

    map.on("moveend", () => {
      const nextZoom = map.getView().getZoom();
      setZoomLevel(typeof nextZoom === "number" ? nextZoom : 8);
    });

    mapRef.current = map;
    baseLayerRef.current = baseLayer;
    aoiLayerRef.current = aoiLayer;

    return () => {
      map.setTarget(undefined);
      mapRef.current = null;
      baseLayerRef.current = null;
      aoiLayerRef.current = null;
      rasterLayerRef.current = null;
    };
  }, [layerControls.aoi.opacity, layerControls.aoi.visible, layerControls.basemap.opacity, layerControls.basemap.visible]);

  useEffect(() => {
    const baseLayer = baseLayerRef.current;
    const aoiLayer = aoiLayerRef.current;
    const rasterLayer = rasterLayerRef.current;
    if (!baseLayer || !aoiLayer) {
      return;
    }

    baseLayer.setVisible(layerControls.basemap.visible);
    baseLayer.setOpacity(layerControls.basemap.opacity);
    aoiLayer.setVisible(layerControls.aoi.visible);
    aoiLayer.setOpacity(layerControls.aoi.opacity);
    if (rasterLayer) {
      rasterLayer.setVisible(layerControls.ndvi.visible);
      rasterLayer.setOpacity(layerControls.ndvi.opacity);
    }
  }, [layerControls]);

  useEffect(() => {
    const map = mapRef.current;
    const aoiLayer = aoiLayerRef.current;
    if (!map || !aoiLayer) {
      return;
    }

    const source = aoiLayer.getSource();
    source?.clear();

    if (!task?.aoi_bbox_bounds || task.aoi_bbox_bounds.length !== 4) {
      return;
    }

    const feature = new Feature({
      geometry: bboxToPolygon(task.aoi_bbox_bounds),
    });
    source?.addFeature(feature);

    const extent = transformExtent(task.aoi_bbox_bounds, "EPSG:4326", "EPSG:3857");
    map.getView().fit(extent, {
      padding: [64, 64, 64, 64],
      duration: 500,
      maxZoom: 13,
    });
  }, [task?.aoi_bbox_bounds, task?.task_id]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }

    if (rasterLayerRef.current) {
      map.removeLayer(rasterLayerRef.current);
      rasterLayerRef.current = null;
    }

    if (!geotiffUrl) {
      return;
    }

    const rasterLayer = new WebGLTileLayer({
      source: new GeoTIFF({
        normalize: false,
        sources: [{ url: geotiffUrl }],
      }),
      visible: layerControls.ndvi.visible,
      opacity: layerControls.ndvi.opacity,
      style: {
        color: [
          "interpolate",
          ["linear"],
          ["band", 1],
          -0.2,
          ["color", 93, 56, 29, 0.2],
          0,
          ["color", 245, 236, 202, 0.72],
          0.2,
          ["color", 195, 220, 158, 0.78],
          0.45,
          ["color", 73, 145, 83, 0.9],
          0.7,
          ["color", 17, 83, 43, 1],
        ],
      },
    });

    map.getLayers().insertAt(1, rasterLayer);
    rasterLayerRef.current = rasterLayer;
  }, [geotiffUrl, layerControls.ndvi.opacity, layerControls.ndvi.visible, task?.task_id]);

  const hasRaster = Boolean(geotiffUrl);

  return (
    <div className={`map-stage map-theme-${theme}`}>
      <div ref={mapElementRef} className="map-canvas" />
      <div className="map-overlay-top">
        <div className="map-hud map-hud-left">
          <span className="map-hud-label">Workspace</span>
          <strong>{task?.aoi_name ?? task?.task_spec?.aoi_input ?? "等待研究区"}</strong>
          <p>{task?.actual_time_range ? `${task.actual_time_range.start} -> ${task.actual_time_range.end}` : "等待时间窗"}</p>
        </div>
        <div className="map-hud map-hud-right">
          <span className="map-hud-label">Raster</span>
          <strong>{formatMapDataset(task)}</strong>
          <p>{hasRaster ? "GeoTIFF layer ready" : "尚未生成 GeoTIFF"}</p>
        </div>
      </div>

      <div className={`map-legend ${hasRaster ? "" : "map-legend-disabled"}`}>
        <span className="map-hud-label">NDVI Legend</span>
        <div className="map-legend-scale" />
        <div className="map-legend-range">
          <span>-0.20</span>
          <span>0.70+</span>
        </div>
      </div>

      <div className="map-status-bar">
        <span>Zoom {zoomLevel.toFixed(1)}</span>
        <span>{task?.aoi_area_km2 ? `AOI ${task.aoi_area_km2.toFixed(1)} km²` : "AOI 未加载"}</span>
        <span>{formatCoordinate(cursorCoordinate)}</span>
        <span className="map-status-pill">{theme === "dark" ? "Dark" : "Light"} · EPSG:3857</span>
      </div>
    </div>
  );
}
