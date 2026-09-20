import { ApiError, del, downloadUrl, get, post, put, upload } from "./api";
import type {
  CampaignBriefResult,
  CampaignIntelligence,
  CampaignSource,
  CampaignSourceKind,
  CampaignWorkspace,
  CampaignMatrixPreview,
  CampaignProductionTask,
  ClientProfile,
  MatrixProductionPlan,
  ProductionBatch,
  TemplateCandidate,
  TemplateCandidateField,
} from "./types";

/** Los endpoints de campaña se desplegaron después que el editor de KV. Este
 * archivo es la frontera de compatibilidad: el resto de la UI solo conoce los
 * tipos estables de arriba y no necesita saber si el servidor respondió con
 * una lista directa o con `{clients: [...]}`. */

export function missingCampaignCapability(error: unknown): boolean {
  return error instanceof ApiError && [404, 405, 501].includes(error.status);
}

const list = (value: unknown): any[] => Array.isArray(value) ? value : [];
const strings = (value: unknown): string[] => {
  if (Array.isArray(value)) return value.map(String).filter(Boolean);
  if (value && typeof value === "object") return Object.values(value).map(String).filter(Boolean);
  return [];
};

function sourceKind(raw: any): CampaignSourceKind {
  const kind = String(raw?.kind || raw?.type || "").toLowerCase();
  if (["document", "pdf", "docx"].includes(kind)) return "document";
  if (["presentation", "ppt", "pptx"].includes(kind)) return "presentation";
  if (["psd", "psb", "layered", "layered_design", "layered_artwork"].includes(kind)) return "layered_artwork";
  if (["image", "raster", "photo"].includes(kind)) return "image";
  if (["font", "typography"].includes(kind)) return "font";
  if (["text", "txt", "brief"].includes(kind)) return "text";
  return "other";
}

export function normaliseClient(raw: any): ClientProfile {
  return {
    client_id: String(raw?.client_id || raw?.brand_id || raw?.id || ""),
    name: String(raw?.name || "Cliente sin nombre"),
    slug: raw?.slug ? String(raw.slug) : undefined,
    social_urls: strings(raw?.social_urls || raw?.handles),
    templates: Number(raw?.templates || raw?.template_count || 0),
    campaigns: Number(raw?.campaigns || raw?.campaign_count || 0),
    updated_at: raw?.updated_at ? String(raw.updated_at) : undefined,
  };
}

export async function listClients(): Promise<ClientProfile[]> {
  try {
    const payload: any = await get("/clients");
    return list(payload?.clients ?? payload).map(normaliseClient).filter((item) => item.client_id);
  } catch (error) {
    if (!missingCampaignCapability(error)) throw error;
    const payload: any = await get("/brands");
    return list(payload?.brands ?? payload).map(normaliseClient).filter((item) => item.client_id);
  }
}

export async function createClient(name: string, socialUrls: string[]): Promise<ClientProfile> {
  try {
    return normaliseClient(await post("/clients", { name, social_urls: socialUrls }));
  } catch (error) {
    if (!missingCampaignCapability(error)) throw error;
    const handles = Object.fromEntries(socialUrls.map((url, index) => ["url_" + String(index + 1), url]));
    const payload: any = await post("/brands", { name, handles });
    return normaliseClient(payload?.brand || payload);
  }
}

