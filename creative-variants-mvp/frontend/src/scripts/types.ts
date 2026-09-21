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

/** Dónde va el producto, en fracciones del lienzo (0..1).
 *
 *  Se guarda en fracciones y no en píxeles porque la misma decisión tiene que
 *  valer en los cinco formatos de la tanda, que no miden lo mismo. */
export interface ProductZone {
  x: number;
  y: number;
  width: number;
  height: number;
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
  /** Zona elegida a mano para el producto. `null` = la decide el motor. */
  product_zone?: ProductZone | null;
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

/** Cuántos bloques del arte entran en un formato, y cuáles sobran. */
export interface FormatFit {
  id: string;
  width: number;
  height: number;
  fits: number;
  total: number;
  dropped: string[];
}

export interface FormatFitResponse {
  project_id: string;
  formats: FormatFit[];
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
  /** Piezas distintas dentro del PNG. Más de una se puede separar. */
  pieces: number;
  /** Id de la capa de la que salió esta parte, si es una parte. */
  part_of?: string | null;
  /** Si la separación cubre el elemento entero sin pisarse. `null` = no viene
   *  de separar nada. */
  split_ok?: boolean | null;
  /** Qué se comprobó al separarla, en una línea. */
  split_check?: string | null;
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

/** Cliente persistente. El backend nuevo lo llama `client`; durante la
 * transición también normalizamos las marcas existentes (`/brands`) a esta
 * forma para que la pantalla no dependa de dos contratos distintos. */
export interface ClientProfile {
  client_id: string;
  name: string;
  slug?: string;
  social_urls: string[];
  templates: number;
  campaigns: number;
  updated_at?: string;
}

export type CampaignSourceKind =
  | "document"
  | "presentation"
  | "layered_artwork"
  | "image"
  | "font"
  | "text"
  | "other";

/** Archivo que informa a la IA. Una fuente NO es un KV ni una plantilla: un
 * PDF de 25 páginas sigue siendo una sola fuente, aunque tenga 25 previews. */
export interface CampaignSource {
  source_id: string;
  filename: string;
  kind: CampaignSourceKind;
  media_type?: string;
  bytes?: number;
  pages?: number;
  status: "uploaded" | "processing" | "ready" | "warning" | "error";
  summary?: string;
  role?: string;
  roles?: string[];
  preview?: string | null;
  previews?: string[];
  warnings: string[];
  /** Solo existe en el fallback legado y nunca se presenta como un KV. */
  legacy_project_ids?: string[];
}

export interface CampaignWorkspace {
  campaign_id: string;
  client_id: string;
  name: string;
  objective?: string;
  social_urls: string[];
  status?: string;
  brief_reviewed_at?: string | null;
  production_assets?: CampaignProductionAsset[];
  social_evidence?: Array<{
    url: string;
    title?: string;
    description?: string;
    posts?: string[];
    accessible?: boolean;
    blocked_reason?: string;
  }>;
  created_at?: string;
  updated_at?: string;
}

export interface CampaignProductionAsset {
  asset_id: string;
  filename: string;
  media_type: string;
  extension: string;
  size_bytes: number;
  width: number;
  height: number;
  preview_url: string;
}

export interface BriefSection {
  key: string;
  label: string;
  value: string | string[];
  confidence?: number;
  source_ids?: string[];
}

export interface CampaignIntelligence {
  objective: string;
  audience: string;
  message: string;
  tone: string[];
  concept: string;
  palette: string[];
  typography: string[];
  headline_style: string;
  cta_style: string;
  visual_rules: string[];
  product_treatment: string[];
  required_elements: string[];
  optional_elements: string[];
  forbidden_elements: string[];
  legal: string[];
  sections: BriefSection[];
  summary: string;
  engine?: string;
  warnings: string[];
}

export type TemplateCandidateStatus = "proposed" | "approved" | "rejected";

export interface TemplateCandidateField {
  id: string;
  label: string;
  kind: "image" | "text";
  category?: string;
  required: boolean;
  generated_when_missing?: boolean;
  hide_when_empty?: boolean;
}

/** Propuesta todavía sin productos. Define estructura, campos y comportamiento
 * multiformato; la imagen de producto entra únicamente desde la matriz. */
export interface TemplateCandidate {
  candidate_id: string;
  name: string;
  description: string;
  category: string;
  rationale: string;
  status: TemplateCandidateStatus;
  fields: TemplateCandidateField[];
  supported_product_count?: { min: number; max: number };
  preview?: string | null;
  preview_url?: string | null;
  preview_urls?: Record<string, string>;
  source_project_id?: string | null;
  /** Fuentes de campaña que sustentaron esta propuesta; no son productos. */
  source_ids?: string[];
  warnings: string[];
  approved_at?: string | null;
  decision_notes?: string | null;
  revision_hash?: string;
  blueprint?: {
    archetype?: string;
    text_alignment?: string;
    background_style?: string;
    accent_style?: string;
    density?: string;
  };
}

export interface CampaignBriefResult {
  campaign_id: string;
  brief: CampaignIntelligence;
  template_candidates: TemplateCandidate[];
  engine?: string;
  warnings: string[];
}

export interface ProductionPiece {
  piece_id: string;
  row_number: number;
  product: string;
  template_name: string;
  format: string;
  width: number;
  height: number;
  proposal: number;
  preview_url: string;
  png_url: string;
  jpg_url: string;
  psd_url: string;
  warnings: string[];
  status: string;
}

export interface ProductionBatch {
  batch_id: string;
  client_id: string;
  campaign_id: string;
  created_at: string;
  status: string;
  total_rows: number;
  total_pieces: number;
  warnings: string[];
  zip_url: string;
  manifest_url: string;
  pieces: ProductionPiece[];
}

/** Resultado de compatibilidad de una fila de matriz antes de crear una tanda.
 *
 * El servidor decide la plantilla compatible con el brief y sus campos. La UI
 * conserva este plan junto con la matriz para que una recarga no esconda una
 * incompatibilidad ni haga creer que se puede producir a ciegas. */
export type MatrixProductionPlanStatus = "ready" | "needs_approval" | "incompatible";

export interface MatrixProductionPlanTemplate {
  candidate_id: string;
  name: string;
}

export interface MatrixProductionPlan {
  row_number: number;
  product_count: number;
  status: MatrixProductionPlanStatus;
  template: MatrixProductionPlanTemplate | null;
  required_fields: string[];
  ai_fillable_fields: string[];
  message: string;
}

export interface CampaignMatrixPreview {
  rows: Record<string, unknown>[];
  plans: MatrixProductionPlan[];
  matrixDraftId: string | null;
}

/** Orden persistente mientras el worker prepara una tanda de campaña. */
export interface CampaignProductionTask {
  task_id: string;
  state: "PENDING" | "STARTED" | "PROGRESS" | "COMPLETED" | "FAILED";
  result: ProductionBatch | null;
  error: string | null;
  meta: {
    progress?: number;
    status?: string;
    planned_pieces?: number;
    batch_id?: string | null;
  };
}
