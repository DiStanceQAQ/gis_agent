import { useEffect, useRef, useState } from "react";
import OLMap from "ol/Map.js";
import View from "ol/View.js";
import { ScaleLine } from "ol/control.js";
import Feature from "ol/Feature.js";
import GeoJSON from "ol/format/GeoJSON.js";
import Polygon from "ol/geom/Polygon.js";
import { isEmpty as isExtentEmpty, type Extent } from "ol/extent.js";
import ImageLayer from "ol/layer/Image.js";
import TileLayer from "ol/layer/Tile.js";
import VectorLayer from "ol/layer/Vector.js";
import WebGLTileLayer from "ol/layer/WebGLTile.js";
import { fromLonLat, toLonLat, transformExtent } from "ol/proj.js";
import ImageStatic from "ol/source/ImageStatic.js";
import VectorSource from "ol/source/Vector.js";
import GeoTIFF from "ol/source/GeoTIFF.js";
import XYZ from "ol/source/XYZ.js";
import { Fill, Stroke, Style } from "ol/style.js";
import type { MapFocusRequest, UploadedLayerPreview } from "../hooks/useWorkbenchState";
import type { Artifact, TaskDetail } from "../types";

type LayerControlState = {
  visible: boolean;
  opacity: number;
};

type GISMapProps = {
  task: TaskDetail | null;
  layerControls: {
    basemap: LayerControlState;
    aoi: LayerControlState;
  };
  uploadedLayerPreviews: Record<string, UploadedLayerPreview>;
  uploadLayerControls: Record<string, LayerControlState>;
  focusRequest?: MapFocusRequest | null;
  artifacts: Artifact[];
  artifactLayerControls: Record<string, LayerControlState>;
  theme: "light" | "dark";
};

type DynamicMapLayer =
  | WebGLTileLayer
  | VectorLayer<VectorSource>
  | ImageLayer<ImageStatic>;

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

const UPLOADED_VECTOR_STYLE = new Style({
  stroke: new Stroke({
    color: "#3d94ff",
    width: 2,
  }),
  fill: new Fill({
    color: "rgba(61, 148, 255, 0.12)",
  }),
});

const ARTIFACT_VECTOR_STYLE = new Style({
  stroke: new Stroke({
    color: "#f59e0b",
    width: 2,
  }),
  fill: new Fill({
    color: "rgba(245, 158, 11, 0.12)",
  }),
});

const DEFAULT_DYNAMIC_CONTROL: LayerControlState = {
  visible: true,
  opacity: 1,
};

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

function isRasterArtifact(artifact: Artifact): boolean {
  return artifact.artifact_type === "geotiff" || artifact.mime_type.includes("tiff");
}

function isGeojsonArtifact(artifact: Artifact): boolean {
  if (artifact.artifact_type === "geojson" || artifact.artifact_type === "vector_geojson") {
    return true;
  }
  return artifact.mime_type.includes("geo+json") || artifact.mime_type.includes("application/json");
}

function removeDynamicLayers(map: OLMap, layerMap: Map<string, DynamicMapLayer>): void {
  for (const layer of layerMap.values()) {
    map.removeLayer(layer);
  }
  layerMap.clear();
}

function fitMapToExtent(map: OLMap, extent: Extent | null, maxZoom = 13): void {
  if (!extent || isExtentEmpty(extent)) {
    return;
  }
  map.getView().fit(extent, {
    padding: [64, 64, 64, 64],
    duration: 450,
    maxZoom,
  });
}