export function normaliseCampaign(raw: any, clientId = ""): CampaignWorkspace {
  const normalizedClientId = String(raw?.client_id || clientId);
  const campaignId = String(raw?.campaign_id || raw?.id || "");
  return {
    campaign_id: campaignId,
    client_id: normalizedClientId,
    name: String(raw?.name || "Campaña sin nombre"),
    objective: raw?.objective ? String(raw.objective) : "",
    social_urls: strings(raw?.social_urls),
    status: raw?.status ? String(raw.status) : undefined,
    brief_reviewed_at: raw?.brief_reviewed_at ? String(raw.brief_reviewed_at) : null,
    social_evidence: list(raw?.meta?.social_evidence).map((item: any) => ({
      url: String(item?.url || ""),
      title: item?.title ? String(item.title) : undefined,
      description: item?.description ? String(item.description) : undefined,
      posts: strings(item?.posts),
      accessible: item?.accessible !== false,
      blocked_reason: item?.blocked_reason ? String(item.blocked_reason) : undefined,
    })).filter((item) => item.url),
    production_assets: list(raw?.production_assets).map((item: any) => ({
      asset_id: String(item?.asset_id || ""),
      filename: String(item?.filename || "Imagen sin nombre"),
      media_type: String(item?.media_type || "application/octet-stream"),
      extension: String(item?.extension || ""),
      size_bytes: Number(item?.size_bytes || 0),
      width: Number(item?.width || 0),
      height: Number(item?.height || 0),
      preview_url: downloadUrl(
        "/clients/" + encodeURIComponent(normalizedClientId) + "/campaigns/" +
        encodeURIComponent(campaignId) + "/production/assets/" +
        encodeURIComponent(String(item?.asset_id || "")) + "/file",
      ),
    })).filter((item) => item.asset_id),
    created_at: raw?.created_at ? String(raw.created_at) : undefined,
    updated_at: raw?.updated_at ? String(raw.updated_at) : undefined,
  };
}

function normaliseProductionAssets(
  raw: any,
  clientId: string,
  campaignId: string,
): CampaignWorkspace["production_assets"] {
  return normaliseCampaign({
    client_id: clientId,
    campaign_id: campaignId,
    production_assets: raw,
  }, clientId).production_assets;
}

export async function listCampaigns(clientId: string): Promise<CampaignWorkspace[]> {
  const payload: any = await get("/clients/" + encodeURIComponent(clientId) + "/campaigns");
  return list(payload?.campaigns ?? payload).map((item) => normaliseCampaign(item, clientId));
}

export async function getCampaignDetails(clientId: string, campaignId: string): Promise<any> {
  return get(
    "/clients/" + encodeURIComponent(clientId) + "/campaigns/" + encodeURIComponent(campaignId),
  );
}

export async function loadCampaignState(clientId: string, campaignId: string): Promise<{
  workspace: CampaignWorkspace;
  sources: CampaignSource[];
  brief: CampaignIntelligence | null;
  candidates: TemplateCandidate[];
}> {
  const raw: any = await getCampaignDetails(clientId, campaignId);
  return {
    workspace: normaliseCampaign(raw, clientId),
    sources: list(raw?.sources).map((item, index) => normaliseSource(item, index, clientId, campaignId)),
    brief: raw?.brief ? normaliseBrief(raw.brief, raw?.analysis_engine, strings(raw?.warnings)) : null,
    candidates: list(raw?.template_candidates).map(normaliseCandidate),
  };
}

export async function createCampaign(
  clientId: string,
  input: { name: string; objective?: string; social_urls: string[] },
): Promise<CampaignWorkspace | null> {
  try {
    return normaliseCampaign(
      await post("/clients/" + encodeURIComponent(clientId) + "/campaigns", input),
      clientId,
    );
  } catch (error) {
    if (missingCampaignCapability(error)) return null;
    throw error;
  }
}

export async function updateCampaignContext(
  clientId: string,
  campaignId: string,
  input: { name?: string; objective?: string; social_urls?: string[] },
): Promise<CampaignWorkspace> {
  return normaliseCampaign(
    await put(
      "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
        encodeURIComponent(campaignId),
      input,
    ),
    clientId,
  );
}

