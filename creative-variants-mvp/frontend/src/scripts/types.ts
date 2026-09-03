/* Los cuatro primeros son los pasos del flujo, en orden. `results` queda
   siempre accesible porque no es un paso sino la galería de lo producido. */
export type ViewName = "campaign" | "layers" | "products" | "generate" | "results";

export interface CanvasSize {
  width: number;
  height: number;
}

export interface ProjectSummary {
  project_id: string;
  name: string;
  created_at: string;
  updated_at: string;
  canvas: CanvasSize;
  layers: number;
  variants: number;
}

export interface Layer {
  id: string;
  name: string;
  type: "image" | "text";
  category: string;
  src?: string | null;
  mask?: string | null;
  x: number;
  y: number;
  width: number;
  height: number;
  rotation: number;
  z_index: number;
  visible: boolean;
  locked: boolean;
  movable: boolean;
  resizable: boolean;
  reorderable: boolean;
  replaceable: boolean;
  preserve_aspect_ratio: boolean;
  content?: string | null;
  font_family: string;
  font_size: number;
  font_weight: "normal" | "bold";
  color: string;
  text_align: "left" | "center" | "right";
  auto_contrast: boolean;
  export_as_text: boolean;
  text_verified: boolean;
  confidence: number;
  extracted: boolean;
  warnings: string[];
  meta: Record<string, any>;
}

export interface Variant {
  id: string;
  index: number;
  layout: string;
  layout_label: string;
  format: string;
  width: number;
  height: number;
  image: string;
  thumbnail?: string | null;
  quality: { score: number; warnings: string[]; metrics: Record<string, number> };
  meta: Record<string, any>;
}

export interface Project {
  project_id: string;
  name: string;
  created_at: string;
  updated_at: string;
  canvas: CanvasSize;
  source: {
    path: string;
    width: number;
    height: number;
    format: string;
    original_filename: string;
  };
  references: {
    kv?: { path: string } | null;
    logo?: { path: string } | null;
    font?: string | null;
    font_bold?: string | null;
  };
  layers: Layer[];
  variants: Variant[];
  warnings: string[];
  analysis: Record<string, any>;
  background: { path?: string | null; provider?: string | null; warnings?: string[] };
  meta: Record<string, any>;
}

export interface FormatPreset {
  id: string;
  platform: string;
  family: string;
  placement: string;
  label: string;
  width: number;
  height: number;
  ratio: string;
  safe_area: { left: number; top: number; right: number; bottom: number };
  recommended: boolean;
  media_type: string;
  note: string;
  source_url: string;
}

export interface Capabilities {
  segmentation: Record<string, any>;
  ocr: Record<string, any>;
  inpainting: Record<string, any>;
  image_models: Array<Record<string, any>>;
  formats: Record<string, [number, number]>;
  format_catalog: FormatPreset[];
  layouts: Array<{ key: string; label: string }>;
  intensities: string[];
}

/** Un elemento del arte que se puede reescribir o quitar de la pieza. */
export interface ArtTextLayer {
  id: string;
  name: string;
  category: string;
  z_index: number;
  text: string;
  original_text: string;
  editable: boolean;
  rewritten: boolean;
  removed: boolean;
  /** Sus píxeles siguen aplanados en el fondo: quitarlo exige borrarlo de ahí. */
  in_plate: boolean;
  src?: string | null;
  style?: {
    color: string;
    align: "left" | "center" | "right";
    lines: number;
    ink_height: number;
    line_height: number;
    stroke: number;
    line_pitch: number;
  } | null;
}

/** Copy del arte de un KV, con el estado de su tipografía de marca. */
export interface ArtTexts {
  layers: ArtTextLayer[];
  brand_font: boolean;
  brand_font_bold: boolean;
}

export interface ProductGroup {
  id: string;
  name: string;
  members: string[];
  arrangement: "auto" | "horizontal" | "vertical" | "overlap";
}
