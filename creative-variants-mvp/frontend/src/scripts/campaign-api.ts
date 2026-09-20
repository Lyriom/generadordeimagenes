import { ApiError, del, downloadUrl, get, post, put, upload } from "./api";
import type {
  CampaignBriefResult,
  CampaignIntelligence,
  CampaignSource,
  CampaignSourceKind,
  CampaignWorkspace,
  ClientProfile,
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
    visual_rules?: string[];
    product_treatment?: string[];
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

export async function produceCampaign(
  clientId: string,
  campaignId: string,
  matrix: File,
  productFiles: File[],
  productAssetIds: string[],
  defaultFormats: string[],
  useAiCopy: boolean,
  onProgress?: (sent: number, total: number) => void,
  onUploaded?: () => void,
): Promise<ProductionBatch> {
  const form = new FormData();
  form.append("matrix", matrix);
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
  return normaliseProductionBatch(payload);
}

export async function previewCampaignMatrix(
  clientId: string,
  campaignId: string,
  matrix: File,
): Promise<any[]> {
  const form = new FormData();
  form.append("matrix", matrix);
  const payload: any = await upload(
    "/clients/" + encodeURIComponent(clientId) + "/campaigns/" +
      encodeURIComponent(campaignId) + "/production/preview",
    form,
  );
  return list(payload?.rows);
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