export function normaliseSource(raw: any, index = 0, clientId = "", campaignId = ""): CampaignSource {
  const warnings = strings(raw?.warnings);
  const previews = strings(raw?.previews || raw?.preview_urls || raw?.preview_files).map((path) =>
    /^https?:\/\//i.test(path) || path.startsWith("/api/")
      ? path
      : campaignSourceFileUrl(clientId, campaignId, String(raw?.source_id || raw?.id || ""), path)
  );
  return {
    source_id: String(raw?.source_id || raw?.id || "source-" + String(index + 1)),
    filename: String(raw?.filename || raw?.name || "Archivo " + String(index + 1)),
    kind: sourceKind(raw),
    media_type: raw?.media_type ? String(raw.media_type) : undefined,
    bytes: Number(raw?.bytes || raw?.size || raw?.size_bytes || 0) || undefined,
    pages: Number(raw?.pages || raw?.page_count || 0) || undefined,
    status: warnings.length ? "warning" : (raw?.status || "ready"),
    summary: raw?.summary ? String(raw.summary) : undefined,
    role: raw?.role || raw?.classification
      ? String(raw.role || raw.classification)
      : strings(raw?.roles).join(" · ") || undefined,
    preview: raw?.preview || raw?.preview_url || previews[0] || null,
    previews,
    warnings,
    legacy_project_ids: strings(raw?.legacy_project_ids),
  };
}

export async function uploadCampaignSources(
  clientId: string,
  campaignId: string,
  files: File[],
  onProgress?: (sent: number, total: number) => void,
  onUploaded?: () => void,
): Promise<{ sources: CampaignSource[]; warnings: string[] } | null> {
  const form = new FormData();
  files.forEach((file) => form.append("files", file));
  try {
    const payload: any = await upload(
      "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
        encodeURIComponent(campaignId) + "/sources",
      form,
      onProgress,
      onUploaded,
    );
    return {
      sources: list(payload?.sources).map((item, index) => normaliseSource(item, index, clientId, campaignId)),
      warnings: strings(payload?.warnings),
    };
  } catch (error) {
    if (missingCampaignCapability(error)) return null;
    throw error;
  }
}

export async function deleteCampaignSource(
  clientId: string,
  campaignId: string,
  sourceId: string,
): Promise<void> {
  await del(
    "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
      encodeURIComponent(campaignId) + "/sources/" + encodeURIComponent(sourceId),
  );
}

function normaliseFields(value: unknown): TemplateCandidateField[] {
  return list(value).map((field: any, index) => ({
    id: String(field?.id || field?.key || "campo_" + String(index + 1)),
    label: String(field?.label || field?.name || field?.id || "Campo"),
    kind: field?.kind === "image" || field?.type === "image" ? "image" : "text",
    category: String(field?.category || ""),
    required: Boolean(field?.required),
    generated_when_missing: Boolean(field?.generated_when_missing || field?.generate_if_missing || field?.ai_fill),
    hide_when_empty: field?.hide_when_empty !== false,
  }));
}

export function normaliseCandidate(raw: any, index = 0): TemplateCandidate {
  const range = raw?.supported_product_count || raw?.product_count || {};
  const status = ["approved", "rejected"].includes(raw?.status) ? raw.status : "proposed";
  const previewUrls = Object.fromEntries(
    Object.entries(raw?.preview_urls || {}).map(([key, value]) => [
      String(key),
      String(value).startsWith("/api/") ? String(value) : downloadUrl(String(value)),
    ]),
  );
  return {
    candidate_id: String(raw?.candidate_id || raw?.template_id || raw?.id || "candidate-" + String(index + 1)),
    name: String(raw?.name || raw?.title || "Plantilla " + String(index + 1)),
    description: String(raw?.description || raw?.layout_intent || raw?.summary || "Sistema adaptable propuesto por la IA."),
    category: String(raw?.category || raw?.type || "Producto"),
    rationale: String(raw?.rationale || raw?.reason || ""),
    status,
    fields: normaliseFields(raw?.fields || raw?.slots),
    supported_product_count: {
      min: Number(range?.minimum ?? range?.min ?? raw?.min_products ?? 1),
      max: Number(range?.maximum ?? range?.max ?? raw?.max_products ?? 1),
    },
    preview: raw?.preview || null,
    preview_url: raw?.preview_url
      ? (String(raw.preview_url).startsWith("/api/") ? String(raw.preview_url) : downloadUrl(String(raw.preview_url)))
      : null,
    preview_urls: previewUrls,
    source_project_id: raw?.source_project_id || null,
    source_ids: strings(raw?.source_ids),
    warnings: strings(raw?.warnings),
    approved_at: raw?.approved_at || null,
    decision_notes: raw?.decision_notes || null,
    revision_hash: raw?.revision_hash ? String(raw.revision_hash) : "",
    blueprint: raw?.blueprint && typeof raw.blueprint === "object"
      ? {
          archetype: raw.blueprint.archetype ? String(raw.blueprint.archetype) : undefined,
          text_alignment: raw.blueprint.text_alignment ? String(raw.blueprint.text_alignment) : undefined,
          background_style: raw.blueprint.background_style ? String(raw.blueprint.background_style) : undefined,
          accent_style: raw.blueprint.accent_style ? String(raw.blueprint.accent_style) : undefined,
          density: raw.blueprint.density ? String(raw.blueprint.density) : undefined,
        }
      : undefined,
  };
}