export default function GISMap({
  task,
  layerControls,
  uploadedLayerPreviews,
  uploadLayerControls,
  focusRequest,
  artifacts,
  artifactLayerControls,
  theme,
}: GISMapProps) {
  const mapElementRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<OLMap | null>(null);
  const baseLayerRef = useRef<TileLayer<XYZ> | null>(null);
  const aoiLayerRef = useRef<VectorLayer<VectorSource> | null>(null);
  const uploadLayerRefs = useRef<Map<string, DynamicMapLayer>>(new Map());
  const artifactLayerRefs = useRef<Map<string, DynamicMapLayer>>(new Map());
  const uploadRenderableIdsRef = useRef<Set<string>>(new Set());
  const artifactLayerIdsRef = useRef<Set<string>>(new Set());

  const [mapReady, setMapReady] = useState(false);
  const [cursorCoordinate, setCursorCoordinate] = useState<[number, number] | null>(null);

  useEffect(() => {
    if (!mapElementRef.current || mapRef.current) {
      return;
    }

    const baseLayer = new TileLayer({
      source: new XYZ({
        url: "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attributions: 'Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics',
        crossOrigin: "anonymous",
      }),
      opacity: 1,
      visible: true,
    });
    const aoiLayer = new VectorLayer({
      source: new VectorSource(),
      style: AOI_STYLE,
      opacity: 1,
      visible: true,
    });

    baseLayer.setZIndex(0);
    aoiLayer.setZIndex(400);

    const map = new OLMap({
      target: mapElementRef.current,
      layers: [baseLayer, aoiLayer],
      controls: [new ScaleLine({ bar: true, steps: 4, text: true, minWidth: 120 })],
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

    mapRef.current = map;
    baseLayerRef.current = baseLayer;
    aoiLayerRef.current = aoiLayer;
    setMapReady(true);

    return () => {
      removeDynamicLayers(map, uploadLayerRefs.current);
      removeDynamicLayers(map, artifactLayerRefs.current);
      map.setTarget(undefined);
      mapRef.current = null;
      baseLayerRef.current = null;
      aoiLayerRef.current = null;
    };
  }, []);

  useEffect(() => {
    const baseLayer = baseLayerRef.current;
    const aoiLayer = aoiLayerRef.current;
    if (!baseLayer || !aoiLayer) {
      return;
    }

    baseLayer.setVisible(layerControls.basemap.visible);
    baseLayer.setOpacity(layerControls.basemap.opacity);
    aoiLayer.setVisible(layerControls.aoi.visible);
    aoiLayer.setOpacity(layerControls.aoi.opacity);
  }, [layerControls.aoi.opacity, layerControls.aoi.visible, layerControls.basemap.opacity, layerControls.basemap.visible]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }

    const renderableIds = new Set<string>();
    for (const [fileId, preview] of Object.entries(uploadedLayerPreviews)) {
      if (preview.kind === "raster" && preview.rasterPreviewImageUrl && preview.previewBounds) {
        renderableIds.add(fileId);
        continue;
      }
      if (preview.kind === "vector" && preview.geojsonText) {
        renderableIds.add(fileId);
      }
    }
    const newlyRenderableIds = new Set(
      [...renderableIds].filter((fileId) => !uploadRenderableIdsRef.current.has(fileId)),
    );
    uploadRenderableIdsRef.current = renderableIds;

    removeDynamicLayers(map, uploadLayerRefs.current);

    for (const [fileId, preview] of Object.entries(uploadedLayerPreviews)) {
      const control = uploadLayerControls[fileId] ?? DEFAULT_DYNAMIC_CONTROL;
      const shouldAutoFit = newlyRenderableIds.has(fileId);

      if (preview.kind === "raster" && preview.rasterPreviewImageUrl && preview.previewBounds) {
        const imageExtent = transformExtent(preview.previewBounds, "EPSG:4326", "EPSG:3857");
        const layer = new ImageLayer({
          source: new ImageStatic({
            url: preview.rasterPreviewImageUrl,
            imageExtent,
            projection: "EPSG:3857",
            crossOrigin: "anonymous",
          }),
          visible: control.visible,
          opacity: control.opacity,
        });
        layer.setZIndex(80);
        map.addLayer(layer);
        uploadLayerRefs.current.set(fileId, layer);
        if (shouldAutoFit) {
          fitMapToExtent(map, imageExtent, 11);
        }
        continue;
      }

      if (preview.kind === "vector" && preview.geojsonText) {
        try {
          const format = new GeoJSON();
          const features = format.readFeatures(preview.geojsonText, {
            dataProjection: "EPSG:4326",
            featureProjection: "EPSG:3857",
          });

          const layer = new VectorLayer({
            source: new VectorSource({ features }),
            style: UPLOADED_VECTOR_STYLE,
            visible: control.visible,
            opacity: control.opacity,
          });
          layer.setZIndex(220);
          map.addLayer(layer);
          uploadLayerRefs.current.set(fileId, layer);
          if (shouldAutoFit) {
            fitMapToExtent(map, layer.getSource()?.getExtent() ?? null, 12);
          }
        } catch {
          // Keep layer list entry and skip map rendering on malformed local GeoJSON.
        }
      }
    }
  }, [mapReady, uploadedLayerPreviews]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }

    const currentIds = artifacts.map((artifact) => artifact.artifact_id);
    const newIds = currentIds.filter((id) => !artifactLayerIdsRef.current.has(id));
    artifactLayerIdsRef.current = new Set(currentIds);

    removeDynamicLayers(map, artifactLayerRefs.current);

    for (const artifact of artifacts) {
      const control = artifactLayerControls[artifact.artifact_id] ?? DEFAULT_DYNAMIC_CONTROL;
      const isNewLayer = newIds.includes(artifact.artifact_id);

      if (isRasterArtifact(artifact)) {
        const source = new GeoTIFF({
          normalize: false,
          sources: [{ url: artifact.download_url }],
        });
        const rasterLayer = new WebGLTileLayer({
          source,
          visible: control.visible,
          opacity: control.opacity,
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
        rasterLayer.setZIndex(120);
        map.addLayer(rasterLayer);
        artifactLayerRefs.current.set(artifact.artifact_id, rasterLayer);
        if (isNewLayer && task?.aoi_bbox_bounds?.length === 4) {
          const aoiExtent = transformExtent(task.aoi_bbox_bounds, "EPSG:4326", "EPSG:3857");
          fitMapToExtent(map, aoiExtent, 12);
        }
        continue;
      }

      if (isGeojsonArtifact(artifact)) {
        const vectorSource = new VectorSource({
          url: artifact.download_url,
          format: new GeoJSON({
            dataProjection: "EPSG:4326",
            featureProjection: "EPSG:3857",
          }),
        });
        const vectorLayer = new VectorLayer({
          source: vectorSource,
          style: ARTIFACT_VECTOR_STYLE,
          visible: control.visible,
          opacity: control.opacity,
        });
        vectorLayer.setZIndex(240);
        map.addLayer(vectorLayer);
        artifactLayerRefs.current.set(artifact.artifact_id, vectorLayer);
        if (isNewLayer) {
          fitMapToExtent(map, vectorSource.getExtent(), 12);
          vectorSource.once("change", () => {
            fitMapToExtent(map, vectorSource.getExtent(), 12);
          });
        }
      }
    }
  }, [artifacts, mapReady, task?.aoi_bbox_bounds, task?.task_id]);

  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map || !focusRequest) {
      return;
    }

    if (focusRequest.kind === "upload") {
      const preview = uploadedLayerPreviews[focusRequest.id];
      if (!preview) {
        return;
      }

      if (preview.kind === "raster" && preview.previewBounds) {
        const imageExtent = transformExtent(preview.previewBounds, "EPSG:4326", "EPSG:3857");
        fitMapToExtent(map, imageExtent, 11);
        return;
      }

      if (preview.kind === "vector") {
        const uploadLayer = uploadLayerRefs.current.get(focusRequest.id);
        if (uploadLayer instanceof VectorLayer) {
          fitMapToExtent(map, uploadLayer.getSource()?.getExtent() ?? null, 12);
          return;
        }

        if (preview.geojsonText) {
          try {
            const format = new GeoJSON();
            const features = format.readFeatures(preview.geojsonText, {
              dataProjection: "EPSG:4326",
              featureProjection: "EPSG:3857",
            });
            const vectorSource = new VectorSource({ features });
            fitMapToExtent(map, vectorSource.getExtent(), 12);
          } catch {
            // Ignore malformed preview data on manual focus.
          }
        }
      }
      return;
    }

    const artifact = artifacts.find((item) => item.artifact_id === focusRequest.id);
    if (!artifact) {
      return;
    }

    if (isRasterArtifact(artifact)) {
      if (task?.aoi_bbox_bounds?.length === 4) {
        const aoiExtent = transformExtent(task.aoi_bbox_bounds, "EPSG:4326", "EPSG:3857");
        fitMapToExtent(map, aoiExtent, 12);
      }
      return;
    }

    if (!isGeojsonArtifact(artifact)) {
      return;
    }

    const artifactLayer = artifactLayerRefs.current.get(artifact.artifact_id);
    if (artifactLayer instanceof VectorLayer) {
      const source = artifactLayer.getSource();
      fitMapToExtent(map, source?.getExtent() ?? null, 12);
      source?.once("change", () => {
        fitMapToExtent(map, source.getExtent(), 12);
      });
      return;
    }

    const tempSource = new VectorSource({
      url: artifact.download_url,
      format: new GeoJSON({
        dataProjection: "EPSG:4326",
        featureProjection: "EPSG:3857",
      }),
    });
    tempSource.once("change", () => {
      fitMapToExtent(map, tempSource.getExtent(), 12);
    });
  }, [artifacts, focusRequest, mapReady, task?.aoi_bbox_bounds, uploadedLayerPreviews]);

  useEffect(() => {
    for (const [fileId, layer] of uploadLayerRefs.current) {
      const control = uploadLayerControls[fileId] ?? DEFAULT_DYNAMIC_CONTROL;
      layer.setVisible(control.visible);
      layer.setOpacity(control.opacity);
    }
  }, [uploadLayerControls]);

  useEffect(() => {
    for (const [artifactId, layer] of artifactLayerRefs.current) {
      const control = artifactLayerControls[artifactId] ?? DEFAULT_DYNAMIC_CONTROL;
      layer.setVisible(control.visible);
      layer.setOpacity(control.opacity);
    }
  }, [artifactLayerControls]);

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

  const aoiBadge = task?.aoi_name ?? task?.task_spec?.aoi_input;
  const aoiAreaText = task?.aoi_area_km2 ? `${task.aoi_area_km2.toFixed(1)}km²` : null;

  return (
    <div className={`map-stage map-theme-${theme}`}>
      <div ref={mapElementRef} className="map-canvas" />

      {aoiBadge ? (
        <div className="map-aoi-badge">
          <span>{aoiBadge}</span>
          <b>{aoiAreaText ?? "AOI"}</b>
        </div>
      ) : null}

      <div className="map-info-ribbon">
        <span className="map-divider" />
        <span>{formatCoordinate(cursorCoordinate)}</span>
        <span className="map-divider" />
      </div>
    </div>
  );
}