function normaliseBrief(raw: any, engine?: string, warnings: string[] = []): CampaignIntelligence {
  const stringValue = (key: string, fallback = "") => {
    const value = raw?.[key];
    return typeof value === "string" ? value : fallback;
  };
  return {
    objective: stringValue("objective"),
    audience: stringValue("audience"),
    message: stringValue("message", stringValue("main_message", stringValue("primary_message"))),
    tone: strings(raw?.tone),
    concept: stringValue("concept", stringValue("creative_concept")),
    palette: strings(raw?.palette),
    typography: strings(raw?.typography || raw?.fonts),
    headline_style: stringValue("headline_style"),
    cta_style: stringValue("cta_style"),
    visual_rules: strings(raw?.visual_rules || raw?.composition_rules),
    product_treatment: strings(raw?.product_treatment),
    required_elements: strings(raw?.required_elements || raw?.mandatory_elements),
    optional_elements: strings(raw?.optional_elements),
    forbidden_elements: strings(raw?.forbidden_elements || raw?.restrictions),
    legal: strings(raw?.legal || raw?.legals || raw?.legal_requirements),
    sections: list(raw?.sections).map((section: any, index) => ({
      key: String(section?.key || "section_" + String(index + 1)),
      label: String(section?.label || section?.title || "Sección"),
      value: Array.isArray(section?.value) ? strings(section.value) : String(section?.value || ""),
      confidence: typeof section?.confidence === "number" ? section.confidence : undefined,
      source_ids: strings(section?.source_ids),
    })),
    summary: stringValue("summary", stringValue("overview", strings(raw?.source_summary).join(" · "))),
    engine,
    warnings: [...warnings, ...strings(raw?.warnings)],
  };
}

export async function generateCampaignBrief(
  clientId: string,
  campaignId: string,
  preserveReview = false,
): Promise<CampaignBriefResult | null> {
  try {
    const payload: any = await post(
      "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
        encodeURIComponent(campaignId) + "/brief/generate",
      { use_ai: true, preserve_review: preserveReview },
    );
    const warnings = strings(payload?.warnings);
    return {
      campaign_id: String(payload?.campaign_id || campaignId),
      brief: normaliseBrief(payload?.brief || {}, payload?.engine, warnings),
      template_candidates: list(payload?.template_candidates).map(normaliseCandidate),
      engine: payload?.engine ? String(payload.engine) : undefined,
      warnings,
    };
  } catch (error) {
    if (missingCampaignCapability(error)) return null;
    throw error;
  }
}

export async function reviseCampaignBrief(
  clientId: string,
  campaignId: string,
  changes: {
    objective?: string;
    audience?: string;
    primary_message?: string;
    creative_concept?: string;
    tone?: string[];
    headline_style?: string;
    cta_style?: string;
    visual_rules?: string[];
    product_treatment?: string[];
    legal_requirements?: string[];
    required_elements?: string[];
    optional_elements?: string[];
    forbidden_elements?: string[];
    feedback?: string;
  },
): Promise<CampaignIntelligence> {
  const payload: any = await put(
    "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
      encodeURIComponent(campaignId) + "/brief",
    changes,
  );
  return normaliseBrief(payload);
}

export async function reviseTemplateCandidate(
  clientId: string,
  campaignId: string,
  candidateId: string,
  notes: string,
): Promise<CampaignBriefResult> {
  const payload: any = await post(
    "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
      encodeURIComponent(campaignId) + "/template-candidates/" +
      encodeURIComponent(candidateId) + "/revise",
    { notes },
  );
  const warnings = strings(payload?.warnings);
  return {
    campaign_id: String(payload?.campaign_id || campaignId),
    brief: normaliseBrief(payload?.brief || {}, payload?.engine, warnings),
    template_candidates: list(payload?.template_candidates).map(normaliseCandidate),
    engine: payload?.engine ? String(payload.engine) : undefined,
    warnings,
  };
}

export async function decideTemplateCandidate(
  clientId: string,
  campaignId: string,
  candidateId: string,
  decision: "approve" | "reject",
  notes = "",
): Promise<TemplateCandidate | null> {
  try {
    const payload: any = await post(
      "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
        encodeURIComponent(campaignId) + "/template-candidates/" +
        encodeURIComponent(candidateId) + "/" + decision,
      { notes },
    );
    return normaliseCandidate(payload?.candidate || payload?.template_candidate || payload);
  } catch (error) {
    if (missingCampaignCapability(error)) return null;
    throw error;
  }
}

function normaliseProductionBatch(raw: any): ProductionBatch {
  const clientId = String(raw?.client_id || "");
  const campaignId = String(raw?.campaign_id || "");
  const batchId = String(raw?.batch_id || "");
  const fileUrl = (relative: unknown) => relative
    ? downloadUrl(
        "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
        encodeURIComponent(campaignId) + "/production/" + encodeURIComponent(batchId) +
        "/files/" + String(relative).split("/").map(encodeURIComponent).join("/"),
      )
    : "";
  return {
    batch_id: batchId,
    client_id: clientId,
    campaign_id: campaignId,
    created_at: String(raw?.created_at || ""),
    status: String(raw?.status || "ready"),
    total_rows: Number(raw?.total_rows || 0),
    total_pieces: Number(raw?.total_pieces || 0),
    warnings: strings(raw?.warnings),
    zip_url: raw?.zip_url ? downloadUrl(String(raw.zip_url)) : "",
    manifest_url: raw?.manifest_url ? downloadUrl(String(raw.manifest_url)) : "",
    pieces: list(raw?.pieces).map((piece: any) => ({
      piece_id: String(piece?.piece_id || ""),
      row_number: Number(piece?.row_number || 0),
      product: String(piece?.product || ""),
      template_name: String(piece?.template_name || ""),
      format: String(piece?.format || ""),
      width: Number(piece?.width || 0),
      height: Number(piece?.height || 0),
      proposal: Number(piece?.proposal || 1),
      preview_url: piece?.preview_url ? downloadUrl(String(piece.preview_url)) : "",
      png_url: fileUrl(piece?.png),
      jpg_url: fileUrl(piece?.jpg),
      psd_url: fileUrl(piece?.psd),
      warnings: strings(piece?.warnings),
      status: String(piece?.status || "ready"),
    })),
  };
}

function normaliseProductionTask(raw: any): CampaignProductionTask {
  const state = String(raw?.state || "PENDING").toUpperCase();
  const valid = ["PENDING", "STARTED", "PROGRESS", "COMPLETED", "FAILED"];
  const meta = raw?.meta && typeof raw.meta === "object" ? raw.meta : {};
  return {
    task_id: String(raw?.task_id || ""),
    state: (valid.includes(state) ? state : "PENDING") as CampaignProductionTask["state"],
    result: Array.isArray(raw?.result?.pieces) ? normaliseProductionBatch(raw.result) : null,
    error: raw?.error ? String(raw.error) : null,
    meta: {
      progress: Number(meta?.progress || raw?.progress || 0),
      status: meta?.status || raw?.detail ? String(meta?.status || raw?.detail) : "",
      planned_pieces: Number(meta?.planned_pieces || raw?.planned_pieces || 0),
      batch_id: meta?.batch_id || raw?.batch_id ? String(meta?.batch_id || raw?.batch_id) : null,
    },
  };
}

export async function produceCampaign(
  clientId: string,
  campaignId: string,
  matrix: File | null,
  matrixDraftId: string | null,
  productFiles: File[],
  productAssetIds: string[],
  defaultFormats: string[],
  useAiCopy: boolean,
  onProgress?: (sent: number, total: number) => void,
  onUploaded?: () => void,
): Promise<ProductionBatch | CampaignProductionTask> {
  const form = new FormData();
  if (matrixDraftId) form.append("matrix_draft_id", matrixDraftId);
  else if (matrix) form.append("matrix", matrix);
  else throw new Error("Primero valida una matriz de producción.");
  productFiles.forEach((file) => form.append("product_files", file));
  form.append("product_asset_ids", JSON.stringify(productAssetIds));
  form.append("default_formats", JSON.stringify(defaultFormats));
  form.append("use_ai_copy", String(useAiCopy));
  const payload: any = await upload(
    "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
      encodeURIComponent(campaignId) + "/production",
    form,
    onProgress,
    onUploaded,
  );
  // En desarrollo/tests la tarea Celery eager conserva la respuesta histórica
  // (la tanda lista). En producción llega una orden 202 y se hace polling.
  return Array.isArray(payload?.pieces)
    ? normaliseProductionBatch(payload)
    : normaliseProductionTask(payload);
}

export async function getCampaignProductionTask(
  clientId: string,
  campaignId: string,
  taskId: string,
): Promise<CampaignProductionTask> {
  const payload: any = await get(
    "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
      encodeURIComponent(campaignId) + "/production/tasks/" + encodeURIComponent(taskId),
  );
  return normaliseProductionTask(payload);
}

export async function listCampaignProductionTasks(
  clientId: string,
  campaignId: string,
): Promise<CampaignProductionTask[]> {
  const payload: any = await get(
    "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
      encodeURIComponent(campaignId) + "/production/tasks",
  );
  return list(payload?.tasks).map(normaliseProductionTask)
    .filter((task) => Boolean(task.task_id));
}

export async function pollCampaignProductionTask(
  clientId: string,
  campaignId: string,
  taskId: string,
  onProgress: (progress: number, detail: string) => void,
): Promise<ProductionBatch> {
  // La tarea vive en el servidor; este límite solo evita que una pestaña deje
  // un poll infinito. Al actualizar, la orden y sus entregables siguen ahí.
  const started = Date.now();
  while (Date.now() - started < 45 * 60 * 1000) {
    const task = await getCampaignProductionTask(clientId, campaignId, taskId);
    if (task.state === "COMPLETED") {
      if (task.result) return task.result;
      throw new Error("La producción terminó pero no devolvió sus entregables.");
    }
    if (task.state === "FAILED") {
      throw new Error(task.error || "La producción no pudo completarse.");
    }
    onProgress(
      Math.max(5, Math.min(98, Number(task.meta.progress || 5))),
      task.meta.status || "El worker está produciendo los artes…",
    );
    await new Promise((resolve) => window.setTimeout(resolve, 1100));
  }
  throw new Error("La tanda sigue en proceso. Actualiza luego para ver sus entregables.");
}

/** Convierte el preflight de la matriz en un contrato pequeño y seguro para la
 * interfaz. Un estado desconocido se trata como incompatible: es preferible
 * explicar que el plan no se entiende antes que producir una pieza distinta a
 * la que el servidor revisó. */
export function normaliseMatrixProductionPlan(raw: any): MatrixProductionPlan | null {
  const rowNumber = Math.trunc(Number(raw?.row_number));
  if (!Number.isFinite(rowNumber) || rowNumber < 2) return null;
  const receivedStatus = String(raw?.status || "").trim();
  const statuses = ["ready", "needs_approval", "incompatible"] as const;
  const status = statuses.includes(receivedStatus as typeof statuses[number])
    ? receivedStatus as MatrixProductionPlan["status"]
    : "incompatible";
  const rawTemplate = raw?.template && typeof raw.template === "object" ? raw.template : null;
  const message = String(raw?.message || "").trim() || (
    status === "ready"
      ? "La fila tiene una plantilla compatible."
      : receivedStatus
        ? "El servidor devolvió un estado de plan no compatible."
        : "No se pudo validar la compatibilidad de esta fila."
  );
  return {
    row_number: rowNumber,
    product_count: Math.max(0, Math.trunc(Number(raw?.product_count) || 0)),
    status,
    template: rawTemplate
      ? {
        candidate_id: String(rawTemplate?.candidate_id || ""),
        name: String(rawTemplate?.name || ""),
      }
      : null,
    required_fields: strings(raw?.required_fields),
    ai_fillable_fields: strings(raw?.ai_fillable_fields),
    message,
  };
}

export async function previewCampaignMatrix(
  clientId: string,
  campaignId: string,
  matrix: File,
  defaultFormats: string[] = [],
): Promise<CampaignMatrixPreview> {
  const form = new FormData();
  form.append("matrix", matrix);
  form.append("default_formats", JSON.stringify(defaultFormats));
  const payload: any = await upload(
    "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
      encodeURIComponent(campaignId) + "/production/preview",
    form,
  );
  return {
    rows: list(payload?.rows),
    plans: list(payload?.plans)
      .map(normaliseMatrixProductionPlan)
      .filter((plan): plan is MatrixProductionPlan => plan !== null),
    matrixDraftId: typeof payload?.matrix_draft_id === "string" && payload.matrix_draft_id
      ? payload.matrix_draft_id
      : null,
  };
}

export async function uploadCampaignProductAssets(
  clientId: string,
  campaignId: string,
  files: File[],
  onProgress?: (sent: number, total: number) => void,
): Promise<{ assets: NonNullable<CampaignWorkspace["production_assets"]>; warnings: string[] }> {
  const form = new FormData();
  files.forEach((file) => form.append("files", file));
  const payload: any = await upload(
    "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
      encodeURIComponent(campaignId) + "/production/assets",
    form,
    onProgress,
  );
  return {
    assets: normaliseProductionAssets(payload?.assets, clientId, campaignId) || [],
    warnings: strings(payload?.warnings),
  };
}

export async function deleteCampaignProductAsset(
  clientId: string,
  campaignId: string,
  assetId: string,
): Promise<void> {
  await del(
    "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
      encodeURIComponent(campaignId) + "/production/assets/" + encodeURIComponent(assetId),
  );
}

export async function listProductionBatches(
  clientId: string,
  campaignId: string,
): Promise<ProductionBatch[]> {
  const payload: any = await get(
    "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
      encodeURIComponent(campaignId) + "/production",
  );
  return list(payload?.batches ?? payload).map(normaliseProductionBatch);
}

export function campaignSourceFileUrl(
  clientId: string,
  campaignId: string,
  sourceId: string,
  relativePath: string,
): string {
  return "/api/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
    encodeURIComponent(campaignId) + "/sources/" + encodeURIComponent(sourceId) +
    "/files/" + relativePath.split("/").map(encodeURIComponent).join("/");
}
