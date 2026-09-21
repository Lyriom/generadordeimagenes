import {
  ApiError,
  del,
  downloadUrl,
  fileUrl,
  get,
  pollTask,
  post,
  put,
  sessionId,
  thumbnailUrl,
  variantPngUrl,
} from "./api";
import {
  createCampaign,
  createClient,
  deleteCampaignSource,
  deleteCampaignProductAsset,
  decideTemplateCandidate,
  generateCampaignBrief,
  getCampaignProductionTask,
  listCampaigns,
  listCampaignProductionTasks,
  listClients,
  listProductionBatches,
  loadCampaignState,
  normaliseMatrixProductionPlan,
  pollCampaignProductionTask,
  previewCampaignMatrix,
  produceCampaign,
  reviseCampaignBrief,
  reviseTemplateCandidate,
  updateCampaignSourceRole,
  updateCampaignContext,
  uploadCampaignProductAssets,
  uploadCampaignSources,
} from "./campaign-api";
import type {
  ArtTextLayer,
  ArtTexts,
  Capabilities,
  FormatFit,
  FormatFitResponse,
  FormatPreset,
  Layer,
  ProductGroup,
  ProductZone,
  ProductionBatch,
  Project,
  ProjectSummary,
  Variant,
  ViewName,
  CampaignIntelligence,
  CampaignProductionTask,
  CampaignProductionAsset,
  MatrixProductionPlan,
  CampaignSource,
  CampaignWorkspace,
  ClientProfile,
  TemplateCandidate,
} from "./types";

const VIEW_LABELS: Record<ViewName, string> = {
  campaign: "Campaña",
  layers: "Brief y activos",
  products: "Plantillas",
  generate: "Producción",
  results: "Entregables",
};

/* El flujo conserva un orden sugerido, pero la revisión de capas no bloquea el
   trabajo. Solo exigimos lo imprescindible para cada acción. */
const STEP_ORDER: ViewName[] = ["campaign", "layers", "products", "generate"];

interface StepState {
  done: Record<string, boolean>;
  blocked: Record<string, string | null>;
  next: ViewName;
}

/** Una capa está confirmada cuando el backend guardó su rol revisado. Vive en
 *  el proyecto, así que sobrevive a recargas y a reabrir la campaña. */
function layersConfirmed(project: Project): boolean {
  const relevant = project.layers.filter((layer) => layer.category !== "background");
  if (!relevant.length) {
    // Todo quedó marcado como fondo: sigue siendo una revisión válida, así que
    // basta con que alguna capa lleve el sello para no bloquear el paso.
    return project.layers.some((layer) => Boolean(layer.meta?.role_confirmed));
  }
  return relevant.every((layer) => Boolean(layer.meta?.role_confirmed));
}

function validGroups(): ProductGroup[] {
  return state.groups.filter((group) => group.members.length >= 2);
}

function productsReady(): boolean {
  return selectedProductFiles().length > 0 || validGroups().length > 0;
}

/** KV sin capa Producto: no se sabe qué pieza retirar para poner la nueva. */
function missingProductTargets(): Project[] {
  return state.campaign.filter((project) => !productTarget(project));
}

/** Nombres para un aviso, recortados para que no ocupen media pantalla. */
function nameList(projects: Project[], max = 3): string {
  const names = projects.slice(0, max).map((item) => item.name);
  const rest = projects.length - names.length;
  return names.join(", ") + (rest > 0 ? " y " + String(rest) + " más" : "");
}

function stepState(): StepState {
  const hasKnowledge = state.campaignSources.length > 0;
  const hasLegacyCampaign = state.campaign.length > 0;
  const hasCampaign = Boolean(state.campaignWorkspace) || hasKnowledge || hasLegacyCampaign;
  const reviewed = state.campaignWorkspace
    ? Boolean(state.campaignIntelligence && state.campaignWorkspace.brief_reviewed_at)
    : hasLegacyCampaign && state.campaign.every(layersConfirmed);
  const approvedTemplates = state.templateCandidates.filter((item) => item.status === "approved").length;
  const templatesReady = state.campaignWorkspace
    ? approvedTemplates > 0
    : productsReady();

  const done: Record<string, boolean> = {
    campaign: hasCampaign,
    layers: reviewed,
    products: templatesReady,
    generate: false,
  };
  const blocked: Record<string, string | null> = {
    campaign: null,
    layers: hasCampaign ? null : "Primero reúne y analiza el material de la campaña.",
    products: !hasCampaign
      ? "Primero reúne el material de la campaña."
      : reviewed ? null : "Primero genera y revisa el brief con IA.",
    generate: !hasCampaign
      ? "Primero reúne el material de la campaña."
      : state.campaignWorkspace && !approvedTemplates
        ? "Aprueba al menos una plantilla antes de producir."
        : null,
    results: null,
  };
  const next = STEP_ORDER.find((view) => !done[view]) || "generate";
  return { done, blocked, next };
}

/** Barra de pasos que encabeza cada vista del flujo. */
function stepBar(current: ViewName): string {
  const steps = stepState();
  const items = STEP_ORDER.map((view, index) => {
    const isCurrent = view === current;
    const isDone = steps.done[view];
    const locked = Boolean(steps.blocked[view]) && !isCurrent;
    const classes = [
      "step-chip",
      isCurrent ? "is-current" : "",
      isDone && !isCurrent ? "is-done" : "",
      locked ? "is-locked" : "",
    ].filter(Boolean).join(" ");
    return [
      '<button class="', classes, '" data-step="', attr(view), '"',
      locked ? ' title="' + attr(steps.blocked[view]!) + '"' : "",
      '><span class="step-num">', isDone && !isCurrent ? "✓" : String(index + 1),
      "</span><span>", esc(VIEW_LABELS[view]), "</span></button>",
    ].join("");
  }).join('<i class="step-sep" aria-hidden="true"></i>');
  return '<nav class="step-bar" aria-label="Pasos">' + items + "</nav>";
}

function bindStepBar(): void {
  queryAll<HTMLButtonElement>(".step-chip").forEach((button) => {
    button.addEventListener("click", () => {
      const view = button.dataset.step as ViewName;
      const reason = stepState().blocked[view];
      if (reason) {
        toast(reason, "error");
        return;
      }
      navigate(view);
    });
  });
}

/** Pie de paso: explica qué falta y ofrece el salto al siguiente. */
function stepFooter(current: ViewName, label: string): string {
  const index = STEP_ORDER.indexOf(current);
  const nextView = STEP_ORDER[index + 1];
  if (!nextView) return "";
  const reason = stepState().blocked[nextView];
  return [
    '<div class="step-footer">',
    reason
      ? '<span class="notice warning" style="flex:1">' + esc(reason) + "</span>"
      : '<span class="notice success" style="flex:1">Listo para continuar.</span>',
    '<button class="button large" id="step-next"', reason ? " disabled" : "", ">",
    esc(label), " →</button></div>",
  ].join("");
}

function bindStepFooter(current: ViewName): void {
  query("#step-next")?.addEventListener("click", () => {
    const nextView = STEP_ORDER[STEP_ORDER.indexOf(current) + 1];
    if (nextView) navigate(nextView);
  });
}

const CATEGORY_LABELS: Record<string, string> = {
  product: "Producto",
  person: "Persona",
  logo: "Logo",
  headline: "Titular",
  subheadline: "Subtítulo",
  price: "Precio",
  cta: "CTA",
  legal: "Legal",
  decoration: "Decoración",
  background: "Fondo",
};

const ROLE_LABELS: Record<string, string> = {
  product: "Producto original · eliminar y reemplazar",
  logo: "Obligatorio · logo",
  headline: "Obligatorio · titular",
  subheadline: "Obligatorio · subtítulo",
  price: "Obligatorio · precio o descuento",
  cta: "Obligatorio · CTA",
  legal: "Obligatorio · legal",
  person: "Persona · conservar",
  decoration: "Decoración · conservar",
  background: "Parte del fondo",
  ignore: "No usar",
};

const MANDATORY = new Set(["logo", "headline", "subheadline", "price", "cta", "legal"]);

interface State {
  view: ViewName;
  health: any;
  capabilities: Capabilities | null;
  projects: ProjectSummary[];
  campaignIds: string[];
  campaign: Project[];
  activeId: string | null;
  selectedLayerId: string | null;
  selectedFormats: Set<string>;
  formatPlatform: string;
  /** Qué cabe en cada formato, según el KV más apretado de la campaña. */
  formatFit: Record<string, FormatFit>;
  products: File[];
  individualProducts: Set<string>;
  groups: ProductGroup[];
  generationMode: "catalog" | "compose";
  /** Motor del fondo elegido. En el estado porque la lista de modelos lo sigue. */
  generationEngine: string;
  /** KV marcados para quitar en bloque. */
  kvPicks: Set<string>;
  /** Último KV marcado, para el rango con shift. */
  lastKvPick: string | null;
  /** Capas marcadas para borrar en bloque, por proyecto. */
  layerPicks: Record<string, Set<string>>;
  /** Última capa marcada, para el rango con shift. */
  lastLayerPick: string | null;
  selectedVariants: Set<string>;
  autoFormats: boolean;
  resultOrder: "score" | "generation";
  /** Qué propuestas se pintan. "clean" deja solo las que no traen avisos. */
  resultFilter: "all" | "clean";
  /** Copy y logos de cada KV, tal como los devuelve GET /texts. */
  texts: Record<string, ArtTexts>;
  /** KV sobre el que se definieron los textos por producto. */
  copySource: string | null;
  /** Elementos de ese KV cuyo texto cambia de un producto a otro. */
  copyFields: Set<string>;
  /** Texto de cada producto para cada uno de esos elementos. */
  productTexts: Record<string, Record<string, string>>;
  /** KV cuyo recuadro de producto se está dibujando en el paso 3. */
  zoneProject: string | null;
  /** Conservar el diseño del KV: una propuesta por formato, sin recomponer. */
  keepTemplate: boolean;
  /** Propuestas por cada formato elegido. */
  proposalsPerFormat: number;
  /** Lo escrito y todavía sin guardar en cada casilla de copy, por capa.
   *
   * La lista se vuelve a pintar entera cada vez que algo cambia en el KV
   * —separar una capa, quitar un elemento, cambiar de pieza—, y con ella se
   * perdía sin avisar lo que hubiera escrito y no hubiera guardado. */
  copyDrafts: Record<string, string>;
  /** Contexto que acompaña a todos los PSD y tandas de esta campaña. */
  campaignBrief: CampaignBrief;
  /** Cliente persistente y espacio de conocimiento de la campaña. */
  clients: ClientProfile[];
  activeClientId: string | null;
  campaignWorkspace: CampaignWorkspace | null;
  campaignSources: CampaignSource[];
  campaignIntelligence: CampaignIntelligence | null;
  templateCandidates: TemplateCandidate[];
  /** Filas de la matriz de producción cargada por el equipo. */
  productionMatrix: MatrixRow[];
  /** Compatibilidad por fila calculada por el servidor al validar la matriz. */
  productionMatrixPlans: MatrixProductionPlan[];
  /** Identificador de la matriz validada que vive en la campaña, no en el navegador. */
  productionMatrixDraftId: string | null;
  productionMatrixFile: File | null;
  productionBatch: ProductionBatch | null;
  /** Orden en curso; se conserva para que una recarga no haga repetirla. */
  productionTask: CampaignProductionTask | null;
  clientCampaigns: CampaignWorkspace[];
}

interface CampaignBrief {
  name: string;
  client: string;
  objective: string;
  notes: string;
  referenceUrl: string;
  referenceTitle: string;
  referencePosts: string[];
  referenceUrls: string[];
  styleGuide: string;
}

interface MatrixRow {
  rowNumber: number;
  product: string;
  image: string;
  headline: string;
  subtitle: string;
  price: string;
  previousPrice: string;
  installment: string;
  discount: string;
  cta: string;
  legal: string;
  validity: string;
  formats: string;
  proposals: number;
  notes: string;
  template: string;
  omit: string[];
}

function emptyCampaignBrief(client = ""): CampaignBrief {
  return {
    name: "",
    client,
    objective: "",
    notes: "",
    referenceUrl: "",
    referenceUrls: [],
    referenceTitle: "",
    referencePosts: [],
    styleGuide: "",
  };
}

const state: State = {
  view: "campaign",
  health: null,
  capabilities: null,
  projects: [],
  campaignIds: [],
  campaign: [],
  activeId: null,
  selectedLayerId: null,
  selectedFormats: new Set(["meta_feed_4_5", "meta_stories", "meta_reels"]),
  formatPlatform: "Todos",
  formatFit: {},
  products: [],
  individualProducts: new Set(),
  groups: [],
  generationMode: "catalog",
  generationEngine: "auto",
  kvPicks: new Set(),
  lastKvPick: null,
  layerPicks: {},
  lastLayerPick: null,
  selectedVariants: new Set(),
  autoFormats: true,
  resultOrder: "score",
  resultFilter: "all",
  texts: {},
  zoneProject: null,
  keepTemplate: true,
  proposalsPerFormat: 1,
  copyDrafts: {},
  copySource: null,
  copyFields: new Set(),
  productTexts: {},
  campaignBrief: emptyCampaignBrief(),
  clients: [],
  activeClientId: null,
  campaignWorkspace: null,
  campaignSources: [],
  campaignIntelligence: null,
  templateCandidates: [],
  productionMatrix: [],
  productionMatrixPlans: [],
  productionMatrixDraftId: null,
  productionMatrixFile: null,
  productionBatch: null,
  productionTask: null,
  clientCampaigns: [],
};

/** Todo lo que pertenece a una campaña se vacía junto. Mantener cualquiera de
 * estas colecciones al cambiar de cliente puede mezclar fotos o copy entre
 * marcas, algo mucho peor que obligar a volver a seleccionar un archivo. */
function resetCampaignWork(clientName = ""): void {
  state.campaignIds = [];
  state.campaign = [];
  state.activeId = null;
  state.selectedLayerId = null;
  state.campaignWorkspace = null;
  state.campaignSources = [];
  state.campaignIntelligence = null;
  state.templateCandidates = [];
  state.productionMatrix = [];
  state.productionMatrixPlans = [];
  state.productionMatrixDraftId = null;
  state.productionMatrixFile = null;
  state.productionBatch = null;
  state.productionTask = null;
  state.products = [];
  state.individualProducts = new Set();
  state.groups = [];
  state.campaignBrief = emptyCampaignBrief(clientName);
}

const ARRANGEMENT_OPTIONS: Record<string, string> = {
  auto: "Automática según el formato",
  horizontal: "En fila",
  vertical: "Apilados",
  overlap: "Superpuestos",
};

const productUrls = new WeakMap<File, string>();

const content = () => document.querySelector<HTMLElement>("#app-content")!;
const activeProject = () =>
  state.campaign.find((project) => project.project_id === state.activeId) ||
  state.campaign[0] ||
  null;

function esc(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function attr(value: unknown): string {
  return esc(value);
}

function checked(value: boolean): string {
  return value ? " checked" : "";
}

function selected(value: boolean): string {
  return value ? " selected" : "";
}

function query<T extends Element>(selector: string, root: ParentNode = document): T | null {
  return root.querySelector<T>(selector);
}

function queryAll<T extends Element>(selector: string, root: ParentNode = document): T[] {
  return Array.from(root.querySelectorAll<T>(selector));
}

function toast(message: string, kind: "success" | "error" | "info" = "info"): void {
  const region = query<HTMLElement>("#toast-region")!;
  if (Array.from(region.children).some((item) =>
    item.className === "toast " + kind && item.textContent === message,
  )) return;
  while (region.children.length >= 3) region.firstElementChild?.remove();
  const item = document.createElement("div");
  item.className = "toast " + kind;
  item.textContent = message;
  region.append(item);
  window.setTimeout(() => item.remove(), kind === "error" ? 8000 : 4200);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/* Confirmación propia en vez de la del navegador.
   El confirm() nativo sale rotulado con el dominio, no se puede maquetar y
   sobre todo no admite un "no volver a preguntar": limpiar una campaña de
   veinte KV obligaba a leer y aceptar el mismo aviso veinte veces. */
type ConfirmRequest = {
  title: string;
  lines?: string[];
  confirm?: string;
  cancel?: string;
  /* false = la acción no borra nada (el botón deja de ser rojo). */
  danger?: boolean;
  /* Recuerda el "sí" por TIPO de acción y nunca en general: aceptar "quitar un
     KV" no puede callar también el aviso de borrar la campaña entera. Sin esta
     clave, el diálogo pregunta siempre y no ofrece la casilla. */
  remember?: string;
};

const CONFIRM_SKIP_KEY = "creative-confirm-skip";
const confirmSkipped = new Set<string>(readConfirmSkips());

function readConfirmSkips(): string[] {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(CONFIRM_SKIP_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return []; /* modo privado: la preferencia vive solo en memoria */
  }
}

/* sessionStorage y no localStorage: "esta sesión" quiere decir esta sesión.
   Al cerrar la pestaña los avisos vuelven, que es lo que se espera de algo que
   borra archivos sin poder deshacerse. */
function rememberConfirmSkip(key: string): void {
  confirmSkipped.add(key);
  try {
    sessionStorage.setItem(CONFIRM_SKIP_KEY, JSON.stringify([...confirmSkipped]));
  } catch {
    /* ídem */
  }
}

export async function confirmAction(request: ConfirmRequest): Promise<boolean> {
  if (request.remember && confirmSkipped.has(request.remember)) return true;
  const overlay = query<HTMLElement>("#confirm-overlay");
  const title = query<HTMLElement>("#confirm-title");
  const body = query<HTMLElement>("#confirm-body");
  const ok = query<HTMLButtonElement>("#confirm-ok");
  const cancel = query<HTMLButtonElement>("#confirm-cancel");
  const rememberRow = query<HTMLElement>("#confirm-remember-row");
  const rememberBox = query<HTMLInputElement>("#confirm-remember");
  // Si el diálogo no está montado se cae al del navegador: preguntar feo es
  // mejor que dar por aceptado un borrado que nadie confirmó.
  if (!overlay || !title || !body || !ok || !cancel || !rememberRow || !rememberBox) {
    return window.confirm([request.title, ...(request.lines || [])].join("\n\n"));
  }

  title.textContent = request.title;
  body.innerHTML = (request.lines || []).map((line) => "<p>" + esc(line) + "</p>").join("");
  ok.textContent = request.confirm || "Sí, continuar";
  ok.className = request.danger === false ? "button" : "danger-button";
  cancel.textContent = request.cancel || "Cancelar";
  rememberRow.hidden = !request.remember;
  rememberBox.checked = false;

  const previous = document.activeElement as HTMLElement | null;
  overlay.hidden = false;
  // El foco arranca en Cancelar, no en el botón que borra: un Enter de más no
  // puede costar una campaña.
  cancel.focus();

  const stop = new AbortController();
  return new Promise<boolean>((resolve) => {
    const close = (value: boolean): void => {
      stop.abort();
      overlay.hidden = true;
      previous?.focus?.();
      resolve(value);
    };
    ok.addEventListener("click", () => {
      if (request.remember && rememberBox.checked) rememberConfirmSkip(request.remember);
      close(true);
    }, { signal: stop.signal });
    cancel.addEventListener("click", () => close(false), { signal: stop.signal });
    // Clic en el fondo = cancelar, como el resto de los modales de la app.
    overlay.addEventListener("mousedown", (event) => {
      if (event.target === overlay) close(false);
    }, { signal: stop.signal });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close(false);
        return;
      }
      // El Tab queda atrapado dentro del diálogo: con el fondo bloqueado, el
      // foco perdido allá atrás deja al teclado sin nada que hacer.
      if (event.key === "Tab") {
        const focusables: HTMLElement[] = request.remember
          ? [rememberBox, cancel, ok]
          : [cancel, ok];
        const index = focusables.indexOf(document.activeElement as HTMLElement);
        const next = (index + (event.shiftKey ? -1 : 1) + focusables.length) % focusables.length;
        event.preventDefault();
        focusables[next].focus();
      }
    }, { signal: stop.signal });
  });
}

function busy(title: string, detail: string, progress = 8): void {
  const overlay = query<HTMLElement>("#busy-overlay")!;
  overlay.hidden = false;
  query<HTMLElement>("#busy-title")!.textContent = title;
  query<HTMLElement>("#busy-detail")!.textContent = detail;
  query<HTMLElement>("#busy-progress")!.style.width = Math.max(3, Math.min(100, progress)) + "%";
}

function busyProgress(progress: number, detail: string): void {
  query<HTMLElement>("#busy-detail")!.textContent = detail;
  query<HTMLElement>("#busy-progress")!.style.width = Math.max(3, Math.min(100, progress)) + "%";
}

function idle(): void {
  query<HTMLElement>("#busy-overlay")!.hidden = true;
}

/* Conserva la campaña de esta pestaña también al recargar. */
function saveSession(): void {
  try {
    sessionStorage.setItem("creative-campaign", JSON.stringify(state.campaignIds));
    if (state.activeId) sessionStorage.setItem("creative-active", state.activeId);
    else sessionStorage.removeItem("creative-active");
    sessionStorage.setItem("creative-campaign-brief", JSON.stringify(state.campaignBrief));
    sessionStorage.setItem("creative-active-client", state.activeClientId || "");
    sessionStorage.setItem("creative-campaign-workspace", JSON.stringify(state.campaignWorkspace));
    sessionStorage.setItem("creative-campaign-sources", JSON.stringify(state.campaignSources));
    sessionStorage.setItem("creative-campaign-intelligence", JSON.stringify(state.campaignIntelligence));
    sessionStorage.setItem("creative-template-candidates", JSON.stringify(state.templateCandidates));
    sessionStorage.setItem("creative-production-matrix", JSON.stringify(state.productionMatrix));
    sessionStorage.setItem("creative-production-matrix-draft", state.productionMatrixDraftId || "");
    if (state.productionMatrixPlans.length) {
      sessionStorage.setItem("creative-production-matrix-plans", JSON.stringify(state.productionMatrixPlans));
    } else {
      sessionStorage.removeItem("creative-production-matrix-plans");
    }
    sessionStorage.setItem("creative-production-batch", JSON.stringify(state.productionBatch));
    sessionStorage.setItem("creative-production-task", JSON.stringify(state.productionTask));
  } catch {
    /* modo privado: la campaña solo vive en memoria */
  }
}

function pageHead(kicker: string, title: string, description: string, action = ""): string {
  return [
    '<div class="page-head"><div>',
    '<span class="kicker">', esc(kicker), "</span>",
    "<h1>", esc(title), "</h1>",
    "<p>", esc(description), "</p>",
    "</div>", action, "</div>",
  ].join("");
}

function emptyState(icon: string, title: string, description: string, action = ""): string {
  return [
    '<div class="empty-state"><span class="empty-icon">', icon, "</span>",
    "<strong>", esc(title), "</strong><span>", esc(description), "</span>",
    action ? '<div class="spacer"></div>' + action : "",
    "</div>",
  ].join("");
}

async function refreshProject(projectId: string): Promise<Project> {
  const project = await get<Project>("/projects/" + projectId);
  const index = state.campaign.findIndex((item) => item.project_id === projectId);
  if (index >= 0) state.campaign[index] = project;
  else state.campaign.push(project);
  // El inventario de copy depende de las capas: confirmar roles, analizar o
  // corregir un recorte lo cambia. Sin invalidar aquí se seguía enseñando el
  // anterior, con categorías y nombres que ya no eran los de la capa.
  delete state.texts[projectId];
  return project;
}

async function refreshAll(): Promise<void> {
  // El listado va acotado a la sesión: el trabajo de sesiones anteriores no se
  // ofrece porque el servidor lo borra por antigüedad.
  const [health, capabilities, projects, clients] = await Promise.all([
    get<any>("/health"),
    get<Capabilities>("/capabilities"),
    get<ProjectSummary[]>("/projects?session=" + encodeURIComponent(sessionId())),
    listClients(),
  ]);
  state.health = health;
  state.capabilities = capabilities;
  state.projects = projects;
  state.clients = clients;

  if (state.activeClientId) {
    state.clientCampaigns = await listCampaigns(state.activeClientId);
  } else {
    state.clientCampaigns = [];
  }
  if (state.activeClientId && state.campaignWorkspace?.campaign_id) {
    let recoveredCampaign: CampaignWorkspace | null = null;
    try {
      const recovered = await loadCampaignState(
        state.activeClientId,
        state.campaignWorkspace.campaign_id,
      );
      state.campaignWorkspace = recovered.workspace;
      state.campaignSources = recovered.sources;
      state.campaignIntelligence = recovered.brief;
      state.templateCandidates = recovered.candidates;
      recoveredCampaign = recovered.workspace;
      // Las fuentes de conocimiento del flujo nuevo nunca son Projects/KV. Una
      // sesión antigua podía conservar las 25 páginas del PDF en esta lista.
      state.campaignIds = [];
      state.campaign = [];
      state.activeId = null;
    } catch (error) {
      // Una caída transitoria, o una respuesta lenta durante un despliegue, no
      // puede convertir una campaña guardada en una pantalla vacía. Solo se
      // limpia el estado si el servidor confirma que esa campaña ya no existe.
      if (error instanceof ApiError && error.status === 404) {
        state.campaignWorkspace = null;
        state.campaignSources = [];
        state.campaignIntelligence = null;
        state.templateCandidates = [];
        state.productionMatrix = [];
        state.productionMatrixPlans = [];
        state.productionMatrixDraftId = null;
        state.productionMatrixFile = null;
        state.productionBatch = null;
        state.productionTask = null;
        state.campaignBrief = emptyCampaignBrief(
          state.clients.find((client) => client.client_id === state.activeClientId)?.name || "",
        );
        toast("Esta campaña ya no está disponible en el servidor.", "info");
      } else {
        toast(
          "No pudimos sincronizar la campaña ahora. Conservamos tu brief y plantillas; vuelve a actualizar en un momento.",
          "info",
        );
      }
    }

    // La galería es secundaria al espacio de trabajo: si este endpoint falla,
    // el brief y las plantillas recién recuperados siguen siendo válidos. Antes
    // ambos pedidos compartían un catch y un fallo de tandas borraba todo.
    if (recoveredCampaign) {
      try {
        const batches = await listProductionBatches(
          state.activeClientId,
          recoveredCampaign.campaign_id,
        );
        state.productionBatch = batches[0] || null;
      } catch {
        toast(
          "La campaña se cargó, pero no pudimos consultar las tandas todavía. Tus artes guardados siguen intactos.",
          "info",
        );
      }
      // Una recarga mientras se renderiza no necesita repetir la subida. La
      // orden persistida retoma su estado y, si ya concluyó, deja disponibles
      // los entregables sin esperar al siguiente clic de la persona.
      let activeTasks: CampaignProductionTask[] = [];
      try {
        activeTasks = await listCampaignProductionTasks(
          state.activeClientId,
          recoveredCampaign.campaign_id,
        );
      } catch {
        // El listado es una ayuda para recuperar otra sesión; no afecta las
        // tandas ni invalida una orden que ya tenga esta pestaña.
      }
      if (state.productionTask?.task_id) {
        try {
          const task = await getCampaignProductionTask(
            state.activeClientId,
            recoveredCampaign.campaign_id,
            state.productionTask.task_id,
          );
          state.productionTask = task;
          if (task.state === "COMPLETED" && task.result) {
            state.productionBatch = task.result;
            state.productionTask = null;
          }
        } catch {
          // La tanda permanece guardada en el servidor aunque una consulta
          // puntual falle; no se borra el task_id que permite retomarla.
        }
      } else if (activeTasks.length) {
        // Si otra persona (o este usuario desde otra pestaña) abrió la misma
        // campaña, la última orden activa sigue disponible para retomar.
        state.productionTask = activeTasks[0];
      }
    }
  }

  const existing = new Set(projects.map((item) => item.project_id));
  state.campaignIds = state.campaignIds.filter((id) => existing.has(id));
  if (!state.campaignIds.length && state.activeId && existing.has(state.activeId)) {
    state.campaignIds = [state.activeId];
  }
  state.campaign = await Promise.all(
    state.campaignIds.map((id) => get<Project>("/projects/" + id)),
  );
  state.formatFit = await peorCupoPorFormato(state.campaignIds);
  if (!state.activeId || !state.campaignIds.includes(state.activeId)) {
    state.activeId = state.campaignIds[0] || null;
  }
  saveSession();
  renderChrome();
}

function renderChrome(): void {
  query<HTMLElement>("#view-name")!.textContent = VIEW_LABELS[state.view];
  const steps = stepState();
  queryAll<HTMLButtonElement>(".nav-item").forEach((button) => {
    const view = button.dataset.view as ViewName;
    button.classList.toggle("is-active", view === state.view);
    button.classList.toggle("is-done", Boolean(steps.done[view]) && view !== state.view);
    const locked = Boolean(steps.blocked[view]) && view !== state.view;
    button.classList.toggle("is-locked", locked);
    if (locked) button.title = steps.blocked[view]!;
    else button.removeAttribute("title");
  });
  const status = query<HTMLElement>("#engine-status")!;
  const connected = state.health?.status === "ok";
  status.classList.toggle("is-offline", !connected);
  status.innerHTML = [
    '<span class="status-dot"></span><div><strong>',
    connected ? "Motor conectado" : "Motor sin conexión",
    "</strong><small>",
    // La versión la mueve una persona; el build se mueve solo en cada
    // despliegue. Con los dos a la vista se sabe si lo que estás mirando ya
    // trae el último cambio, sin tener que preguntarlo.
    connected
      ? "v" + esc(state.health.version || "?") + (state.health.build && state.health.build !== "local" ? " · " + esc(state.health.build) : "")
      : "Revisa backend y red",
    "</small></div>",
  ].join("");
  const live = query<HTMLElement>(".live-pill")!;
  // El texto va envuelto: en móvil el CSS oculta el <span> y deja solo el punto.
  live.innerHTML = '<i></i><span>' + (connected ? "Motor conectado" : "Sin conexión") + "</span>";

  const project = activeProject();
  const client = state.clients.find((item) => item.client_id === state.activeClientId);
  const panel = query<HTMLElement>("#sidebar-project")!;
  panel.innerHTML = state.campaignWorkspace
    ? [
        '<span class="eyebrow">', esc(client?.name || "CLIENTE"), '</span>',
        '<strong>', esc(state.campaignWorkspace.name), '</strong>',
        '<small>', String(state.campaignSources.length), ' fuentes · ',
        String(state.templateCandidates.filter((item) => item.status === "approved").length),
        ' plantillas aprobadas</small>',
      ].join("")
    : project
    ? [
        '<span class="eyebrow">CAMPAÑA · ', String(state.campaign.length), " KV</span>",
        "<strong>", esc(project.name), "</strong>",
        "<small>", String(project.canvas.width), "×", String(project.canvas.height),
        " · ", String(project.variants.length), " variantes</small>",
      ].join("")
    : '<span class="eyebrow">SIN CAMPAÑA</span><strong>Reúne el material</strong><small>Documentos, artes, manuales y redes</small>';
}

async function navigate(view: ViewName): Promise<void> {
  // Si se intenta saltar a un paso que aún no toca, se redirige al que falta en
  // vez de mostrar una pantalla vacía sin explicación.
  const reason = stepState().blocked[view];
  if (reason) {
    toast(reason, "error");
    view = stepState().next;
  }
  state.view = view;
  renderChrome();
  document.body.classList.remove("menu-open");
  closeViewer();
  content().innerHTML = '<div class="loading-state"><div class="loader"></div><strong>Cargando</strong></div>';
  try {
    if (view === "campaign") await renderCampaign();
    if (view === "layers") await renderLayers();
    if (view === "products") await renderProducts();
    if (view === "generate") await renderGenerate();
    if (view === "results") await renderResults();
  } catch (error) {
    content().innerHTML = emptyState(
      "!",
      "No pudimos cargar esta sección",
      errorMessage(error),
      '<button class="button" id="retry-view">Reintentar</button>',
    );
    query("#retry-view")?.addEventListener("click", () => navigate(view));
  }
}

/* El trabajo de esta sesión, en lista compacta con selección múltiple. No hay
   "trabajos guardados": lo de sesiones anteriores el servidor ya lo borró. */
function savedProjectsHtml(items: ProjectSummary[]): string {
  if (!items.length) return "";
  const rows = items.map((item) => [
    '<label class="saved-row"><input class="saved-check" type="checkbox" value="', attr(item.project_id), '">',
    '<img src="', attr(thumbnailUrl(item.project_id, 120)), '" alt="" loading="lazy" decoding="async">',
    '<span class="saved-copy"><strong>', esc(item.name), "</strong><small>",
    String(item.canvas.width), "×", String(item.canvas.height), " · ", String(item.layers),
    " capas · ", String(item.variants), " artes · ", esc(item.created_at.slice(0, 10)),
    "</small></span>",
    '<button type="button" class="ghost-button open-project" data-id="', attr(item.project_id), '">Abrir</button>',
    "</label>",
  ].join("")).join("");
  return [
    '<details class="card"><summary>Otros KV de esta sesión · ', String(items.length), "</summary>",
    '<p class="muted tiny" style="margin-top:10px">Ábrelos para seguir trabajándolos, o marca los que ya no sirvan y bórralos para liberar el servidor.</p>',
    '<div class="saved-list">', rows, "</div>",
    '<div class="button-row" style="margin-top:14px"><button class="ghost-button" id="saved-select-all">Marcar todos</button>',
    '<button class="danger-button" id="saved-delete" disabled>Borrar marcados</button>',
    '<span class="muted tiny right" id="saved-count">0 marcados</span></div></details>',
  ].join("");
}

async function renderCampaign(): Promise<void> {
  if (!state.clients.length) state.clients = await listClients();
  const hiddenKnowledgeProjects = new Set(
    state.campaignSources.flatMap((source) => source.legacy_project_ids || []),
  );
  const saved = state.projects.filter((item) =>
    !state.campaignIds.includes(item.project_id) && !hiddenKnowledgeProjects.has(item.project_id)
  );
  const ready = Boolean(state.campaignWorkspace || state.campaignSources.length);

  content().innerHTML = [
    stepBar("campaign"),
    pageHead(
      ready ? "Sistema creativo" : "Nueva campaña",
      ready ? "La campaña ya tiene contexto" : "Primero entendemos; después diseñamos",
      ready
        ? "El material se conserva como conocimiento. Las plantillas y los productos se trabajan en pasos separados."
        : "Selecciona el cliente, reúne todo el material y añade sus redes. La IA hará el brief y propondrá de 3 a 5 plantillas sin productos.",
      ready
        ? '<button class="ghost-button" id="clear-campaign">Cambiar campaña</button>'
        : "",
    ),
    campaignPhaseRail(),
    ready ? activeCampaignHtml() : campaignBriefHtml(false),
    ready ? stepFooter("campaign", "Revisar brief y activos") : "",
    '<div class="spacer"></div>',
    savedProjectsHtml(saved),
  ].join("");

  bindStepBar();
  bindStepFooter("campaign");
  bindSavedProjects();
  bindProjectCards();
  bindCampaignBrief();
  query("#clear-campaign")?.addEventListener("click", () => {
    const client = state.clients.find((item) => item.client_id === state.activeClientId);
    resetCampaignWork(client?.name || "");
    saveSession();
    navigate("campaign");
  });
  if (!ready) bindUpload();
  else bindActiveCampaign();
}

function campaignPhaseRail(): string {
  const phases = [
    ["1", "Cliente", Boolean(state.activeClientId)],
    ["2", "Material", state.campaignSources.length > 0],
    ["3", "Redes", state.campaignBrief.referenceUrls.length > 0],
    ["4", "Brief IA", Boolean(state.campaignIntelligence)],
    ["5", "Plantillas", state.templateCandidates.length > 0],
  ];
  let currentFound = false;
  return '<ol class="campaign-phase-rail">' + phases.map(([number, label, done]) => {
    const current = !currentFound && !done;
    if (current) currentFound = true;
    return '<li class="' + (done ? "is-done" : current ? "is-current" : "") + '"><i>' +
      (done ? "✓" : number) + '</i><span>' + label + '</span></li>';
  }).join("") + "</ol>";
}

function campaignBriefHtml(compact: boolean): string {
  const brief = state.campaignBrief;
  if (compact) return "";
  const intakeDisabled = state.activeClientId ? "" : " disabled";
  const clients = state.clients.map((client) => [
    '<button class="client-choice', client.client_id === state.activeClientId ? " is-selected" : "",
    '" type="button" data-client="', attr(client.client_id), '">',
    '<span class="client-monogram">', esc(client.name.slice(0, 2).toUpperCase()), '</span>',
    '<span><strong>', esc(client.name), '</strong><small>', String(client.templates), ' plantillas · ',
    String(client.campaigns), ' campañas</small></span><i>', client.client_id === state.activeClientId ? "✓" : "→", '</i></button>',
  ].join("")).join("");
  const pastCampaigns = state.activeClientId
    ? state.clientCampaigns.map((campaign) => [
        '<button class="campaign-memory-card" type="button" data-open-campaign="', attr(campaign.campaign_id), '">',
        '<span><strong>', esc(campaign.name), '</strong><small>', esc(campaign.objective || "Campaña guardada"),
        '</small></span><i>Continuar →</i></button>',
      ].join("")).join("")
    : "";
  return [
    '<div class="campaign-intake">',
    '<section class="card intake-section client-section"><div class="intake-number">1</div><div class="intake-body">',
    '<div class="card-head"><div><span class="kicker">MEMORIA POR CLIENTE</span><h2>¿Para quién es esta campaña?</h2>',
    '<p>Las plantillas aprobadas y tus correcciones quedarán guardadas en este cliente.</p></div></div>',
    clients ? '<div class="client-grid">' + clients + '</div>' : '<div class="notice">Todavía no hay clientes. Crea el primero.</div>',
    '<div class="create-client-row"><label class="field"><span>Nuevo cliente</span><input id="new-client-name" placeholder="Nombre de la marca"></label>',
    '<button class="ghost-button" id="create-client" type="button">Crear cliente</button></div>',
    state.activeClientId && pastCampaigns
      ? '<div class="campaign-memory"><span class="label">Campañas guardadas de este cliente</span>' + pastCampaigns + '</div>'
      : '',
    state.activeClientId ? '' : '<div class="notice compact">Selecciona o crea el cliente para habilitar el material de campaña.</div>',
    '</div></section>',

    '<section class="card intake-section"><div class="intake-number">2</div><div class="intake-body">',
    '<div class="card-head"><div><span class="kicker">FUENTES DE CONOCIMIENTO</span><h2>Sube todo lo que exista de la campaña</h2>',
    '<p>PDF, PPTX, PSD, artes, manuales, logos, textos y tipografías se analizan como contexto. Un PDF sigue siendo un documento; sus páginas no se convierten en campañas ni KV separados.</p></div></div>',
    '<div class="form-grid"><label class="field"><span>Nombre de campaña</span><input id="campaign-name" value="', attr(brief.name), '" placeholder="Ej. Credifest 2026"', intakeDisabled, '></label>',
    '<label class="field"><span>Objetivo conocido · opcional</span><input id="campaign-objective" value="', attr(brief.objective), '" placeholder="La IA lo completará si está vacío"', intakeDisabled, '></label></div>',
    '<label class="dropzone source-drop" id="artwork-drop"><input id="artwork-files" type="file" multiple accept=".psd,.psb,.pdf,.pptx,.docx,.xlsx,.csv,.tsv,.txt,.rtf,.md,.png,.jpg,.jpeg,.webp,.bmp,.gif,.tif,.tiff,.avif,.ttf,.otf"', intakeDisabled, '>',
    '<span class="drop-icon" id="drop-icon">⇧</span><strong class="drop-title" id="drop-title">Arrastra todo el material aquí</strong>',
    '<span class="drop-hint" id="drop-hint">Documentos, presentaciones, PSD, imágenes, logos, textos y tipografías</span></label>',
    '<div id="upload-summary" class="queue-summary" hidden></div><div id="upload-file-list" class="queue-list"></div>',
    '</div></section>',

    '<section class="card intake-section"><div class="intake-number">3</div><div class="intake-body">',
    '<div class="card-head"><div><span class="kicker">CONTEXTO PÚBLICO</span><h2>Añade las redes y sitios de la marca</h2>',
    '<p>Una URL por línea. Pega perfiles y, cuando los tengas, enlaces públicos de posts concretos: la IA contrastará productos, precios, titulares, CTA y legales.</p></div></div>',
    '<label class="field"><span>Perfiles y sitios</span><textarea id="campaign-reference" rows="4" placeholder="https://instagram.com/marca&#10;https://facebook.com/marca&#10;https://marca.com"', intakeDisabled, '>',
    esc((brief.referenceUrls?.length ? brief.referenceUrls : [brief.referenceUrl]).filter(Boolean).join("\n")), '</textarea></label>',
    '<div class="notice compact"><strong>Acceso público:</strong> si una red bloquea la lectura, la campaña continúa con los demás archivos y podrás añadir capturas.</div>',
    '</div></section>',

    '<section class="autopilot-launch"><div><span class="kicker">AUTOPILOT DE CAMPAÑA</span><h2>La IA clasifica, redacta el brief y propone 3–5 plantillas</h2>',
    '<p>Las propuestas no llevan productos. Reservan espacios adaptables para producto, precio, copy, CTA y legales según lo que realmente use esta campaña.</p></div>',
    '<button class="button large" id="upload-campaign" disabled>Analizar campaña con IA <span>→</span></button></section>',
    '</div>',
  ].join("");
}

function bindCampaignBrief(): void {
  queryAll<HTMLButtonElement>(".client-choice").forEach((button) => {
    button.addEventListener("click", async () => {
      const nextClientId = button.dataset.client || null;
      const changedClient = state.activeClientId !== nextClientId;
      state.activeClientId = nextClientId;
      const client = state.clients.find((item) => item.client_id === state.activeClientId);
      if (changedClient) resetCampaignWork(client?.name || "");
      else if (client) state.campaignBrief.client = client.name;
      queryAll<HTMLElement>(".client-choice").forEach((item) => {
        const selected = item.getAttribute("data-client") === state.activeClientId;
        item.classList.toggle("is-selected", selected);
        const mark = item.querySelector("i");
        if (mark) mark.textContent = selected ? "✓" : "→";
      });
      state.clientCampaigns = state.activeClientId
        ? await listCampaigns(state.activeClientId)
        : [];
      saveSession();
      await renderCampaign();
    });
  });
  queryAll<HTMLButtonElement>("[data-open-campaign]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!state.activeClientId) return;
      busy("Abriendo campaña", "Recuperando su brief, fuentes y plantillas…", 35);
      try {
        const client = state.clients.find((item) => item.client_id === state.activeClientId);
        resetCampaignWork(client?.name || "");
        const recovered = await loadCampaignState(
          state.activeClientId,
          button.dataset.openCampaign || "",
        );
        state.campaignWorkspace = recovered.workspace;
        state.campaignSources = recovered.sources;
        state.campaignIntelligence = recovered.brief;
        state.templateCandidates = recovered.candidates;
        state.campaignBrief.name = recovered.workspace.name;
        state.campaignBrief.objective = recovered.brief?.objective || recovered.workspace.objective || "";
        state.campaignBrief.referenceUrls = recovered.workspace.social_urls;
        state.campaignBrief.referenceUrl = recovered.workspace.social_urls[0] || "";
        const batches = await listProductionBatches(
          state.activeClientId,
          recovered.workspace.campaign_id,
        );
        state.productionBatch = batches[0] || null;
        saveSession();
        await navigate("campaign");
      } catch (error) {
        toast(errorMessage(error), "error");
      } finally {
        idle();
      }
    });
  });
  query("#create-client")?.addEventListener("click", async () => {
    const name = query<HTMLInputElement>("#new-client-name")?.value.trim() || "";
    if (!name) { toast("Escribe el nombre del cliente.", "error"); return; }
    busy("Creando cliente", "Preparando su biblioteca permanente…", 35);
    try {
      const client = await createClient(name, []);
      state.clients = [...state.clients.filter((item) => item.client_id !== client.client_id), client];
      state.activeClientId = client.client_id;
      state.clientCampaigns = [];
      resetCampaignWork(client.name);
      saveSession();
      await renderCampaign();
      toast("Cliente creado. Ya puedes reunir su campaña.", "success");
    } catch (error) { toast(errorMessage(error), "error"); } finally { idle(); }
  });
}

function parseSocialUrls(raw: string): string[] {
  return raw.split(/[\n,]/).map((value) => value.trim()).filter(Boolean).map((value) =>
    /^https?:\/\//i.test(value) ? value : "https://" + value
  ).filter((value, index, all) => all.indexOf(value) === index);
}

function socialUrlsFromField(): string[] {
  return parseSocialUrls(query<HTMLTextAreaElement>("#campaign-reference")?.value || "");
}

function sourceKindLabel(source: CampaignSource): string {
  const extension = (source.filename.split(".").pop() || "").toLowerCase();
  const exact: Record<string, string> = {
    pdf: "PDF", pptx: "PPTX", psd: "PSD", psb: "PSB", docx: "DOCX",
    xlsx: "XLSX", csv: "CSV", tsv: "TSV", ttf: "TTF", otf: "OTF",
  };
  if (exact[extension]) return exact[extension];
  const labels: Record<string, string> = {
    document: "Documento", presentation: "Presentación", layered_artwork: "Arte con capas",
    image: "Imagen", font: "Tipografía", text: "Texto", other: "Archivo",
  };
  return labels[source.kind] || "Archivo";
}

function sourceBrandingControlHtml(source: CampaignSource): string {
  // Solo una imagen limpia se puede componer como logo o fondo fijo. Un PDF o
  // un post completo seguirán siendo evidencia para el brief, no se copian por
  // accidente dentro de todas las piezas.
  if (source.kind !== "image") return "";
  const current = source.roles?.[0] || "visual_reference";
  const options: Array<[string, string]> = [
    ["visual_reference", "Referencia visual"],
    ["key_visual", "Key visual"],
    ["final_art", "Arte final / inspiración"],
    ["logo", "Logo fijo · conservar"],
    ["background", "Fondo o textura fija · conservar"],
    ["product_reference", "Referencia de producto"],
  ];
  return '<label class="source-branding-control"><span>Uso de este archivo</span><select class="source-role-select" data-source-id="' +
    attr(source.source_id) + '">' + options.map(([value, label]) => '<option value="' + attr(value) + '"' +
      (value === current ? " selected" : "") + '>' + esc(label) + '</option>').join("") +
    '</select><small>Logo y fondo fijo entran nítidos en las plantillas.</small></label>';
}

function activeCampaignHtml(): string {
  const workspace = state.campaignWorkspace;
  const approved = state.templateCandidates.filter((item) => item.status === "approved").length;
  const social = workspace?.social_urls || state.campaignBrief.referenceUrls || [];
  const sources = state.campaignSources.map((source) => [
    '<article class="source-card"><span class="source-format">', esc(sourceKindLabel(source)), '</span>',
    '<div><strong>', esc(source.filename), '</strong><small>',
    source.pages ? String(source.pages) + " páginas · " : "",
    source.bytes ? readableSize(source.bytes) + " · " : "",
    esc(source.role || source.summary || "Lista para el análisis"), '</small></div>',
    '<span class="source-state ', source.status === "error" ? "error" : '', '">',
    source.status === "error" ? "Error" : source.status === "warning" ? "Revisar" : "✓ Analizada", '</span>',
    sourceBrandingControlHtml(source),
    '<button class="source-remove" type="button" data-source-id="', attr(source.source_id), '" aria-label="Quitar ', attr(source.filename), '" title="Quitar esta fuente">×</button></article>',
  ].join("")).join("");
  return [
    '<section class="campaign-summary knowledge-summary"><div><span class="kicker">CAMPAÑA ACTIVA</span><h2>',
    esc(workspace?.name || state.campaignBrief.name || "Campaña"), '</h2><p>',
    esc(state.campaignBrief.client || "Cliente"), state.campaignBrief.objective ? ' · ' + esc(state.campaignBrief.objective) : '',
    '</p></div><div class="campaign-summary-actions"><button class="ghost-button" id="reanalyze-campaign">',
    state.campaignIntelligence ? "Volver a analizar" : "Generar brief con IA", '</button></div></section>',
    '<div class="stat-row knowledge-stats"><div class="stat"><strong>', String(state.campaignSources.length), '</strong><span>fuentes, no KV</span></div>',
    '<div class="stat"><strong>', String(social.length), '</strong><span>redes y sitios</span></div>',
    '<div class="stat"><strong>', state.campaignIntelligence ? "✓" : "—", '</strong><span>brief IA</span></div>',
    '<div class="stat"><strong>', String(approved), '/', String(state.templateCandidates.length), '</strong><span>plantillas aprobadas</span></div></div>',
    '<div class="campaign-active-grid"><section class="card"><div class="card-head"><div><h2>Material de campaña</h2>',
    '<p>Cada archivo conserva su función como fuente de conocimiento. Sube logos y fondos limpios como PNG/JPG para fijarlos en todas las plantillas.</p></div><label class="ghost-button file-button">Añadir material<input id="add-source-files" type="file" multiple accept=".psd,.psb,.pdf,.pptx,.docx,.xlsx,.csv,.tsv,.txt,.rtf,.md,.png,.jpg,.jpeg,.webp,.bmp,.gif,.tif,.tiff,.avif,.ttf,.otf"></label></div>',
    '<div class="source-list">', sources || '<div class="notice">No hay fuentes guardadas todavía.</div>', '</div></section>',
    '<section class="card"><div class="card-head"><div><h2>Contexto conectado</h2><p>Perfiles y posts públicos que completan el lenguaje visual.</p></div></div>',
    social.length ? '<div class="social-url-list">' + social.map((url) => '<a href="' + attr(url) + '" target="_blank" rel="noreferrer"><i>↗</i><span>' + esc(url.replace(/^https?:\/\//, "")) + '</span></a>').join("") + '</div>' : '<div class="notice">No se añadieron redes; el análisis usa solo los archivos.</div>',
    '<details class="context-editor"><summary>Añadir o corregir perfiles</summary><label class="field"><span>Una URL por línea</span><textarea id="connected-socials" rows="4">', esc(social.join("\n")), '</textarea></label>',
    '<button class="ghost-button" id="save-social-context">Guardar y reanalizar</button></details>',
    '<div class="button-row campaign-route-actions"><button class="button" id="open-brief"', state.campaignIntelligence ? "" : " disabled", '>Revisar brief</button>',
    '<button class="ghost-button" id="open-templates"', state.templateCandidates.length ? "" : " disabled", '>Ver propuestas</button></div></section></div>',
  ].join("");
}

async function analyzeCurrentCampaign(): Promise<void> {
  if (!state.activeClientId || !state.campaignWorkspace) return;
  busy("Entendiendo la campaña", "Clasificando estrategia, artes, recursos y referencias…", 58);
  try {
    const result = await generateCampaignBrief(state.activeClientId, state.campaignWorkspace.campaign_id);
    if (!result) throw new Error("Este servidor aún no tiene habilitado el análisis integral de campaña.");
    const recovered = await loadCampaignState(
      state.activeClientId,
      state.campaignWorkspace.campaign_id,
    );
    state.campaignWorkspace = recovered.workspace;
    state.campaignSources = recovered.sources;
    state.campaignIntelligence = recovered.brief || result.brief;
    state.templateCandidates = recovered.candidates.length ? recovered.candidates : result.template_candidates;
    state.productionMatrixPlans = [];
    state.campaignBrief.objective = result.brief.objective || state.campaignBrief.objective;
    state.campaignBrief.notes = result.brief.summary || state.campaignBrief.notes;
    state.campaignBrief.styleGuide = result.brief.visual_rules.join("\n");
    saveSession();
    result.warnings.forEach((warning) => toast(warning, "info"));
    toast("Brief listo y " + String(result.template_candidates.length) + " plantillas propuestas.", "success");
    await navigate("layers");
  } catch (error) { toast(errorMessage(error), "error"); } finally { idle(); }
}

function bindActiveCampaign(): void {
  query("#open-brief")?.addEventListener("click", () => navigate("layers"));
  query("#open-templates")?.addEventListener("click", () => navigate("products"));
  query("#reanalyze-campaign")?.addEventListener("click", () => void analyzeCurrentCampaign());
  query("#save-social-context")?.addEventListener("click", async () => {
    if (!state.activeClientId || !state.campaignWorkspace) return;
    const urls = parseSocialUrls(query<HTMLTextAreaElement>("#connected-socials")?.value || "");
    busy("Actualizando referencias", "Guardando los perfiles del cliente…", 25);
    try {
      state.campaignWorkspace = await updateCampaignContext(
        state.activeClientId,
        state.campaignWorkspace.campaign_id,
        { social_urls: urls },
      );
      state.campaignBrief.referenceUrls = urls;
      state.campaignBrief.referenceUrl = urls[0] || "";
      state.campaignIntelligence = null;
      state.templateCandidates = [];
      state.productionMatrixPlans = [];
      saveSession();
      idle();
      await analyzeCurrentCampaign();
    } catch (error) {
      toast(errorMessage(error), "error");
      idle();
    }
  });
  query<HTMLInputElement>("#add-source-files")?.addEventListener("change", async (event) => {
    const files = Array.from((event.currentTarget as HTMLInputElement).files || []);
    if (!files.length || !state.activeClientId || !state.campaignWorkspace) return;
    busy("Añadiendo material", "Guardando nuevas fuentes de conocimiento…", 10);
    try {
      const result = await uploadCampaignSources(
        state.activeClientId, state.campaignWorkspace.campaign_id, files,
        (sent, total) => busyProgress(total ? 10 + Math.round(sent / total * 70) : 30, "Subiendo material…"),
        () => busyProgress(84, "Clasificando los archivos…"),
      );
      if (!result) throw new Error("Este servidor aún no admite añadir fuentes a la campaña.");
      const byId = new Map(state.campaignSources.map((source) => [source.source_id, source]));
      result.sources.forEach((source) => byId.set(source.source_id, source));
      state.campaignSources = [...byId.values()];
      state.campaignIntelligence = null;
      state.templateCandidates = [];
      state.productionMatrixPlans = [];
      saveSession();
      toast("Material añadido. Vuelve a analizar para actualizar el brief.", "success");
      await renderCampaign();
    } catch (error) { toast(errorMessage(error), "error"); } finally { idle(); }
  });
  queryAll<HTMLSelectElement>(".source-role-select").forEach((select) => {
    select.addEventListener("change", async () => {
      if (!state.activeClientId || !state.campaignWorkspace || !select.dataset.sourceId) return;
      const source = state.campaignSources.find((item) => item.source_id === select.dataset.sourceId);
      if (!source) return;
      busy("Fijando branding", "Guardando el elemento obligatorio y reconstruyendo las plantillas…", 28);
      try {
        const updated = await updateCampaignSourceRole(
          state.activeClientId,
          state.campaignWorkspace.campaign_id,
          source.source_id,
          select.value,
        );
        state.campaignSources = state.campaignSources.map((item) =>
          item.source_id === updated.source_id ? updated : item,
        );
        state.campaignIntelligence = null;
        state.templateCandidates = [];
        state.productionMatrixPlans = [];
        saveSession();
        idle();
        await analyzeCurrentCampaign();
      } catch (error) {
        select.value = source.roles?.[0] || "visual_reference";
        toast(errorMessage(error), "error");
        idle();
      }
    });
  });
  queryAll<HTMLButtonElement>(".source-remove").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!state.activeClientId || !state.campaignWorkspace) return;
      const source = state.campaignSources.find((item) => item.source_id === button.dataset.sourceId);
      if (!source) return;
      const confirmed = await confirmAction({
        title: "¿Quitar esta fuente?",
        lines: [
          "Se eliminará “" + source.filename + "” de la campaña.",
          "El brief y las propuestas se volverán a generar sin este material.",
        ],
        confirm: "Sí, quitar fuente",
        remember: "quitar-fuente-campana",
      });
      if (!confirmed) return;
      busy("Quitando material", "Actualizando la base de conocimiento de la campaña…", 45);
      try {
        await deleteCampaignSource(
          state.activeClientId,
          state.campaignWorkspace.campaign_id,
          source.source_id,
        );
        const recovered = await loadCampaignState(
          state.activeClientId,
          state.campaignWorkspace.campaign_id,
        );
        state.campaignWorkspace = recovered.workspace;
        state.campaignSources = recovered.sources;
        state.campaignIntelligence = recovered.brief;
        state.templateCandidates = recovered.candidates;
        state.productionMatrixPlans = [];
        saveSession();
        toast("Fuente quitada. Vuelve a analizar para reconstruir el brief.", "success");
        await renderCampaign();
      } catch (error) {
        toast(errorMessage(error), "error");
      } finally {
        idle();
      }
    });
  });
}

function bindSavedProjects(): void {
  const checks = queryAll<HTMLInputElement>(".saved-check");
  const deleteButton = query<HTMLButtonElement>("#saved-delete");
  const counter = query<HTMLElement>("#saved-count");
  if (!checks.length || !deleteButton || !counter) return;

  const sync = () => {
    const marked = checks.filter((item) => item.checked).length;
    deleteButton.disabled = marked === 0;
    counter.textContent = String(marked) + (marked === 1 ? " marcado" : " marcados");
  };
  checks.forEach((check) => check.addEventListener("change", sync));

  query("#saved-select-all")?.addEventListener("click", () => {
    const allMarked = checks.every((item) => item.checked);
    checks.forEach((item) => { item.checked = !allMarked; });
    sync();
  });

  deleteButton.addEventListener("click", async () => {
    const ids = checks.filter((item) => item.checked).map((item) => item.value);
    if (!ids.length) return;
    const confirmed = await confirmAction({
      title: ids.length === 1
        ? "¿Borrar este proyecto?"
        : "¿Borrar " + String(ids.length) + " proyectos?",
      lines: ["Se van con todos sus archivos y no se puede deshacer."],
      confirm: ids.length === 1 ? "Sí, borrar" : "Sí, borrar los " + String(ids.length),
      remember: "borrar-proyecto",
    });
    if (!confirmed) return;
    // El backend solo borra de uno en uno, así que se recorre la selección y se
    // informa de los que fallen en vez de dar por hecho que se fueron todos.
    busy("Borrando proyectos", "Liberando espacio…", 5);
    const failed: string[] = [];
    try {
      for (let index = 0; index < ids.length; index += 1) {
        busyProgress(Math.round((index / ids.length) * 92) + 4, String(index + 1) + " de " + String(ids.length));
        try {
          await del("/projects/" + ids[index]);
        } catch {
          failed.push(ids[index]);
        }
      }
      state.campaignIds = state.campaignIds.filter((id) => !ids.includes(id));
      state.campaign = state.campaign.filter((item) => !ids.includes(item.project_id));
      if (state.activeId && ids.includes(state.activeId)) {
        state.activeId = state.campaignIds[0] || null;
      }
      saveSession();
      await refreshAll();
      const removed = ids.length - failed.length;
      if (removed) toast(String(removed) + " proyecto(s) eliminado(s).", "success");
      if (failed.length) toast("No se pudieron borrar " + String(failed.length) + " proyecto(s).", "error");
      await navigate("campaign");
    } finally {
      idle();
    }
  });
}

/** Marcar KV y quitarlos en bloque.
 *
 *  Un PSD de agencia entra como veinte piezas —portadas, versiones, tamaños— y
 *  casi nunca se quieren las veinte. Quitarlas con el aspa era una a una, con su
 *  confirmación cada vez: veinte diálogos para empezar a trabajar. */
function bindKvPicks(): void {
  const repintar = () => void renderLayers();
  // El rango se lee de la pantalla, que es el orden en que están las fichas.
  const orden = queryAll<HTMLInputElement>(".kv-pick-box")
    .map((box) => box.dataset.kvPick || "")
    .filter(Boolean);

  queryAll<HTMLInputElement>(".kv-pick-box").forEach((box) => {
    box.addEventListener("click", (event) => {
      const id = box.dataset.kvPick!;
      if ((event as MouseEvent).shiftKey && state.lastKvPick) {
        const desde = orden.indexOf(state.lastKvPick);
        const hasta = orden.indexOf(id);
        if (desde >= 0 && hasta >= 0) {
          const [a, b] = desde < hasta ? [desde, hasta] : [hasta, desde];
          for (const entre of orden.slice(a, b + 1)) {
            if (box.checked) state.kvPicks.add(entre);
            else state.kvPicks.delete(entre);
          }
        }
      } else if (box.checked) {
        state.kvPicks.add(id);
      } else {
        state.kvPicks.delete(id);
      }
      state.lastKvPick = id;
      repintar();
    });
  });

  query("#pick-all-kv")?.addEventListener("click", () => {
    for (const item of state.campaign) state.kvPicks.add(item.project_id);
    repintar();
  });
  query("#clear-kv-picks")?.addEventListener("click", () => {
    state.kvPicks.clear();
    state.lastKvPick = null;
    repintar();
  });
  query("#drop-kv-picks")?.addEventListener("click", () => void dropKvPicks());
}

async function dropKvPicks(): Promise<void> {
  const marcados = state.campaign.filter((item) => state.kvPicks.has(item.project_id));
  if (!marcados.length) return;
  const quedan = state.campaign.length - marcados.length;
  const nombres = marcados.map((item) => item.name);
  const confirmado = await confirmAction({
    title: "¿Quitar " + String(marcados.length) + " KV de la campaña?",
    lines: [
      nombres.slice(0, 5).join(", ") +
        (nombres.length > 5 ? " y " + String(nombres.length - 5) + " más." : "."),
      quedan
        ? "Quedan " + String(quedan) + " en la campaña."
        : "No queda ninguno: vuelves a la pantalla de carga.",
      "Se borran con sus archivos y no se puede deshacer.",
    ],
    confirm: "Sí, quitar los " + String(marcados.length),
  });
  if (!confirmado) return;

  const ids = marcados.map((item) => item.project_id);
  busy("Quitando KV", String(ids.length) + " piezas de la campaña", 30);
  try {
    // Una sola petición: veinte DELETE seguidos tardaban y dejaban la campaña a
    // medias si una fallaba.
    const resultado = await post<any>("/projects/delete", { project_ids: ids });
    const fuera = new Set<string>([...(resultado.removed || []), ...(resultado.missing || [])]);
    // El siguiente activo: el primero que sobreviva a partir de donde estabas.
    const posicion = state.campaignIds.findIndex((id) => fuera.has(id));
    state.campaignIds = state.campaignIds.filter((id) => !fuera.has(id));
    state.campaign = state.campaign.filter((item) => !fuera.has(item.project_id));
    for (const id of fuera) delete state.texts[id];
    state.kvPicks.clear();
    state.lastKvPick = null;
    if (!state.activeId || fuera.has(state.activeId)) {
      state.activeId =
        state.campaignIds[Math.min(Math.max(0, posicion), state.campaignIds.length - 1)] || null;
      state.selectedLayerId = null;
    }
    saveSession();
    await refreshAll();
    toast(String(resultado.removed_count || 0) + " KV quitados de la campaña.", "success");
    if (!state.campaignIds.length) {
      await navigate("campaign");
      return;
    }
    await renderLayers();
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    idle();
  }
}

async function dropKv(projectId: string): Promise<void> {
  const project = state.campaign.find((item) => item.project_id === projectId);
  if (!project) return;
  const last = state.campaignIds.length <= 1;
  const warning = last
    ? "Es el último KV de la campaña: al quitarlo vuelves a la carga."
    : "Los otros " + String(state.campaignIds.length - 1) + " no se tocan.";
  const confirmed = await confirmAction({
    title: "¿Quitar «" + project.name + "» de la campaña?",
    lines: [warning, "Se borra con sus archivos y no se puede deshacer."],
    confirm: "Sí, quitar",
    // Si es el último KV el aviso es distinto (se vuelve a la carga), así que
    // ese caso se pregunta siempre: no lo tapa el "no volver a preguntar".
    remember: last ? undefined : "quitar-kv",
  });
  if (!confirmed) return;

  busy("Quitando el KV", project.name, 40);
  try {
    await del("/projects/" + projectId);
    // El siguiente de la lista, no el primero: quitar el KV que estabas
    // mirando te dejaba al principio de veinte y había que volver a buscar.
    const position = state.campaignIds.indexOf(projectId);
    state.campaignIds = state.campaignIds.filter((id) => id !== projectId);
    state.campaign = state.campaign.filter((item) => item.project_id !== projectId);
    delete state.texts[projectId];
    if (state.activeId === projectId) {
      state.activeId = state.campaignIds[Math.min(position, state.campaignIds.length - 1)] || null;
      state.selectedLayerId = null;
    }
    saveSession();
    await refreshAll();
    toast("«" + project.name + "» quitado de la campaña.", "success");
    await navigate(state.campaignIds.length ? "layers" : "campaign");
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    idle();
  }
}


function bindProjectCards(): void {
  queryAll<HTMLButtonElement>(".open-project").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = button.dataset.id!;
      state.activeId = id;
      if (!state.campaignIds.includes(id)) state.campaignIds.push(id);
      await refreshProject(id);
      saveSession();
      await navigate("layers");
    });
  });
  queryAll<HTMLButtonElement>(".delete-project").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = button.dataset.id!;
      const confirmed = await confirmAction({
        title: "¿Borrar este proyecto?",
        lines: ["Se va con todos sus archivos y no se puede deshacer."],
        confirm: "Sí, borrar",
        remember: "borrar-proyecto",
      });
      if (!confirmed) return;
      try {
        await del("/projects/" + id);
        state.campaignIds = state.campaignIds.filter((item) => item !== id);
        state.campaign = state.campaign.filter((item) => item.project_id !== id);
        if (state.activeId === id) state.activeId = state.campaignIds[0] || null;
        await refreshAll();
        toast("Proyecto eliminado.", "success");
        await navigate("campaign");
      } catch (error) {
        toast(errorMessage(error), "error");
      }
    });
  });
}

const ARTWORK_EXTENSIONS = /\.(psd|psb|pdf|pptx|docx|xlsx|csv|tsv|txt|rtf|md|png|jpe?g|webp|bmp|gif|tiff?|avif|ttf|otf)$/i;
const PRODUCT_IMAGE_EXTENSIONS = /\.(png|jpe?g|webp|bmp|gif|tiff?|avif)$/i;
const HEIC_EXTENSIONS = /\.(heic|heif)$/i;

/** Tamaño legible. En MB con un decimal un PNG de 40 KB sale como "0.0 MB",
 *  que es peor que no poner nada: parece que el archivo está vacío. */
function readableSize(bytes: number): string {
  if (bytes < 1024) return String(bytes) + " B";
  if (bytes < 1024 * 1024) return String(Math.round(bytes / 1024)) + " KB";
  const mb = bytes / 1024 / 1024;
  return mb.toFixed(mb < 10 ? 1 : 0) + " MB";
}

/** Ficha de un archivo en cola: se ve qué es antes de subir nada.
 *
 * Del PSD no se puede sacar miniatura en el navegador —hay que leerlo capa por
 * capa, y eso lo hace el servidor—, así que en su lugar va el rótulo del
 * formato. Lo importante es que no se quede un hueco: un archivo elegido tiene
 * que verse elegido. */
function queueCard(file: File, index: number): string {
  const isLayered = /\.(psd|psb)$/i.test(file.name);
  const isImage = /\.(png|jpe?g|webp|tiff?|avif|svg)$/i.test(file.name);
  const extension = (file.name.split(".").pop() || "FILE").toUpperCase();
  const media = isImage
    ? '<img src="' + attr(productUrl(file)) + '" alt="">'
    : '<div class="queue-badge">' + esc(extension) + "</div>";
  return [
    '<article class="queue-card">', media,
    '<div class="queue-meta"><strong>', esc(file.name), "</strong><span>",
    readableSize(file.size),
    isLayered ? " · capas para analizar" : isImage ? " · referencia visual" : " · fuente de contexto",
    "</span></div>",
    '<button type="button" class="queue-drop" data-index="', String(index),
    '" title="Quitar de la cola" aria-label="Quitar ', attr(file.name), '">×</button>',
    "</article>",
  ].join("");
}

function bindUpload(): void {
  const input = query<HTMLInputElement>("#artwork-files")!;
  const zone = query<HTMLElement>("#artwork-drop")!;
  const icon = query<HTMLElement>("#drop-icon")!;
  const title = query<HTMLElement>("#drop-title")!;
  const hint = query<HTMLElement>("#drop-hint")!;
  const button = query<HTMLButtonElement>("#upload-campaign")!;
  const list = query<HTMLElement>("#upload-file-list")!;
  const summary = query<HTMLElement>("#upload-summary")!;
  let queuedFiles: File[] = [];

  const renderQueue = (): void => {
    const total = queuedFiles.reduce((sum, file) => sum + file.size, 0);
    const campaignName = query<HTMLInputElement>("#campaign-name")?.value.trim() || "";
    const objective = query<HTMLInputElement>("#campaign-objective")?.value.trim() || "";
    const urls = socialUrlsFromField();
    const hasEvidence = queuedFiles.length > 0 || Boolean(objective) || urls.length > 0;
    button.disabled = !hasEvidence || !state.activeClientId || !campaignName;
    button.innerHTML = queuedFiles.length
      ? "Analizar " + String(queuedFiles.length) + (queuedFiles.length === 1 ? " fuente" : " fuentes") + " con IA <span>→</span>"
      : "Analizar campaña con IA <span>→</span>";
    const conCola = queuedFiles.length > 0;
    zone.classList.toggle("slim", conCola);
    icon.textContent = conCola ? "+" : "⇧";
    zone.setAttribute("aria-label", conCola ? "Añadir más material" : "");
    zone.title = conCola ? "Añadir más material" : "";
    title.textContent = conCola ? "Añadir más material" : "Arrastra todo el material aquí";
    hint.textContent = conCola
      ? ""
      : "Documentos, presentaciones, PSD, imágenes, logos, textos y tipografías";
    summary.hidden = queuedFiles.length === 0;
    summary.innerHTML = queuedFiles.length
      ? "<strong>" + String(queuedFiles.length) + (queuedFiles.length === 1 ? " archivo" : " archivos") +
        " en cola</strong><span>" + readableSize(total) + " en total</span>"
      : "";
    list.innerHTML = queuedFiles.map(queueCard).join("");
    queryAll<HTMLButtonElement>(".queue-drop").forEach((item) => {
      item.addEventListener("click", () => {
        queuedFiles.splice(Number(item.dataset.index), 1);
        renderQueue();
      });
    });
  };

  const enqueue = (incoming: File[]): void => {
    const accepted = incoming.filter((file) => ARTWORK_EXTENSIONS.test(file.name));
    const rejected = incoming.filter((file) => !ARTWORK_EXTENSIONS.test(file.name));
    const before = queuedFiles.length;
    queuedFiles = mergeUniqueFiles(queuedFiles, accepted);
    renderQueue();
    if (rejected.length) {
      toast(
        "No se admite " + rejected.map((file) => file.name).join(", ") +
        ". Usa documentos, presentaciones, PSD, imágenes, textos o tipografías estándar.",
        "error",
      );
    }
    // Si el archivo ya estaba en la cola no se duplica, pero en silencio parece
    // que el clic no hizo nada.
    if (accepted.length && queuedFiles.length === before) {
      toast("Ese archivo ya estaba en la cola.", "info");
    }
  };

  input.addEventListener("change", () => {
    enqueue(Array.from(input.files || []));
    input.value = "";
  });
  query<HTMLInputElement>("#campaign-name")?.addEventListener("input", renderQueue);
  query<HTMLInputElement>("#campaign-objective")?.addEventListener("input", renderQueue);
  query<HTMLTextAreaElement>("#campaign-reference")?.addEventListener("input", renderQueue);
  button.addEventListener("sync", renderQueue);

  // El input transparente cubre toda la zona, así que el navegador ya acepta el
  // archivo soltado. Lo que faltaba era que se viera: sin esto, arrastrar sobre
  // un recuadro que dice "arrastra aquí" no cambia nada en pantalla.
  ["dragenter", "dragover"].forEach((name) => {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add("is-dragging");
    });
  });
  ["dragleave", "dragend", "drop"].forEach((name) => {
    zone.addEventListener(name, () => zone.classList.remove("is-dragging"));
  });
  zone.addEventListener("drop", (event) => {
    const dropped = Array.from((event as DragEvent).dataTransfer?.files || []);
    if (!dropped.length) return;
    event.preventDefault();
    enqueue(dropped);
  });

  renderQueue();

  button.addEventListener("click", async () => {
    const files = queuedFiles;
    if (!state.activeClientId) return;
    const field = (id: string) => query<HTMLInputElement | HTMLTextAreaElement>(id)?.value.trim() || "";
    const socialUrls = socialUrlsFromField();
    const client = state.clients.find((item) => item.client_id === state.activeClientId);
    state.campaignBrief = {
      ...state.campaignBrief,
      name: field("#campaign-name") || state.campaignBrief.name,
      client: client?.name || state.campaignBrief.client,
      objective: field("#campaign-objective") || state.campaignBrief.objective,
      referenceUrl: socialUrls[0] || "",
      referenceUrls: socialUrls,
    };
    saveSession();
    busy("Creando la campaña", "Preparando la biblioteca del cliente…", 4);
    try {
      const workspace = await createCampaign(state.activeClientId, {
        name: state.campaignBrief.name,
        objective: state.campaignBrief.objective,
        social_urls: socialUrls,
      });
      if (!workspace) {
        throw new Error("El servidor todavía no tiene el nuevo espacio de campañas. Actualiza el despliegue y vuelve a intentar.");
      }
      state.campaignWorkspace = workspace;
      saveSession();
      if (files.length) {
        const uploaded = await uploadCampaignSources(
          state.activeClientId,
          workspace.campaign_id,
          files,
          (sent, total) => busyProgress(
            total ? 8 + Math.round(sent / total * 62) : 28,
            total ? "Subiendo " + readableSize(sent) + " de " + readableSize(total) : "Subiendo material…",
          ),
          () => busyProgress(74, "Extrayendo texto, imágenes, capas y señales visuales…"),
        );
        if (!uploaded) {
          throw new Error("El servidor todavía no puede guardar documentos como fuentes de campaña.");
        }
        state.campaignSources = uploaded.sources;
        uploaded.warnings.forEach((warning) => toast(warning, "info"));
      } else {
        state.campaignSources = [];
      }
      saveSession();
      busyProgress(82, "Construyendo el brief y las plantillas sin productos…");
      const analysis = await generateCampaignBrief(state.activeClientId, workspace.campaign_id);
      if (!analysis) {
        toast("El material quedó guardado, pero el análisis IA aún no está disponible en este despliegue.", "info");
        await renderCampaign();
        return;
      }
      const recovered = await loadCampaignState(state.activeClientId, workspace.campaign_id);
      state.campaignWorkspace = recovered.workspace;
      state.campaignSources = recovered.sources;
      state.campaignIntelligence = recovered.brief || analysis.brief;
      state.templateCandidates = recovered.candidates.length
        ? recovered.candidates
        : analysis.template_candidates;
      state.campaignBrief.objective = analysis.brief.objective || state.campaignBrief.objective;
      state.campaignBrief.notes = analysis.brief.summary || state.campaignBrief.notes;
      state.campaignBrief.styleGuide = analysis.brief.visual_rules.join("\n");
      saveSession();
      analysis.warnings.forEach((warning) => toast(warning, "info"));
      toast("Campaña entendida: brief listo y " + String(analysis.template_candidates.length) + " plantillas propuestas.", "success");
      await navigate("layers");
    } catch (error) {
      toast(errorMessage(error), "error");
      if (state.campaignWorkspace) await renderCampaign();
    } finally {
      idle();
    }
  });
}

export async function mountApp(): Promise<void> {
  try {
    const saved = JSON.parse(sessionStorage.getItem("creative-campaign") || "[]");
    state.campaignIds = Array.isArray(saved)
      ? saved.filter((id): id is string => typeof id === "string" && id.length > 0)
      : [];
    state.activeId = sessionStorage.getItem("creative-active");
    const brief = JSON.parse(sessionStorage.getItem("creative-campaign-brief") || "{}");
    if (brief && typeof brief === "object") state.campaignBrief = { ...state.campaignBrief, ...brief };
    state.activeClientId = sessionStorage.getItem("creative-active-client") || null;
    const workspace = JSON.parse(sessionStorage.getItem("creative-campaign-workspace") || "null");
    if (workspace && typeof workspace === "object") state.campaignWorkspace = workspace;
    const sources = JSON.parse(sessionStorage.getItem("creative-campaign-sources") || "[]");
    if (Array.isArray(sources)) state.campaignSources = sources;
    const intelligence = JSON.parse(sessionStorage.getItem("creative-campaign-intelligence") || "null");
    if (intelligence && typeof intelligence === "object") state.campaignIntelligence = intelligence;
    const candidates = JSON.parse(sessionStorage.getItem("creative-template-candidates") || "[]");
    if (Array.isArray(candidates)) state.templateCandidates = candidates;
    const matrix = JSON.parse(sessionStorage.getItem("creative-production-matrix") || "[]");
    if (Array.isArray(matrix)) {
      // Las sesiones anteriores no tenían rowNumber. Lo reconstruimos de la
      // posición de la hoja para poder relacionar el plan nuevo sin romper una
      // matriz guardada antes de esta versión.
      state.productionMatrix = matrix
        .filter((row) => row && typeof row === "object")
        .map((row: any, index) => ({
          ...row,
          rowNumber: Math.max(2, Math.trunc(Number(row?.rowNumber || row?.row_number || index + 2))),
        })) as MatrixRow[];
    }
    const matrixDraftId = (sessionStorage.getItem("creative-production-matrix-draft") || "").trim();
    state.productionMatrixDraftId = matrixDraftId || null;
    const matrixPlans = JSON.parse(sessionStorage.getItem("creative-production-matrix-plans") || "[]");
    if (Array.isArray(matrixPlans)) {
      state.productionMatrixPlans = matrixPlans
        .map(normaliseMatrixProductionPlan)
        .filter((plan): plan is MatrixProductionPlan => plan !== null);
    }
    if (!state.productionMatrix.length) state.productionMatrixPlans = [];
    const batch = JSON.parse(sessionStorage.getItem("creative-production-batch") || "null");
    if (batch && typeof batch === "object") state.productionBatch = batch;
    const task = JSON.parse(sessionStorage.getItem("creative-production-task") || "null");
    if (task && typeof task === "object" && typeof task.task_id === "string") {
      state.productionTask = task as CampaignProductionTask;
    }
  } catch {
    state.campaignIds = [];
    state.activeId = null;
  }

  queryAll<HTMLButtonElement>(".nav-item").forEach((button) => {
    button.addEventListener("click", () => navigate(button.dataset.view as ViewName));
  });
  query("#mobile-menu")?.addEventListener("click", () => document.body.classList.toggle("menu-open"));
  bindViewer();
  query("#refresh-button")?.addEventListener("click", () => window.location.reload());

  try {
    await refreshAll();
    await navigate(state.campaign.length ? "layers" : "campaign");
  } catch (error) {
    state.health = null;
    renderChrome();
    content().innerHTML = emptyState(
      "!",
      "El motor no está disponible",
      errorMessage(error),
      '<button class="button" id="boot-retry">Volver a intentar</button>',
    );
    query("#boot-retry")?.addEventListener("click", () => window.location.reload());
  }
}

// Las demás vistas se definen abajo para mantener un único bundle sin imports
// dinámicos. Así una pestaña abierta sigue funcionando durante un despliegue.

/** Los píxeles con los que **llegó** el KV, no los del producto que se le puso.
 *
 *  Al sustituir el producto, la capa se queda con el PNG del nuevo y su caja
 *  recalculada. Eso es correcto para componer, pero en «Revisar capas» se está
 *  mirando el KV: ahí salía el exprimidor que se había subido en el sitio del
 *  televisor del arte, y parecía que el KV había cambiado. El original se
 *  guarda al sustituir, así que aquí se enseña ese. */
function originalSrc(layer: Layer): string | null {
  const meta = (layer.meta || {}) as Record<string, unknown>;
  const original = meta.original_src;
  if (typeof original === "string" && original) return original;
  return layer.src || null;
}

/** ¿A esta capa ya se le puso un producto encima? */
function wasReplaced(layer: Layer): boolean {
  const meta = (layer.meta || {}) as Record<string, unknown>;
  return typeof meta.original_src === "string" && Boolean(meta.original_src);
}

/** Capas marcadas de este KV./** Capas marcadas de este KV. Se guarda por proyecto: la selección de un KV no
 *  tiene sentido en otro, y sus ids ni existen. */
function layerPicks(projectId: string): Set<string> {
  if (!state.layerPicks[projectId]) state.layerPicks[projectId] = new Set();
  return state.layerPicks[projectId];
}

/** El arte del PSD al que pertenece la capa, si el pliego lo traía.
 *
 *  Un PSD de agencia mete varias piezas en el mismo archivo y las guarda como
 *  grupos. Cuando solo se quiere editar una, lo que hay que quitar son todas
 *  las capas de las demás: por eso el grupo es la unidad de selección. */
function layerGroup(layer: Layer): string {
  const raw = String((layer.meta || {}).psd_group || "").trim();
  if (!raw) return "";
  // "09_Story/BG" y "09_Story" son el mismo arte: el subgrupo no separa piezas.
  return raw.split("/")[0];
}

function layerItem(project: Project, layer: Layer): string {
  const pixeles = originalSrc(layer);
  const preview = pixeles
    ? '<img class="layer-mini" src="' + attr(fileUrl(project.project_id, pixeles)) + '" alt="">'
    : '<span class="layer-mini"></span>';
  const marcada = layerPicks(project.project_id).has(layer.id);
  return [
    '<div class="layer-row', marcada ? " is-picked" : "", '">',
    '<label class="layer-pick" title="Marcar para borrar en bloque">',
    '<input type="checkbox" class="layer-pick-box" data-layer-id="', attr(layer.id), '"',
    checked(marcada), "></label>",
    '<button class="layer-item', state.selectedLayerId === layer.id ? " is-active" : "",
    '" data-layer-id="', attr(layer.id), '" aria-pressed="',
    state.selectedLayerId === layer.id ? "true" : "false", '">', preview, "<span><strong>", esc(layer.name),
    "</strong><small>", esc(CATEGORY_LABELS[layer.category] || layer.category), " · ",
    String(layer.width), "×", String(layer.height), layer.visible ? "" : " · oculta",
    "</small></span></button></div>",
  ].join("");
}

/** El listado, agrupado por arte cuando el PSD trae más de una. */
function layerListHtml(project: Project, layers: Layer[]): string {
  const grupos: string[] = [];
  for (const layer of layers) {
    const grupo = layerGroup(layer);
    if (!grupos.includes(grupo)) grupos.push(grupo);
  }
  const conNombre = grupos.filter(Boolean);
  if (conNombre.length < 2) return layers.map((layer) => layerItem(project, layer)).join("");

  const marcadas = layerPicks(project.project_id);
  return grupos.map((grupo) => {
    const suyas = layers.filter((layer) => layerGroup(layer) === grupo);
    const todas = suyas.length > 0 && suyas.every((layer) => marcadas.has(layer.id));
    const titulo = grupo || "Sueltas (sin arte)";
    return [
      '<div class="layer-group"><label class="layer-group-head">',
      '<input type="checkbox" class="layer-group-box" data-group="', attr(grupo), '"',
      checked(todas), "> <strong>", esc(titulo), "</strong>",
      '<small>', String(suyas.length), " capas</small></label>",
      suyas.map((layer) => layerItem(project, layer)).join(""),
      "</div>",
    ].join("");
  }).join("");
}

function optionList(values: Record<string, string>, current: string): string {
  return Object.entries(values).map(([value, label]) =>
    '<option value="' + attr(value) + '"' + selected(value === current) + ">" +
    esc(label) + "</option>"
  ).join("");
}

function layerEditor(layer: Layer): string {
  const isEditableText = layer.type === "text" || layer.category === "legal";
  return [
    '<section class="card layer-editor"><div class="card-head"><div><h2>Propiedades</h2><p>',
    esc(layer.name), "</p></div><span class=\"badge\">z ", String(layer.z_index), "</span></div>",
    '<form id="layer-form" class="editor-sections">',
    '<div class="form-grid"><label class="field"><span>Nombre</span><input name="name" value="', attr(layer.name), '"></label>',
    '<label class="field"><span>Categoría</span><select name="category">', optionList(CATEGORY_LABELS, layer.category), "</select></label></div>",
    '<div class="form-grid four"><label class="field"><span>X</span><input name="x" type="number" min="0" value="', String(layer.x), '"></label>',
    '<label class="field"><span>Y</span><input name="y" type="number" min="0" value="', String(layer.y), '"></label>',
    '<label class="field"><span>Ancho</span><input name="width" type="number" min="1" value="', String(layer.width), '"></label>',
    '<label class="field"><span>Alto</span><input name="height" type="number" min="1" value="', String(layer.height), '"></label></div>',
    '<div class="switch-grid">',
    switchHtml("visible", "Visible", layer.visible),
    switchHtml("locked", "Bloquear píxeles", layer.locked),
    switchHtml("movable", "Puede moverse", layer.movable),
    switchHtml("resizable", "Puede escalarse", layer.resizable),
    switchHtml("reorderable", "Puede reordenarse", layer.reorderable),
    switchHtml("replaceable", "Puede reemplazarse", layer.replaceable),
    switchHtml("preserve_aspect_ratio", "Mantener proporción", layer.preserve_aspect_ratio),
    "</div>",
    isEditableText ? [
      '<div class="divider"></div><label class="field"><span>Contenido editable</span><textarea name="content">',
      esc(layer.content || layer.meta?.editable_content || ""), "</textarea></label>",
      '<div class="form-grid four"><label class="field"><span>Tamaño</span><input name="font_size" type="number" min="8" max="600" value="', String(layer.font_size || 48), '"></label>',
      '<label class="field"><span>Peso</span><select name="font_weight"><option value="normal"', selected(layer.font_weight === "normal"), '>Normal</option><option value="bold"', selected(layer.font_weight === "bold"), '>Bold</option></select></label>',
      '<label class="field"><span>Alineación</span><select name="text_align"><option value="left"', selected(layer.text_align === "left"), '>Izquierda</option><option value="center"', selected(layer.text_align === "center"), '>Centro</option><option value="right"', selected(layer.text_align === "right"), '>Derecha</option></select></label>',
      '<label class="field"><span>Color</span><input name="color" type="color" value="', attr(layer.color || "#ffffff"), '"></label></div>',
      switchHtml("auto_contrast", "Contraste automático", layer.auto_contrast),
      '<div class="legal-options"', layer.category === "legal" ? "" : " hidden", ">",
      switchHtml("export_as_text", "Texto editable en SVG / Illustrator", layer.export_as_text),
      switchHtml("text_verified", "Texto legal revisado y exacto", layer.text_verified),
      "</div>",
    ].join("") : "",
    '<div class="button-row"><button class="button" type="submit">Guardar cambios</button>',
    '<button class="danger-button" type="button" id="delete-layer">Eliminar capa</button></div>',
    "</form></section>",
  ].join("");
}

function switchHtml(name: string, label: string, value: boolean): string {
  return '<label class="choice"><input type="checkbox" name="' + attr(name) + '"' +
    checked(value) + "> " + esc(label) + "</label>";
}

function inventoryHtml(project: Project): string {
  const scan = project.meta?.psd_layer_scan || {};
  const items = scan.items || project.layers.map((layer, index) => ({
    index,
    name: layer.name,
    group_path: layer.meta?.psd_group || "",
    kind: layer.meta?.psd_kind || layer.type,
    visible: layer.visible,
    bbox: [layer.x, layer.y, layer.x + layer.width, layer.y + layer.height],
    status: "imported",
    suggested_category: layer.category,
    text: layer.content,
  }));
  const labels: Record<string, string> = {
    imported: "Importada y editable",
    background_plate: "Integrada en fondo",
    hidden: "Oculta",
    outside: "Fuera de pieza",
    empty: "Vacía",
    render_error: "Error al leer",
    catalog_only_limit: "Solo catalogada",
    pending: "Catalogada",
  };
  const rows = items.map((item: any) => [
    "<tr><td>", String(Number(item.index || 0) + 1), "</td><td class=\"truncate\">",
    esc(item.group_path || "—"), "</td><td class=\"truncate\">", esc(item.name || "Sin nombre"),
    "</td><td>", esc(item.kind || "pixel"), "</td><td>", item.visible === false ? "No" : "Sí",
    "</td><td><span class=\"badge gray\">", esc(labels[item.status] || item.status || "Catalogada"),
    "</span></td><td>", esc(CATEGORY_LABELS[item.suggested_category] || item.suggested_category || "—"),
    "</td><td class=\"truncate\">", esc(item.text || ""), "</td></tr>",
  ].join("")).join("");
  return [
    '<details class="card" open><summary>Escaneo completo del KV · ', String(items.length),
    ' capas hoja</summary><p class="muted tiny">Incluye grupos, capas ocultas, vacías y fuera de la pieza. Las capas grandes no rasterizadas siguen catalogadas.</p>',
    '<div class="inventory"><table><thead><tr><th>#</th><th>Grupo PSD</th><th>Capa</th><th>Tipo</th><th>Visible</th><th>Estado</th><th>Categoría</th><th>Texto</th></tr></thead><tbody>',
    rows, "</tbody></table></div></details>",
  ].join("");
}

async function renderLayers(): Promise<void> {
  if (state.campaignWorkspace) {
    renderCampaignIntelligence();
    return;
  }
  const project = activeProject();
  if (!project) {
    content().innerHTML = emptyState("◫", "Primero abre una campaña", "Carga o abre un KV para revisar sus capas.", '<button class="button" id="go-campaign">Ir a campaña</button>');
    query("#go-campaign")?.addEventListener("click", () => navigate("campaign"));
    return;
  }
  if (!state.selectedLayerId || !project.layers.some((layer) => layer.id === state.selectedLayerId)) {
    state.selectedLayerId = project.layers.find((layer) => layer.category !== "background")?.id || null;
  }
  const layer = project.layers.find((item) => item.id === state.selectedLayerId) || null;
  await Promise.all([loadTexts(project.project_id), loadFontLibrary()]);
  const projectOptions = state.campaign.map((item) =>
    '<option value="' + attr(item.project_id) + '"' + selected(item.project_id === project.project_id) + ">" + esc(item.name) + "</option>"
  ).join("");
  const layers = [...project.layers]
    .filter((item) => item.category !== "background")
    .sort((a, b) => b.z_index - a.z_index);
  const layerList = layerListHtml(project, layers);
  const preview = layer
    ? [
        '<div class="layer-preview"><img id="mask-source" src="/api/projects/', attr(project.project_id),
        "/preview/mask/", attr(layer.id), '" alt="Máscara de ', attr(layer.name), '">',
        '<canvas id="mask-canvas" class="mask-canvas" hidden></canvas></div>',
        '<div class="button-row" style="margin-top:12px"><button class="ghost-button" id="draw-mask">Corregir bordes con pincel</button>',
        '<button class="ghost-button" id="auto-segment">Auto-segmentar</button>',
        '<button class="ghost-button" id="reset-mask">Usar rectángulo</button></div>',
        '<div id="mask-tools" class="card soft" style="margin-top:12px" hidden>',
        '<p class="muted tiny">La zona verde es el recorte actual. Añade partes faltantes o borra restos del fondo.</p>',
        '<div class="form-grid"><label class="field"><span>Pincel</span><input id="brush-size" type="range" min="2" max="60" value="16"></label>',
        '<label class="field"><span>Modo</span><select id="brush-mode"><option value="add">Añadir al producto</option><option value="subtract">Quitar del producto</option></select></label></div>',
        '<div class="button-row" style="margin-top:12px"><button class="button" id="save-mask">Guardar máscara</button><button class="ghost-button" id="cancel-mask">Cancelar</button></div></div>',
      ].join("")
    : emptyState("◫", "No hay capas", "Analiza el arte o crea una capa manual.");

  const pending = state.campaign.filter((item) => !layersConfirmed(item));
  const reviewNotice = pending.length
    ? '<div class="notice warning" style="margin-bottom:16px"><strong>Falta confirmar ' + String(pending.length) +
      " de " + String(state.campaign.length) + ' KV:</strong> ' + esc(pending.map((item) => item.name).join(", ")) +
      ". Marca en cada capa si se usa y para qué, y guarda con “Guardar y confirmar”." + "</div>"
    : '<div class="notice success" style="margin-bottom:16px">Las capas de los ' + String(state.campaign.length) +
      " KV están confirmadas.</div>";

  content().innerHTML = [
    stepBar("layers"),
    pageHead(
      "Paso 2 de 4",
      "Limpia las capas: qué se usa y qué no",
      "Define qué se elimina y qué debe conservarse exactamente. Esto evita que una prenda, un sello o un copy mal nombrado se interpreten mal.",
    ),
    reviewNotice,
    // Qué KV se está revisando, siempre visible y con su estado.
    kvSwitcher(project),
    // La confirmación de roles va primero: es lo único obligatorio de este paso.
    reviewRoles(project, layers),
    '<div class="spacer"></div>',
    // El copy va justo después: es lo que se cambia en casi todas las campañas.
    copyEditor(project),
    '<div class="spacer"></div>',
    '<details class="card" open><summary>Ajustes finos de la capa seleccionada · opcional</summary>',
    '<p class="muted tiny" style="margin-top:10px">Máscaras, geometría y permisos. Solo si algo salió mal en la importación.</p>',
    '<div class="button-row" style="margin:14px 0 16px"><label class="field" style="min-width:260px"><span>KV activo</span><select id="active-kv">', projectOptions, "</select></label>",
    '<button class="ghost-button" id="analyze-project">Detectar elementos</button>',
    '<button class="ghost-button" id="extract-layers">Extraer PNG</button>',
    '<button class="ghost-button" id="rebuild-background">Reconstruir fondo</button>',
    '<button class="ghost-button" id="show-detections">Ver detecciones</button></div>',
    '<div class="layer-workbench">',
    '<section class="card flush"><div class="card-head" style="padding:16px 16px 0"><div><h2>Capas</h2><p>', String(layers.length), " elementos</p></div>",
    '<button class="icon-button" id="new-layer" title="Crear capa">+</button></div>',
    layerPickBar(project, layers),
    '<div class="layer-list">', layerList || '<div class="notice">Sin capas.</div>', "</div></section>",
    '<section class="card"><div class="card-head"><div><h2>Vista de máscara</h2><p>Verde = píxeles incluidos</p></div></div>', preview, "</section>",
    layer ? layerEditor(layer) : '<section class="card layer-editor">' + emptyState("◇", "Selecciona una capa", "Elige una capa del panel izquierdo.") + "</section>",
    "</div>",
    '<div style="margin-top:18px">', orderEditor(layers), "</div>",
    '<div style="margin-top:18px">', inventoryHtml(project), "</div>",
    "</details>",
    stepFooter("layers", "Cargar productos"),
  ].join("");

  bindStepBar();
  bindStepFooter("layers");
  bindCopyEditor(project);
  bindLayerActions(project, layer, layers);
}

function briefList(items: string[], empty = "No detectado"): string {
  return items.length
    ? '<div class="brief-tags">' + items.map((item) => '<span>' + esc(item) + '</span>').join("") + '</div>'
    : '<span class="muted tiny">' + esc(empty) + '</span>';
}

function renderCampaignIntelligence(): void {
  const brief = state.campaignIntelligence;
  if (!brief) {
    content().innerHTML = [
      stepBar("layers"),
      pageHead("02 · INTELIGENCIA", "Todavía falta construir el brief", "El material ya está guardado; ejecuta el análisis para clasificarlo y proponer las plantillas."),
      emptyState("✦", "Analiza la campaña", "La IA separará estrategia, identidad visual, recursos y restricciones.", '<button class="button" id="generate-missing-brief">Generar brief con IA</button>'),
    ].join("");
    bindStepBar();
    query("#generate-missing-brief")?.addEventListener("click", () => void analyzeCurrentCampaign());
    return;
  }
  const reviewed = Boolean(state.campaignWorkspace?.brief_reviewed_at);
  const sourceRows = state.campaignSources.map((source) => [
    '<div class="knowledge-row"><span class="source-format">', esc(sourceKindLabel(source)), '</span><div><strong>',
    esc(source.filename), '</strong><small>', esc(source.role || source.summary || "Contexto visual y textual"), '</small></div>',
    '<i>', source.warnings.length ? "Revisar" : "✓", '</i></div>',
    (source.previews || []).length
      ? '<div class="evidence-strip">' + (source.previews || []).slice(0, 5).map((preview) =>
          '<a href="' + attr(preview) + '" target="_blank" rel="noreferrer"><img src="' +
          attr(preview) + '" alt="Evidencia de ' + attr(source.filename) + '" loading="lazy"></a>'
        ).join("") + '</div>'
      : '',
  ].join("")).join("");
  const evidence = state.campaignWorkspace?.social_evidence || [];
  const socialEvidence = evidence.map((item) => [
    '<article class="social-evidence"><strong>', esc(item.title || item.url), '</strong>',
    item.accessible === false
      ? '<p class="muted"><strong>No se usó como post:</strong> la red bloqueó la lectura pública. '
        + 'Pega enlaces públicos de posts concretos o sube capturas en “Material de campaña” si esta referencia es importante.</p>'
      : item.description ? '<p>' + esc(item.description) + '</p>' : '',
    item.accessible !== false && item.posts?.length
      ? '<p class="muted tiny"><strong>' + String(item.posts.length) + '</strong> imagen' + (item.posts.length === 1 ? '' : 'es') + ' de publicaciones públicas; se excluyeron avatar y logos de red.</p><div class="evidence-strip">' + item.posts.slice(0, 5).map((post) =>
        '<a href="' + attr(post) + '" target="_blank" rel="noreferrer"><img src="' + attr(post) +
        '" alt="Post público de referencia" loading="lazy" referrerpolicy="no-referrer"></a>'
      ).join("") + '</div>'
      : item.accessible !== false
        ? '<p class="muted">Se leyó la URL, pero no expuso imágenes públicas de posts. Prueba con enlaces directos a publicaciones o añade capturas.</p>'
        : '',
    '</article>',
  ].join("")).join("");
  content().innerHTML = [
    stepBar("layers"),
    pageHead("02 · INTELIGENCIA DE CAMPAÑA", "Este es el brief que entendió la IA", "Corrige únicamente lo necesario. Estas reglas guían las 3–5 plantillas y todas sus adaptaciones."),
    '<div class="brief-review-layout"><section class="card elevated brief-main"><div class="card-head"><div><span class="kicker">NÚCLEO CREATIVO</span><h2>', esc(brief.concept || state.campaignBrief.name || "Concepto de campaña"), '</h2><p>', esc(brief.summary || "Brief construido desde el material disponible."), '</p></div><span class="badge green">IA · ', esc(brief.engine || "automático"), '</span></div>',
    reviewed
      ? '<div class="notice success compact"><strong>Brief revisado:</strong> las plantillas visibles corresponden a esta versión aprobada.</div>'
      : '<div class="notice warning compact"><strong>Falta tu revisión:</strong> guarda el brief para regenerar las plantillas con estas reglas antes de aprobarlas.</div>',
    '<div class="form-grid"><label class="field"><span>Objetivo</span><textarea id="brief-objective" rows="3">', esc(brief.objective), '</textarea></label>',
    '<label class="field"><span>Público</span><textarea id="brief-audience" rows="3">', esc(brief.audience), '</textarea></label></div>',
    '<label class="field" style="margin-top:14px"><span>Mensaje principal</span><textarea id="brief-message" rows="3">', esc(brief.message), '</textarea></label>',
    '<label class="field" style="margin-top:14px"><span>Concepto creativo</span><textarea id="brief-concept" rows="2">', esc(brief.concept), '</textarea></label>',
    '<div class="form-grid" style="margin-top:14px"><label class="field"><span>Tono · uno por línea</span><textarea id="brief-tone" rows="5">', esc(brief.tone.join("\n")), '</textarea></label>',
    '<label class="field"><span>Tratamiento del producto</span><textarea id="brief-product-treatment" rows="5">', esc(brief.product_treatment.join("\n")), '</textarea></label></div>',
    '<div class="form-grid" style="margin-top:14px"><label class="field"><span>Estilo de titulares</span><textarea id="brief-headline-style" rows="2">', esc(brief.headline_style), '</textarea></label>',
    '<label class="field"><span>Regla de CTA</span><textarea id="brief-cta-style" rows="2">', esc(brief.cta_style), '</textarea></label></div>',
    '<div class="brief-grid"><div><span class="label">Paleta detectada</span>', briefList(brief.palette), '</div><div><span class="label">Tipografías detectadas</span>', briefList(brief.typography), '</div></div>',
    '<div class="button-row" style="margin-top:20px"><button class="button" id="save-intelligence">', reviewed ? 'Guardar y regenerar plantillas' : 'Aprobar brief y generar plantillas', '</button><button class="ghost-button" id="rerun-intelligence">Reanalizar todo</button></div></section>',
    '<aside class="brief-side"><section class="card"><div class="card-head"><div><h2>Reglas visuales editables</h2><p>Una por línea.</p></div></div><label class="field"><textarea id="brief-visual-rules" rows="9">', esc(brief.visual_rules.join("\n")), '</textarea></label></section>',
    '<section class="card"><div class="card-head"><div><h2>Contenido dinámico</h2><p>Una regla por línea; vacío significa que no se fuerza.</p></div></div>',
    '<label class="field"><span>Obligatorio</span><textarea id="brief-required" rows="4">', esc(brief.required_elements.join("\n")), '</textarea></label>',
    '<label class="field brief-subhead"><span>Opcional</span><textarea id="brief-optional" rows="4">', esc(brief.optional_elements.join("\n")), '</textarea></label>',
    '<label class="field brief-subhead"><span>Legales, fechas y restricciones</span><textarea id="brief-legal" rows="5">', esc(brief.legal.join("\n")), '</textarea></label>',
    '<label class="field brief-subhead"><span>No usar</span><textarea id="brief-forbidden" rows="4">', esc(brief.forbidden_elements.join("\n")), '</textarea></label></section></aside></div>',
    '<div class="spacer"></div><section class="card"><div class="card-head"><div><h2>Qué extrajo de cada archivo</h2><p>Las páginas se analizan dentro de su documento; no aparecen como KV separados.</p></div><span class="badge">', String(state.campaignSources.length), ' FUENTES</span></div><div class="knowledge-list">', sourceRows, '</div></section>',
    socialEvidence ? '<div class="spacer"></div><section class="card"><div class="card-head"><div><h2>Evidencia pública encontrada</h2><p>Esto es lo que la IA pudo leer realmente; una URL sin posts no se presenta como analizada.</p></div></div>' + socialEvidence + '</section>' : '',
    brief.warnings.length ? '<div class="notice warning" style="margin-top:16px"><strong>Revisión recomendada:</strong> ' + esc(brief.warnings.join(" · ")) + '</div>' : '',
    reviewed ? stepFooter("layers", "Revisar 3–5 plantillas") : '',
  ].join("");
  bindStepBar();
  if (reviewed) bindStepFooter("layers");
  query("#rerun-intelligence")?.addEventListener("click", () => void analyzeCurrentCampaign());
  query("#save-intelligence")?.addEventListener("click", async () => {
    if (!state.campaignIntelligence || !state.activeClientId || !state.campaignWorkspace) return;
    const objective = query<HTMLTextAreaElement>("#brief-objective")?.value.trim() || "";
    const audience = query<HTMLTextAreaElement>("#brief-audience")?.value.trim() || "";
    const message = query<HTMLTextAreaElement>("#brief-message")?.value.trim() || "";
    const values = (selector: string) => (query<HTMLTextAreaElement>(selector)?.value || "")
      .split(/\n|,/).map((item) => item.trim()).filter(Boolean);
    const concept = query<HTMLTextAreaElement>("#brief-concept")?.value.trim() || "";
    const headlineStyle = query<HTMLTextAreaElement>("#brief-headline-style")?.value.trim() || "";
    const ctaStyle = query<HTMLTextAreaElement>("#brief-cta-style")?.value.trim() || "";
    busy("Aprobando el brief", "Guardando reglas y regenerando las plantillas desde esta versión…", 35);
    try {
      state.campaignIntelligence = await reviseCampaignBrief(
        state.activeClientId,
        state.campaignWorkspace.campaign_id,
        {
          objective,
          audience,
          primary_message: message,
          creative_concept: concept,
          tone: values("#brief-tone"),
          headline_style: headlineStyle,
          cta_style: ctaStyle,
          visual_rules: values("#brief-visual-rules"),
          product_treatment: values("#brief-product-treatment"),
          legal_requirements: values("#brief-legal"),
          required_elements: values("#brief-required"),
          optional_elements: values("#brief-optional"),
          forbidden_elements: values("#brief-forbidden"),
        },
      );
      busyProgress(62, "Redibujando propuestas sin productos…");
      const regenerated = await generateCampaignBrief(
        state.activeClientId,
        state.campaignWorkspace.campaign_id,
        true,
      );
      if (!regenerated) throw new Error("El servidor no pudo regenerar las plantillas aprobables.");
      const recovered = await loadCampaignState(
        state.activeClientId,
        state.campaignWorkspace.campaign_id,
      );
      state.campaignWorkspace = recovered.workspace;
      state.campaignSources = recovered.sources;
      state.campaignIntelligence = recovered.brief || regenerated.brief;
      state.templateCandidates = recovered.candidates.length
        ? recovered.candidates
        : regenerated.template_candidates;
      state.productionMatrixPlans = [];
      state.campaignBrief.objective = state.campaignIntelligence.objective;
      saveSession();
      toast("Brief aprobado y plantillas regeneradas desde tus correcciones.", "success");
      renderCampaignIntelligence();
    } catch (error) {
      toast(errorMessage(error), "error");
    } finally {
      idle();
    }
  });
}

/** Barra de borrado en bloque. Aparece al marcar la primera capa.
 *
 *  Un pliego con cuatro artes son treinta capas, y quitar las de los otros tres
 *  de una en una —con su confirmación cada vez— era el paso más lento de todo
 *  el flujo. El backend ya aceptaba una lista; lo que faltaba era poder armarla. */
function layerPickBar(project: Project, layers: Layer[]): string {
  const marcadas = layerPicks(project.project_id);
  const total = layers.filter((layer) => marcadas.has(layer.id)).length;
  if (!total) {
    return [
      '<div class="layer-pickbar is-idle">',
      '<span class="muted tiny">Marca capas con su casilla para borrarlas en bloque. ',
      "Con <strong>Mayús</strong> se marca todo el tramo.</span>",
      '<button class="ghost-button small" id="pick-all-layers">Marcar todas</button>',
      "</div>",
    ].join("");
  }
  return [
    '<div class="layer-pickbar">',
    "<span><strong>", String(total), "</strong> de ", String(layers.length), " marcadas</span>",
    '<span class="pickbar-actions">',
    '<button class="ghost-button small" id="clear-layer-picks">Ninguna</button>',
    '<button class="danger-button small" id="delete-layer-picks">Eliminar ', String(total), "</button>",
    "</span></div>",
  ].join("");
}

function orderEditor(layers: Layer[]): string {
  const rows = layers.map((layer, index) => [
    '<div class="order-row"><span><strong>', esc(layer.name), '</strong> <small class="muted">z ', String(layer.z_index), "</small></span>",
    '<button class="icon-button move-layer" data-index="', String(index), '" data-direction="-1"', index === 0 ? " disabled" : "", ">↑</button>",
    '<button class="icon-button move-layer" data-index="', String(index), '" data-direction="1"', index === layers.length - 1 ? " disabled" : "", ">↓</button></div>",
  ].join("")).join("");
  return '<section class="card"><div class="card-head"><div><h2>Orden visual</h2><p>Arriba = al frente</p></div></div><div class="order-list">' + rows + "</div></section>";
}

/* Selector de KV siempre visible.
   Antes vivía dentro del acordeón "Ajustes finos · opcional", así que al
   confirmar un KV no había forma evidente de pasar al siguiente y el paso
   parecía roto: el botón guardaba pero el pie seguía pidiendo 7 KV más. */
function kvSwitcher(active: Project): string {
  const chips = state.campaign.map((project, index) => {
    const done = layersConfirmed(project);
    const isCurrent = project.project_id === active.project_id;
    // El aspa va fuera del botón y no dentro: un <button> no puede contener
    // otro, y anidarlos hace que el clic de quitar abra el KV además de
    // borrarlo. Por eso la ficha es un envoltorio con dos botones hermanos.
    const marcado = state.kvPicks.has(project.project_id);
    return [
      '<div class="kv-chip-wrap', marcado ? " is-picked" : "", '">',
      '<label class="kv-pick" title="Marcar para quitar en bloque">',
      '<input type="checkbox" class="kv-pick-box" data-kv-pick="', attr(project.project_id), '"',
      checked(marcado), "></label>",
      '<button class="kv-chip', isCurrent ? " is-current" : "", done ? " is-done" : "",
      '" data-kv="', attr(project.project_id), '" title="', attr(project.name), '">',
      '<img src="', attr(thumbnailUrl(project.project_id, 120)), '" alt="" loading="lazy" decoding="async">',
      '<span class="kv-chip-copy"><strong>', esc(project.name), "</strong><small>",
      String(project.canvas.width), "×", String(project.canvas.height), " · ",
      done ? "confirmado" : "sin confirmar", "</small></span>",
      '<i class="kv-chip-mark" aria-hidden="true">', done ? "✓" : String(index + 1), "</i>",
      "</button>",
      '<button class="kv-drop" data-drop="', attr(project.project_id),
      '" title="Quitar «', attr(project.name), '» de la campaña"',
      ' aria-label="Quitar ', attr(project.name), ' de la campaña">×</button>',
      "</div>",
    ].join("");
  }).join("");
  const done = state.campaign.filter(layersConfirmed).length;
  const marcados = state.campaign.filter((item) => state.kvPicks.has(item.project_id)).length;
  return [
    '<section class="card" style="margin-bottom:18px"><div class="card-head"><div><h2>KV que estás revisando</h2>',
    '<p>Cada uno se confirma por separado. Al guardar se salta al siguiente que falte. ',
    "Marca los que no vayas a usar y quítalos de una vez; con <strong>Mayús</strong> se marca todo el tramo.</p></div>",
    '<span class="badge', done === state.campaign.length ? " green" : "", '">', String(done), " DE ",
    String(state.campaign.length), "</span></div>",
    marcados
      ? [
          '<div class="kv-pickbar">',
          "<span><strong>", String(marcados), "</strong> de ", String(state.campaign.length),
          " marcados</span>",
          '<span class="pickbar-actions">',
          '<button class="ghost-button small" id="clear-kv-picks">Ninguno</button>',
          '<button class="danger-button small" id="drop-kv-picks">Quitar ', String(marcados),
          "</button></span></div>",
        ].join("")
      : [
          '<div class="kv-pickbar is-idle">',
          '<button class="ghost-button small" id="pick-all-kv">Marcar todos</button>',
          "</div>",
        ].join(""),
    '<div class="kv-switch">', chips, "</div></section>",
  ].join("");
}

/** Miniatura de la capa. Sin verla no se puede decidir su función. */
function roleThumb(project: Project, layer: Layer): string {
  const pixeles = originalSrc(layer);
  if (pixeles) {
    return '<img class="role-thumb" src="' + attr(fileUrl(project.project_id, pixeles)) +
      '" alt="' + attr(layer.name) + '" loading="lazy" decoding="async">';
  }
  // Las capas de texto del PSD no siempre traen PNG: se muestra su contenido.
  const text = (layer.content || layer.meta?.editable_content || "").trim();
  return '<span class="role-thumb is-text" aria-hidden="true">' +
    (text ? esc(text.slice(0, 28)) : "◇") + "</span>";
}

function reviewRoles(project: Project, layers: Layer[]): string {
  const rows = [...layers].sort((a, b) => b.z_index - a.z_index).map((layer) => {
    const current = layer.visible ? layer.category : "ignore";
    return [
      '<div class="role-row">', roleThumb(project, layer),
      '<div class="role-copy"><strong>', esc(layer.name), "</strong><small>",
      esc(CATEGORY_LABELS[layer.category] || layer.category), " · ",
      String(layer.width), "×", String(layer.height), layer.visible ? "" : " · oculta en el PSD",
      wasReplaced(layer) ? " · ya sustituido en una tanda" : "",
      "</small></div>",
      '<select class="role-select" data-id="', attr(layer.id), '" aria-label="Función de ',
      attr(layer.name), '">', optionList(ROLE_LABELS, current), "</select></div>",
    ].join("");
  }).join("");
  const others = state.campaign.length - 1;
  return [
    '<section class="card elevated"><div class="card-head"><div><h2>Función de cada capa</h2>',
    '<p>Mira la miniatura y marca qué se elimina, qué se conserva y qué no se usa</p></div>',
    '<span class="badge">', String(layers.length), ' CAPAS</span></div>',
    '<div class="role-list">', rows, '</div>',
    // Aviso en vivo: confirmar un KV sin capa Producto era posible, y el
    // problema no aparecía hasta el último clic de Generar, tres pasos después.
    '<div id="role-status" style="margin-top:16px"></div>',
    '<div class="button-row" style="margin-top:14px">',
    '<button class="button large" id="confirm-roles" style="flex:1;min-width:260px">',
    'Guardar y confirmar este KV</button>',
    others > 0
      ? '<button class="ghost-button large" id="apply-to-all" title="Copia logo, legales, CTA y decoración a los demás KV. El producto se marca en cada uno, porque en cada pieza tiene un nombre distinto.">' +
        "Copiar logo y textos a los otros " + String(others) + " KV</button>"
      : "",
    "</div>",
    others > 0
      ? '<p class="muted tiny" style="margin-top:10px">“Copiar” lleva logo, legales, CTA y decoración al resto. El producto se marca KV por KV: en cada pieza del PSD tiene otro nombre, y copiarlo marcaría la capa equivocada.</p>'
      : "",
    "</section>",
  ].join("");
}

/* ------------------------------------------------------ copy del arte
   Un KV llega con el precio, el nombre del producto y el logo como píxeles.
   Esto los vuelve editables: reescribir el texto respeta color, cuerpo y sitio
   del original, y "quitar del arte" borra el elemento también del fondo cuando
   venía aplanado, que es el caso en el que ocultarlo no quitaba nada. */

const EMPTY_TEXTS: ArtTexts = { layers: [], brand_font: false, brand_font_bold: false };

async function loadTexts(projectId: string, force = false): Promise<ArtTexts> {
  if (!force && state.texts[projectId]) return state.texts[projectId];
  try {
    const response = await get<ArtTexts>("/projects/" + projectId + "/texts");
    state.texts[projectId] = { ...EMPTY_TEXTS, ...response, layers: response.layers || [] };
  } catch (error) {
    toast(errorMessage(error), "error");
    state.texts[projectId] = { ...EMPTY_TEXTS };
  }
  return state.texts[projectId];
}

/* La tipografía decide si el copy reescrito sirve para producción. El color, el
   cuerpo y el sitio salen del arte, pero las letras son las de la fuente que
   haya: sin la de marca, un precio de Marcimex sale en DejaVu. Antes solo se
   podía subir al crear el proyecto —tres pasos antes de que hiciera falta—, así
   que el aviso y el formulario viven aquí, donde se nota el problema. */
function brandFontHtml(texts: ArtTexts): string {
  if (texts.brand_font) {
    return [
      '<div class="notice success" style="margin-bottom:14px"><strong>Tipografía de marca cargada</strong>',
      texts.brand_font_bold ? " (redonda y negrita)." : " (solo la redonda; la negrita usará esta misma cara).",
      ' <button class="ghost-button small" id="open-brand-font" style="margin-left:8px">Cambiar</button></div>',
      '<div id="brand-font-form" hidden>', brandFontFormHtml(), "</div>",
    ].join("");
  }
  return [
    '<div class="notice warning" style="margin-bottom:14px"><strong>Falta la tipografía de marca.</strong> ',
    "El copy que reescribas saldrá con el color, el cuerpo y la posición del original, ",
    "pero con las letras de la tipografía del sistema. Sube el .ttf/.otf de la marca ",
    "para que el arte quede listo para producción.",
    '<div style="margin-top:10px">', brandFontFormHtml(), "</div></div>",
  ].join("");
}

interface ClientFontCatalog {
  id: string;
  name: string;
  fonts: { id: string; name: string }[];
}
let fontLibrary: ClientFontCatalog[] = [];

async function loadFontLibrary(): Promise<void> {
  try {
    fontLibrary = await get<ClientFontCatalog[]>("/projects/font-library/catalog");
  } catch {
    fontLibrary = [];
    toast("No se pudo cargar el catálogo de tipografías. Puedes volver a abrir Revisar capas.", "error");
  }
}

function savedFontOptions(clientId: string, chosen = ""): string {
  const fonts = fontLibrary.find((client) => client.id === clientId)?.fonts || [];
  return '<option value="">Seleccionar tipografía</option>' + fonts.map((font) =>
    '<option value="' + attr(font.id) + '"' + selected(font.id === chosen) + '>' + esc(font.name) + '</option>'
  ).join("");
}

function brandFontFormHtml(): string {
  const others = state.campaign.length - 1;
  const saved = activeProject()?.meta?.client_fonts as { client_id?: string; font?: string; font_bold?: string } | undefined;
  const clientId = saved?.client_id || "";
  return [
    '<label class="field"><span>Tipografías guardadas por cliente</span><select id="font-client">',
    '<option value="">Subir archivos propios</option>',
    ...fontLibrary.map((client) => '<option value="' + attr(client.id) + '"' + selected(client.id === clientId) + '>' + esc(client.name) + ' · ' + String(client.fonts.length) + ' fuentes</option>'),
    '</select></label>',
    '<div id="saved-font-fields"' + (clientId ? '' : ' hidden') + '><p class="muted">Estas fuentes quedan guardadas para futuras campañas, aunque borres el KV.</p><div class="form-grid">',
    '<label class="field"><span>Cara redonda</span><select id="saved-font">', savedFontOptions(clientId, saved?.font), '</select></label>',
    '<label class="field"><span>Cara negrita · opcional</span><select id="saved-font-bold">', savedFontOptions(clientId, saved?.font_bold), '</select></label></div></div>',
    '<div id="uploaded-font-fields"' + (clientId ? ' hidden' : '') + '>',
    '<div class="form-grid"><label class="field"><span>Cara redonda</span>',
    '<input id="brand-font" type="file" accept=".ttf,.otf"></label>',
    '<label class="field"><span>Cara negrita · opcional</span>',
    '<input id="brand-font-bold" type="file" accept=".ttf,.otf"></label></div>',
    "</div>",
    others > 0
      ? '<label class="choice" style="margin-top:10px"><input id="brand-font-all" type="checkbox" checked> ' +
        "Aplicar a los " + String(state.campaign.length) + " KV de la campaña</label>"
      : "",
    '<button class="button small" id="save-brand-font" style="margin-top:10px">Guardar tipografía</button>',
  ].join("");
}

async function saveBrandFont(project: Project): Promise<void> {
  const regular = query<HTMLInputElement>("#brand-font")?.files?.[0] || null;
  const bold = query<HTMLInputElement>("#brand-font-bold")?.files?.[0] || null;
  const clientId = query<HTMLSelectElement>("#font-client")?.value || "";
  const savedFont = query<HTMLSelectElement>("#saved-font")?.value || "";
  const savedBold = query<HTMLSelectElement>("#saved-font-bold")?.value || "";
  if (clientId ? !savedFont : (!regular && !bold)) {
    toast(clientId ? "Elige la cara redonda del cliente." : "Elige al menos un archivo de tipografía.", "error");
    return;
  }
  const toAll = query<HTMLInputElement>("#brand-font-all")?.checked ?? false;
  const targets = toAll ? state.campaign : [project];
  busy("Guardando tipografía", "Aplicando la cara de marca al copy…", 20);
  try {
    for (let index = 0; index < targets.length; index += 1) {
      const target = targets[index];
      busyProgress(
        20 + Math.round((index / Math.max(1, targets.length)) * 70),
        target.name,
      );
      const data = new FormData();
      if (clientId) {
        data.append("client_id", clientId);
        data.append("saved_font", savedFont);
        if (savedBold) data.append("saved_font_bold", savedBold);
      } else {
        if (regular) data.append("font", regular);
        if (bold) data.append("font_bold", bold);
      }
      const result = await post<any>(
        "/projects/" + target.project_id + "/references/font",
        data,
      );
      (result.warnings || []).forEach((warning: string) => toast(warning, "info"));
      await refreshProject(target.project_id);
      await loadTexts(target.project_id, true);
    }
    toast(
      "Tipografía aplicada a " + String(targets.length) + " KV. El copy ya reescrito se recalculó.",
      "success",
    );
    await renderLayers();
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    idle();
  }
}

/** Misma capa en otro KV de la campaña.
 *
 *  Los KV de una campaña suelen ser piezas del mismo PSD —el cuadrado y el
 *  vertical del mismo aviso—, así que el precio es el enésimo elemento de su
 *  categoría en ambos. No hay identificador compartido: los ids son por pieza. */
function twinLayerId(source: Project, target: Project, layerId: string): string | null {
  if (source.project_id === target.project_id) return layerId;
  const layer = source.layers.find((item) => item.id === layerId);
  if (!layer) return null;
  const byCategory = (project: Project) =>
    project.layers
      .filter((item) => item.category === layer.category)
      .sort((a, b) => a.z_index - b.z_index);
  const index = byCategory(source).findIndex((item) => item.id === layerId);
  return byCategory(target)[index]?.id || null;
}

function copyThumb(project: Project, item: ArtTextLayer): string {
  if (!item.src) {
    return '<span class="copy-thumb is-text">' + esc(item.text.slice(0, 28) || "◇") + "</span>";
  }
  const url = attr(fileUrl(project.project_id, item.src));
  return [
    '<button type="button" class="copy-thumb-btn zoom-copy" data-layer="', attr(item.id),
    '" title="Ver el elemento en grande">',
    '<img class="copy-thumb" src="', url, '" alt="', attr(item.name),
    '" loading="lazy" decoding="async"><span class="copy-zoom-hint">⤢</span></button>',
  ].join("");
}

function copyRow(project: Project, item: ArtTextLayer): string {
  const lines = Math.max(1, Math.min(4, item.style?.lines || item.text.split("\n").length));
  // Lo escrito y sin guardar manda sobre lo que hay en el arte: si no, cualquier
  // repintado de la lista lo borraba y había que volver a escribirlo.
  const draft = state.copyDrafts[item.id];
  const pending = draft !== undefined && draft !== item.text;
  const shown = draft !== undefined ? draft : item.text;
  const editor = item.editable && item.pieces <= 1
    ? [
        '<textarea class="copy-text" rows="', String(lines), '" data-layer="', attr(item.id),
        '" spellcheck="false" placeholder="',
        item.text
          ? ""
          : "Lee la miniatura de la izquierda y escribe aquí el texto que la reemplaza",
        '" maxlength="400" aria-label="Texto del arte">', esc(shown), "</textarea>",
        '<div class="copy-controls"><label>Color <input type="color" class="copy-color" value="',
        attr((item.rewritten ? project.layers.find((layer) => layer.id === item.id)?.color : undefined) || item.style?.color || "#ffffff"),
        '"></label><label>Alineación <select class="copy-align">',
        optionList({left: "Izquierda", center: "Centro", right: "Derecha"},
          (item.rewritten ? project.layers.find((layer) => layer.id === item.id)?.text_align : undefined) || item.style?.align || "center"),
        '</select></label><small class="muted">El tamaño se ajusta al espacio disponible.</small></div>',
        pending
          ? '<small class="copy-pending">Sin guardar todavía: pulsa «Guardar texto».</small>'
          : "",
      ].join("")
    : item.pieces > 1
      ? '<p class="muted tiny copy-locked">Separa las partes para editar cada texto conservando su decoración.</p>'
      : '<p class="muted tiny copy-locked">Sus píxeles no contienen texto que se pueda medir ' +
      "(es una forma, un sello o una foto). Puedes sustituirlo por una imagen o quitarlo del arte.</p>";
  const notes = [
    CATEGORY_LABELS[item.category] || item.category,
    item.rewritten ? "reescrito" : "",
    item.in_plate ? "aplanado en el fondo" : "",
    item.part_of ? "parte separada" : "",
  ].filter(Boolean).join(" · ");
  // Qué se comprobó al separar la capa. Una separación que se dejó una pieza
  // fuera solo se descubría al generar la tanda, con el precio viejo asomando
  // por debajo del nuevo.
  const check = item.part_of && item.split_check
    ? '<p class="copy-check ' + (item.split_ok ? "is-ok" : "is-bad") + '">' +
      (item.split_ok ? "✓ " : "⚠ ") + esc(item.split_check) + "</p>"
    : "";
  // El PSD trae el rótulo, el precio, el precio anterior y el sello en la misma
  // capa. Reescribir eso de una vez no sirve para cambiar solo el precio, así
  // que se ofrece separarlo en las piezas que el arte ya tiene dibujadas.
  const split = item.pieces > 1
    ? '<div class="copy-split">Esta capa trae <strong>' + String(item.pieces) +
      " partes</strong> (rótulo, precio, centavos en volado, sello…). " +
      "Sepáralas para cambiar solo una: reescribirla entera la reemplaza por un " +
      "solo renglón y se pierde el diseño." +
      '<button class="button small split-copy" data-layer="' + attr(item.id) +
      '">Separar en ' + String(item.pieces) + " partes</button></div>"
    : "";
  return [
    '<div class="copy-row', item.removed ? " is-removed" : "", '">', copyThumb(project, item),
    '<div class="copy-meta"><strong>', esc(item.name), "</strong><small>", esc(notes), "</small>", check, "</div>",
    '<div class="copy-edit">', editor, "</div>",
    item.src
      ? '<div class="copy-zoom" data-layer="' + attr(item.id) + '" hidden><img src="' +
        attr(fileUrl(project.project_id, item.src)) + '" alt="' + attr(item.name) + '"></div>'
      : "",
    '<div class="copy-actions">',
    '<label class="ghost-button small">Sustituir logo / imagen',
    '<input class="replace-copy-image" type="file" accept="image/png,image/webp,image/jpeg" data-layer="',
    attr(item.id), '" aria-label="Sustituir logo o imagen" style="display:none"></label>',

    item.editable && item.pieces <= 1
      ? '<button class="button small save-copy" data-layer="' + attr(item.id) + '">Guardar texto</button>'
      : "",
    item.rewritten
      ? '<button class="ghost-button small restore-copy" data-layer="' + attr(item.id) + '">Volver al original</button>'
      : "",
    item.part_of
      ? '<button class="ghost-button small unsplit-copy" data-layer="' + attr(item.id) +
        '" title="Junta las partes en una sola capa, conservando lo que hayas cambiado en ellas">Volver a unir</button>'
      : "",
    '<label class="check"><input class="remove-copy" type="checkbox" data-layer="', attr(item.id), '"',
    checked(item.removed), "> Quitar del arte</label></div>",
    split,
    "</div>",
  ].join("");
}

/** El arte como va quedando, al lado de los textos.
 *
 *  Hasta ahora había que llegar al paso 4 y generar para ver el efecto de un
 *  copy: minutos por cada cambio de una palabra. El motor ya sabía componerlo
 *  —`/preview/template` existía desde el principio— pero nadie lo llamaba. */
function copyPreviewHtml(project: Project): string {
  return [
    '<aside class="copy-preview"><div class="copy-preview-head">',
    "<strong>Cómo va quedando</strong>",
    '<button class="ghost-button small" id="refresh-copy-preview" title="Volver a componerlo">Actualizar</button>',
    "</div>",
    '<div class="copy-preview-art"><img id="copy-preview-img" src="',
    attr(templatePreviewUrl(project.project_id)),
    '" alt="Arte de ', attr(project.name), '" loading="lazy" decoding="async"></div>',
    '<small class="muted tiny">El arte con tus textos, sin el producto: ese entra en el paso 3. ',
    "Se actualiza al guardar un texto o sustituir una imagen.</small></aside>",
  ].join("");
}

/** La URL del preview con marca de tiempo: sin ella el navegador reusa la
 *  imagen anterior y el texto recién guardado no aparece. */
function templatePreviewUrl(projectId: string): string {
  return "/api/projects/" + projectId + "/preview/template?v=" + String(Date.now());
}

/** Recompone el arte del panel sin repintar toda la pantalla. */
function refreshCopyPreview(projectId: string): void {
  const img = query<HTMLImageElement>("#copy-preview-img");
  if (img) img.src = templatePreviewUrl(projectId);
}

function copyEditor(project: Project): string {
  const texts = state.texts[project.project_id] || EMPTY_TEXTS;
  const items = texts.layers;
  if (!items.length) {
    return [
      '<section class="card" id="copy-editor"><div class="card-head"><div><h2>Textos y logos del arte</h2>',
      "<p>Cambia el texto, sustituye un logo por tu archivo o quita un elemento</p></div></div>",
      '<div class="notice">Este KV no tiene copy ni logos separados: todo llegó dentro del fondo. ',
      "Crea la capa que quieras editar en <em>Ajustes finos</em> y volverá a aparecer aquí.</div></section>",
    ].join("");
  }
  const editable = items.filter((item) => item.editable).length;
  return [
    '<section class="card elevated" id="copy-editor"><div class="card-head"><div><h2>Textos y logos del arte</h2>',
    "<p>Cambia el texto, sustituye un logo por tu archivo o quita un elemento</p></div>",
    '<span class="badge', editable ? " green" : "", '">', String(editable), " EDITABLES</span></div>",
    '<p class="muted tiny" style="margin-bottom:14px">El texto nuevo se escribe con el color, el cuerpo y la ',
    "posición del original. Si necesitas un texto distinto por producto, no lo cambies aquí: ",
    "hazlo en el paso 3, donde cada producto lleva el suyo.</p>",
    brandFontHtml(texts),
    '<div class="copy-workbench">',
    '<div class="copy-list">', items.map((item) => copyRow(project, item)).join(""), "</div>",
    copyPreviewHtml(project),
    "</div></section>",
  ].join("");
}

async function sendCopySplit(
  project: Project, layerId: string, undo: boolean,
): Promise<void> {
  busy(
    undo ? "Volviendo a unir" : "Separando en partes",
    undo ? "Devolviendo las piezas a su capa…" : "Buscando dónde acaba cada pieza…",
    30,
  );
  try {
    const response = await post<any>(
      "/projects/" + project.project_id + "/layers/" + layerId +
        (undo ? "/unsplit" : "/split"),
      {},
    );
    (response.warnings || []).forEach((warning: string) => toast(warning, "info"));
    await refreshProject(project.project_id);
    await loadTexts(project.project_id, true);
    await renderLayers();
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    idle();
  }
}


async function sendCopyEdit(project: Project, payload: Record<string, unknown>): Promise<void> {
  busy("Guardando el arte", "Aplicando el cambio sobre el KV…", 30);
  try {
    const layerId = String(payload.layer_id);
    delete payload.layer_id;
    const response = await post<any>(
      "/projects/" + project.project_id + "/layers/" + layerId + "/text",
      payload,
    );
    (response.warnings || []).forEach((warning: string) => toast(warning, "info"));
    delete state.copyDrafts[layerId];
    await refreshProject(project.project_id);
    await loadTexts(project.project_id, true);
    await renderLayers();
    toast("Arte actualizado.", "success");
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    idle();
  }
}

function bindCopyEditor(project: Project): void {
  query("#refresh-copy-preview")?.addEventListener("click", () =>
    refreshCopyPreview(project.project_id),
  );
  query("#open-brand-font")?.addEventListener("click", () => {
    const form = query<HTMLElement>("#brand-font-form");
    if (form) form.hidden = !form.hidden;
  });
  query("#font-client")?.addEventListener("change", () => {
    const clientId = query<HTMLSelectElement>("#font-client")?.value || "";
    const savedFields = query<HTMLElement>("#saved-font-fields");
    const uploadFields = query<HTMLElement>("#uploaded-font-fields");
    if (savedFields) savedFields.hidden = !clientId;
    if (uploadFields) uploadFields.hidden = !!clientId;
    for (const id of ["#saved-font", "#saved-font-bold"]) {
      const select = query<HTMLSelectElement>(id);
      if (select) select.innerHTML = savedFontOptions(clientId);
    }
  });
  query("#save-brand-font")?.addEventListener("click", () => void saveBrandFont(project));
  queryAll<HTMLButtonElement>(".zoom-copy").forEach((button) => {
    button.addEventListener("click", () => {
      const panel = query<HTMLElement>('.copy-zoom[data-layer="' + button.dataset.layer + '"]');
      if (panel) panel.hidden = !panel.hidden;
    });
  });
  queryAll<HTMLTextAreaElement>(".copy-text").forEach((field) => {
    field.addEventListener("input", () => {
      state.copyDrafts[field.dataset.layer!] = field.value;
    });
  });
  queryAll<HTMLInputElement>(".replace-copy-image").forEach((input) => {
    input.addEventListener("change", async () => {
      const file = input.files?.[0];
      if (!file) return;
      busy("Sustituyendo imagen", "Ajustando el logo al espacio original…", 30);
      try {
        const data = new FormData();
        data.append("image", file);
        const result = await post<any>(
          "/projects/" + project.project_id + "/layers/" + input.dataset.layer + "/image", data,
        );
        (result.warnings || []).forEach((warning: string) => toast(warning, "info"));
        delete state.copyDrafts[input.dataset.layer!];
        await refreshProject(project.project_id);
        await loadTexts(project.project_id, true);
        await renderLayers();
        toast("Imagen sustituida. Ya aparece en la vista previa y se usará en el arte final.", "success");
      } catch (error) {
        toast(errorMessage(error), "error");
      } finally {
        input.value = "";
        idle();
      }
    });
  });
  queryAll<HTMLButtonElement>(".save-copy").forEach((button) => {
    button.addEventListener("click", () => {
      const id = button.dataset.layer!;
      const field = query<HTMLTextAreaElement>('.copy-text[data-layer="' + id + '"]');
      const content = (field?.value || "").trim();
      if (!content) {
        toast("Escribe el texto nuevo, o usa “Quitar del arte” si sobra.", "error");
        return;
      }
      const row = button.closest(".copy-row")!;
      const color = row.querySelector<HTMLInputElement>(".copy-color")?.value;
      const align = row.querySelector<HTMLSelectElement>(".copy-align")?.value;
      void sendCopyEdit(project, { layer_id: id, content, color, align });
    });
  });
  queryAll<HTMLButtonElement>(".restore-copy").forEach((button) => {
    button.addEventListener("click", () => {
      delete state.copyDrafts[button.dataset.layer!];
      void sendCopyEdit(project, { layer_id: button.dataset.layer!, restore: true });
    });
  });
  queryAll<HTMLButtonElement>(".split-copy").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = button.dataset.layer!;
      // Al separar, la capa madre deja de existir y con ella su casilla. Si
      // había texto escrito sin guardar se perdía en silencio.
      const draft = state.copyDrafts[id];
      const item = (state.texts[project.project_id] || EMPTY_TEXTS).layers
        .find((entry) => entry.id === id);
      if (draft !== undefined && item && draft !== item.text) {
        const seguir = await confirmAction({
          title: "Tienes texto sin guardar en «" + item.name + "»",
          lines: [
            "Al separarla en partes esa casilla desaparece y el texto se pierde: cada parte se reescribe por su cuenta.",
            "Cancela para guardarlo primero, o sepárala de todas formas.",
          ],
          confirm: "Separar de todas formas",
          remember: "separar-copy-sin-guardar",
        });
        if (!seguir) return;
        delete state.copyDrafts[id];
      }
      void sendCopySplit(project, id, false);
    });
  });
  queryAll<HTMLButtonElement>(".unsplit-copy").forEach((button) => {
    button.addEventListener("click", () =>
      void sendCopySplit(project, button.dataset.layer!, true),
    );
  });
  queryAll<HTMLInputElement>(".remove-copy").forEach((box) => {
    box.addEventListener("change", () =>
      void sendCopyEdit(project, { layer_id: box.dataset.layer!, removed: box.checked }),
    );
  });
}

function bindLayerPicks(project: Project, layers: Layer[]): void {
  const marcadas = layerPicks(project.project_id);
  // El orden del rango es el de la pantalla, no el de profundidad: agrupado por
  // arte los dos no coinciden, y con Mayús se marcaría un tramo que no es el
  // que se ve. Se lee de las casillas ya pintadas.
  const orden = queryAll<HTMLInputElement>(".layer-pick-box")
    .map((box) => box.dataset.layerId || "")
    .filter(Boolean);

  const repintar = () => void renderLayers();

  queryAll<HTMLInputElement>(".layer-pick-box").forEach((box) => {
    box.addEventListener("click", (event) => {
      const id = box.dataset.layerId!;
      const rango = (event as MouseEvent).shiftKey && state.lastLayerPick;
      if (rango) {
        // Las capas de un mismo arte llegan seguidas del PSD, así que el tramo
        // es justo lo que se quiere marcar.
        const desde = orden.indexOf(state.lastLayerPick!);
        const hasta = orden.indexOf(id);
        if (desde >= 0 && hasta >= 0) {
          const [a, b] = desde < hasta ? [desde, hasta] : [hasta, desde];
          for (const entre of orden.slice(a, b + 1)) {
            if (box.checked) marcadas.add(entre);
            else marcadas.delete(entre);
          }
        }
      } else if (box.checked) {
        marcadas.add(id);
      } else {
        marcadas.delete(id);
      }
      state.lastLayerPick = id;
      repintar();
    });
  });

  queryAll<HTMLInputElement>(".layer-group-box").forEach((box) => {
    box.addEventListener("click", () => {
      const grupo = box.dataset.group ?? "";
      for (const item of layers) {
        if (layerGroup(item) !== grupo) continue;
        if (box.checked) marcadas.add(item.id);
        else marcadas.delete(item.id);
      }
      repintar();
    });
  });

  query("#pick-all-layers")?.addEventListener("click", () => {
    for (const item of layers) marcadas.add(item.id);
    repintar();
  });
  query("#clear-layer-picks")?.addEventListener("click", () => {
    marcadas.clear();
    state.lastLayerPick = null;
    repintar();
  });

  query("#delete-layer-picks")?.addEventListener("click", async () => {
    const ids = layers.filter((item) => marcadas.has(item.id)).map((item) => item.id);
    if (!ids.length) return;
    const nombres = layers
      .filter((item) => marcadas.has(item.id))
      .map((item) => item.name);
    const confirmado = await confirmAction({
      title: "¿Eliminar " + String(ids.length) + " capa(s)?",
      lines: [
        nombres.slice(0, 6).join(", ") + (nombres.length > 6
          ? " y " + String(nombres.length - 6) + " más."
          : "."),
        "Se quitan de este KV y no se puede deshacer.",
      ],
      confirm: "Sí, eliminar las " + String(ids.length),
    });
    if (!confirmado) return;
    busy("Eliminando capas", String(ids.length) + " capas de " + project.name, 20);
    try {
      // Una sola petición: el endpoint ya aceptaba la lista entera.
      await put("/projects/" + project.project_id + "/layers", { updates: [], delete: ids });
      marcadas.clear();
      state.lastLayerPick = null;
      if (state.selectedLayerId && ids.includes(state.selectedLayerId)) {
        state.selectedLayerId = null;
      }
      await refreshProject(project.project_id);
      toast(String(ids.length) + " capas eliminadas.", "success");
      await renderLayers();
    } catch (error) {
      toast(errorMessage(error), "error");
    } finally {
      idle();
    }
  });
}

function bindLayerActions(project: Project, layer: Layer | null, layers: Layer[]): void {
  query<HTMLSelectElement>("#active-kv")?.addEventListener("change", async (event) => {
    state.activeId = (event.currentTarget as HTMLSelectElement).value;
    state.selectedLayerId = null;
    state.lastLayerPick = null;
    saveSession();
    await navigate("layers");
  });
  queryAll<HTMLButtonElement>(".layer-item").forEach((button) => {
    button.addEventListener("click", async () => {
      state.selectedLayerId = button.dataset.layerId || null;
      await renderLayers();
    });
  });
  bindLayerPicks(project, layers);
  query("#analyze-project")?.addEventListener("click", () => runProjectAction(
    "Detectando elementos", () => post("/projects/" + project.project_id + "/analyze", {
      run_ocr: true, run_segmentation: true, max_regions: 20, extract: true,
    }), "layers",
  ));
  query("#extract-layers")?.addEventListener("click", () => runProjectAction(
    "Extrayendo capas", () => post("/projects/" + project.project_id + "/extract", {
      layer_ids: null, feather: 2, force: true,
    }), "layers",
  ));
  query("#rebuild-background")?.addEventListener("click", () => {
    // La lista arranca vacía: «Automático» no elige modelo, lo hace el servidor.
    const pintarModelos = (engine: string) => {
      const select = query<HTMLSelectElement>("#background-model");
      if (!select) return;
      const opciones = modelOptionsFor(engine);
      select.innerHTML = '<option value="">Predeterminado</option>' + opciones;
      select.disabled = !opciones;
      const nota = query<HTMLElement>("#background-model-note");
      if (nota) {
        nota.textContent = opciones
          ? "Modelos de " + engineLabel(engine) + "."
          : "Lo elige el servidor según las claves que tenga.";
      }
    };
    content().insertAdjacentHTML("afterbegin", [
      '<section class="card elevated" id="background-panel" style="margin-bottom:18px"><div class="card-head"><div><h2>Reconstruir fondo</h2><p>Elige el motor sin modificar las capas</p></div><button class="icon-button" id="close-background">×</button></div>',
      '<div class="form-grid"><label class="field"><span>Motor</span><select id="background-engine"><option value="auto">Automático</option>',
      engineTakesModel("magnific") ? '<option value="magnific">Magnific</option>' : "",
      engineTakesModel("openai") ? '<option value="openai">OpenAI</option>' : "",
      '</select></label>',
      '<label class="field"><span>Modelo de IA</span><select id="background-model"><option value="">Predeterminado</option></select>',
      '<small id="background-model-note">Lo elige el servidor según las claves que tenga.</small></label></div>',
      '<label class="field" style="margin-top:12px"><span>Dirección visual</span><textarea id="background-prompt" placeholder="Fondo limpio, sin texto ni logos"></textarea></label>',
      '<div class="form-grid" style="margin-top:12px"><label class="field"><span>Expansión de máscara</span><input id="background-dilate" type="number" min="0" max="64" value="8"></label>',
      '<button class="button" id="run-background">Reconstruir</button></div></section>',
    ].join(""));
    query("#close-background")?.addEventListener("click", () => query("#background-panel")?.remove());
    query<HTMLSelectElement>("#background-engine")?.addEventListener("change", (event) =>
      pintarModelos((event.currentTarget as HTMLSelectElement).value),
    );
    query("#run-background")?.addEventListener("click", () => runProjectAction(
      "Reconstruyendo fondo",
      () => post("/projects/" + project.project_id + "/reconstruct-background", {
        provider: query<HTMLSelectElement>("#background-engine")!.value,
        model: query<HTMLSelectElement>("#background-model")!.value || null,
        prompt: query<HTMLTextAreaElement>("#background-prompt")!.value || null,
        dilate: Number(query<HTMLInputElement>("#background-dilate")!.value),
      }),
      "layers",
    ));
  });
  query("#show-detections")?.addEventListener("click", () => {
    window.open("/api/projects/" + project.project_id + "/preview/detections", "_blank", "noopener");
  });

  if (layer) bindSingleLayer(project, layer);

  queryAll<HTMLButtonElement>(".move-layer").forEach((button) => {
    button.addEventListener("click", async () => {
      const index = Number(button.dataset.index);
      const direction = Number(button.dataset.direction);
      const ordered = [...layers];
      const other = index + direction;
      [ordered[index], ordered[other]] = [ordered[other], ordered[index]];
      try {
        await put("/projects/" + project.project_id + "/layers", {
          updates: [], delete: [], order: ordered.map((item) => item.id).reverse(),
        });
        await refreshProject(project.project_id);
        await renderLayers();
      } catch (error) {
        toast(errorMessage(error), "error");
      }
    });
  });
  /* Cuántas capas van marcadas como Producto en lo que hay en pantalla.
     Es el requisito que después bloquea la generación, así que se dice aquí,
     con las capas delante, y no al final del flujo. */
  const refreshRoleStatus = () => {
    const host = query<HTMLElement>("#role-status");
    if (!host) return;
    const marcadas = queryAll<HTMLSelectElement>(".role-select")
      .filter((item) => item.value === "product").length;
    host.innerHTML = marcadas
      ? '<div class="notice success">' + String(marcadas) +
        " capa(s) marcada(s) como <strong>Producto</strong>: son las que se retirarán para poner el producto nuevo.</div>"
      : [
          '<div class="notice error"><strong>Ninguna capa está marcada como Producto.</strong> ',
          "Este KV no podrá generar artes: el sistema no sabría qué pieza retirar. ",
          "Marca la capa del producto como <em>“Producto original · eliminar y reemplazar”</em>, ",
          "o deja que la IA la detecte.",
          '<div class="button-row" style="margin-top:10px">',
          '<button class="ghost-button" id="detect-here">Detectar el producto con IA en este KV</button>',
          "</div></div>",
        ].join("");

    query("#detect-here")?.addEventListener("click", async () => {
      busy("Detectando producto", "Separando el sujeto principal del fondo…", 15);
      try {
        const result = await post<any>(
          "/projects/" + project.project_id + "/layers/detect-product",
          { force: false },
        );
        await refreshProject(project.project_id);
        (result.warnings || []).forEach((warning: string) => toast(warning, "info"));
        toast(
          result.detected
            ? "Producto detectado. Revisa que la capa nueva sea la correcta."
            : "No se pudo detectar. Marca la capa del producto a mano.",
          result.detected ? "success" : "error",
        );
        await renderLayers();
      } catch (error) {
        toast(errorMessage(error), "error");
      } finally {
        idle();
      }
    });
  };
  refreshRoleStatus();
  queryAll<HTMLSelectElement>(".role-select").forEach((selectBox) => {
    selectBox.addEventListener("change", refreshRoleStatus);
  });

  // Cambiar de KV desde las fichas de arriba.
  queryAll<HTMLButtonElement>(".kv-chip").forEach((button) => {
    button.addEventListener("click", async () => {
      state.activeId = button.dataset.kv || null;
      state.selectedLayerId = null;
      saveSession();
      await renderLayers();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  });

  // Quitar un KV de la campaña. Un pliego trae portadas y piezas que no se van
  // a producir, y hasta ahora arrastraban hasta el final: contaban en "0 de 20",
  // pedían capas confirmadas y se generaban igual.
  queryAll<HTMLButtonElement>(".kv-drop").forEach((button) => {
    button.addEventListener("click", () => dropKv(button.dataset.drop!));
  });
  bindKvPicks();

  query("#confirm-roles")?.addEventListener("click", async () => {
    const selections = readRoleSelections();
    // Guardar sin producto deja el KV inservible para generar. Se avisa aquí
    // y no tres pasos después, cuando ya no se recuerda qué KV era.
    if (!selections.some((item) => item.category === "product")) {
      const seguir = await confirmAction({
        title: "«" + project.name + "» no tiene ninguna capa marcada como Producto",
        lines: [
          "Así no podrá generar artes: el sistema no sabe qué pieza retirar.",
          "Cancela para marcarla ahora, o guarda de todas formas.",
        ],
        confirm: "Guardar de todas formas",
        // No borra nada: es un aviso de que el KV queda inservible para generar.
        danger: false,
        remember: "guardar-sin-producto",
      });
      if (!seguir) return;
    }
    busy("Guardando revisión", "Actualizando funciones de las capas…", 35);
    try {
      await put("/projects/" + project.project_id + "/layers", {
        updates: selections,
        delete: [],
      });
      await refreshProject(project.project_id);

      // Se salta al siguiente KV sin confirmar, como hacía el flujo anterior:
      // guardar y quedarse en el mismo KV era lo que parecía "no vale".
      const next = state.campaign.find(
        (item) => item.project_id !== project.project_id && !layersConfirmed(item),
      );
      if (next) {
        state.activeId = next.project_id;
        state.selectedLayerId = null;
        saveSession();
        toast("Capas confirmadas. Sigue con " + next.name + ".", "success");
      } else {
        toast("Todos los KV de la campaña quedaron confirmados.", "success");
      }
      await renderLayers();
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (error) {
      toast(errorMessage(error), "error");
    } finally {
      idle();
    }
  });

  query("#apply-to-all")?.addEventListener("click", async () => {
    /* Las piezas de un mismo PSD repiten logo, legal, CTA y decoración, así que
       copiarlas ahorra decenas de desplegables.

       El producto NO se copia, a propósito. En un PSD real cada pieza nombra su
       producto distinto ("copy 7", "copy 8", "copy 3 2"…), mientras los nombres
       genéricos ("Vector Smart Object 2") sí se repiten apuntando a piezas que
       no son el producto. Copiar ese rol marcaría como producto una capa
       cualquiera, y el arte saldría mal sin avisar. Se marca KV por KV. */
    const selections = readRoleSelections();
    const roleByName = new Map<string, Record<string, any>>();
    layers.forEach((layer) => {
      const match = selections.find((item) => item.id === layer.id);
      if (match && match.category !== "product") roleByName.set(layer.name, match);
    });

    const others = state.campaign.filter((item) => item.project_id !== project.project_id);
    busy("Copiando la revisión", "Aplicando logo, legales, CTA y decoración…", 5);
    let applied = 0;
    try {
      // El KV actual también se guarda: si no, quedaría como el único sin confirmar.
      await put("/projects/" + project.project_id + "/layers", { updates: selections, delete: [] });
      await refreshProject(project.project_id);

      for (let index = 0; index < others.length; index += 1) {
        const target = others[index];
        busyProgress(8 + Math.round((index / Math.max(1, others.length)) * 88), target.name);
        const updates = target.layers
          .filter((layer) => layer.category !== "background")
          .map((layer) => {
            const copied = matchRole(roleByName, layer.name);
            if (copied) return { ...copied, id: layer.id };
            // Sin coincidencia se conserva lo que ya tenía, pero se confirma:
            // así el KV no queda a medias bloqueando el paso siguiente.
            return {
              id: layer.id,
              category: layer.category,
              visible: layer.visible,
              locked: MANDATORY.has(layer.category),
              replaceable: layer.category === "product",
              preserve_aspect_ratio: true,
            };
          });
        if (!updates.length) continue;
        await put("/projects/" + target.project_id + "/layers", { updates, delete: [] });
        await refreshProject(target.project_id);
        applied += 1;
      }

      toast("Logo, legales, CTA y decoración copiados a " + String(applied) + " KV.", "success");
      const sinProducto = missingProductTargets();
      if (sinProducto.length) {
        toast(
          "El producto se marca en cada KV: falta en " + nameList(sinProducto, 4) + ".",
          "info",
        );
        state.activeId = sinProducto[0].project_id;
        state.selectedLayerId = null;
        saveSession();
      }
      await renderLayers();
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (error) {
      toast(errorMessage(error), "error");
    } finally {
      idle();
    }
  });

  query("#new-layer")?.addEventListener("click", () => showNewLayer(project));
}

/** Nombre sin el sufijo de duplicado que añade Photoshop.
 *  Un PSD trae "GRATIS" en una pieza y "GRATIS copy" en otra: son la misma
 *  capa y sin esto no se reconocían. No se tocan los números sueltos, porque
 *  "Vector Smart Object 2" y "…3" sí son capas distintas. */
function normalizeLayerName(name: string): string {
  return name.trim().replace(/(\s+cop(y|ia))+(\s+\d+)*$/i, "").trim();
}

/** Busca el rol por nombre exacto y, si no, por nombre normalizado. */
function matchRole(
  roleByName: Map<string, Record<string, any>>,
  name: string,
): Record<string, any> | null {
  const exact = roleByName.get(name);
  if (exact) return exact;
  const target = normalizeLayerName(name).toLowerCase();
  if (!target) return null;
  // Solo si es inequívoco: dos capas distintas no pueden reclamar el mismo rol.
  const candidates = Array.from(roleByName.entries()).filter(
    ([key]) => normalizeLayerName(key).toLowerCase() === target,
  );
  return candidates.length === 1 ? candidates[0][1] : null;
}

/** Lo que el usuario marcó en los desplegables de función. */
function readRoleSelections(): Array<Record<string, any>> {
  return queryAll<HTMLSelectElement>(".role-select").map((selectBox) => {
    const role = selectBox.value;
    const ignored = role === "ignore";
    return {
      id: selectBox.dataset.id,
      category: ignored ? "decoration" : role,
      visible: !ignored && role !== "background",
      locked: MANDATORY.has(role),
      replaceable: role === "product",
      preserve_aspect_ratio: true,
    };
  });
}

async function runProjectAction(
  title: string,
  action: () => Promise<any>,
  view: ViewName,
): Promise<void> {
  const project = activeProject();
  if (!project) return;
  busy(title, "El motor está procesando el KV…", 18);
  try {
    const result = await action();
    await refreshProject(project.project_id);
    (result?.warnings || []).forEach((warning: string) => toast(warning, "info"));
    toast(title + " completado.", "success");
    await navigate(view);
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    idle();
  }
}

function formValue(form: FormData, name: string, fallback = ""): string {
  return String(form.get(name) ?? fallback);
}

function bindSingleLayer(project: Project, layer: Layer): void {
  query<HTMLFormElement>("#layer-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget as HTMLFormElement);
    const category = formValue(form, "category");
    const payload: Record<string, any> = {
      id: layer.id,
      name: formValue(form, "name"),
      category,
      x: Number(formValue(form, "x", "0")),
      y: Number(formValue(form, "y", "0")),
      width: Number(formValue(form, "width", "1")),
      height: Number(formValue(form, "height", "1")),
      visible: form.has("visible"),
      locked: form.has("locked"),
      movable: form.has("movable"),
      resizable: form.has("resizable"),
      reorderable: form.has("reorderable"),
      replaceable: form.has("replaceable"),
      preserve_aspect_ratio: form.has("preserve_aspect_ratio"),
    };
    if (layer.type === "text" || category === "legal") {
      Object.assign(payload, {
        content: formValue(form, "content"),
        font_size: Number(formValue(form, "font_size", "48")),
        font_weight: formValue(form, "font_weight", "normal"),
        text_align: formValue(form, "text_align", "left"),
        color: formValue(form, "color", "#ffffff"),
        auto_contrast: form.has("auto_contrast"),
        export_as_text: form.has("export_as_text"),
        text_verified: form.has("text_verified"),
      });
    }
    busy("Guardando capa", "Aplicando geometría y comportamiento…", 40);
    try {
      await put("/projects/" + project.project_id + "/layers", { updates: [payload], delete: [] });
      await refreshProject(project.project_id);
      toast("Capa actualizada.", "success");
      await renderLayers();
    } catch (error) {
      toast(errorMessage(error), "error");
    } finally {
      idle();
    }
  });
  query("#delete-layer")?.addEventListener("click", async () => {
    const confirmed = await confirmAction({
      title: "¿Eliminar la capa «" + layer.name + "»?",
      lines: ["Se quita de este KV y no se puede deshacer."],
      confirm: "Sí, eliminar",
      remember: "eliminar-capa",
    });
    if (!confirmed) return;
    try {
      await put("/projects/" + project.project_id + "/layers", {
        updates: [], delete: [layer.id],
      });
      state.selectedLayerId = null;
      await refreshProject(project.project_id);
      toast("Capa eliminada.", "success");
      await renderLayers();
    } catch (error) {
      toast(errorMessage(error), "error");
    }
  });
  query("#auto-segment")?.addEventListener("click", () => updateMask(project, {
    layer_id: layer.id, auto_segment: true, re_extract: true,
  }));
  query("#reset-mask")?.addEventListener("click", () => updateMask(project, {
    layer_id: layer.id, reset_from_box: true, re_extract: true,
  }));
  query("#draw-mask")?.addEventListener("click", () => void enableMaskCanvas(project, layer));
}

async function updateMask(project: Project, payload: any): Promise<void> {
  busy("Actualizando máscara", "Recortando la capa nuevamente…", 30);
  try {
    await post("/projects/" + project.project_id + "/layers/mask", payload);
    await refreshProject(project.project_id);
    toast("Máscara actualizada.", "success");
    await renderLayers();
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    idle();
  }
}

async function enableMaskCanvas(project: Project, layer: Layer): Promise<void> {
  const source = query<HTMLImageElement>("#mask-source")!;
  const canvas = query<HTMLCanvasElement>("#mask-canvas")!;
  const tools = query<HTMLElement>("#mask-tools")!;
  const rect = source.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(rect.width));
  canvas.height = Math.max(1, Math.round(rect.height));
  canvas.style.width = rect.width + "px";
  canvas.style.height = rect.height + "px";
  canvas.hidden = false;
  tools.hidden = false;
  const ctx = canvas.getContext("2d")!;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  // Se edita la máscara existente. Antes el canvas comenzaba transparente y al
  // guardar reemplazaba todo por los últimos trazos; "Borrar" tampoco podía
  // quitar nada de un lienzo vacío.
  if (layer.mask) {
    try {
      const maskImage = new Image();
      maskImage.src = fileUrl(project.project_id, layer.mask);
      await maskImage.decode();
      const raw = document.createElement("canvas");
      raw.width = canvas.width;
      raw.height = canvas.height;
      const rawCtx = raw.getContext("2d")!;
      rawCtx.drawImage(maskImage, 0, 0, raw.width, raw.height);
      const pixels = rawCtx.getImageData(0, 0, raw.width, raw.height);
      for (let index = 0; index < pixels.data.length; index += 4) {
        const value = pixels.data[index];
        pixels.data[index] = 0;
        pixels.data[index + 1] = 255;
        pixels.data[index + 2] = 120;
        pixels.data[index + 3] = value;
      }
      ctx.putImageData(pixels, 0, 0);
    } catch {
      toast("No se pudo cargar la máscara actual; no se modificó el recorte.", "error");
      canvas.hidden = true;
      tools.hidden = true;
      return;
    }
  }
  let drawing = false;
  const point = (event: PointerEvent) => {
    const bounds = canvas.getBoundingClientRect();
    return {
      x: (event.clientX - bounds.left) * canvas.width / bounds.width,
      y: (event.clientY - bounds.top) * canvas.height / bounds.height,
    };
  };
  canvas.onpointerdown = (event) => {
    drawing = true;
    canvas.setPointerCapture(event.pointerId);
    const p = point(event);
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
  };
  canvas.onpointermove = (event) => {
    if (!drawing) return;
    const p = point(event);
    const mode = query<HTMLSelectElement>("#brush-mode")!.value;
    ctx.globalCompositeOperation = mode === "subtract" ? "destination-out" : "source-over";
    ctx.strokeStyle = "rgba(0,255,120,.92)";
    ctx.lineWidth = Number(query<HTMLInputElement>("#brush-size")!.value);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.lineTo(p.x, p.y);
    ctx.stroke();
  };
  canvas.onpointerup = () => { drawing = false; };
  query("#cancel-mask")?.addEventListener("click", () => {
    canvas.hidden = true;
    tools.hidden = true;
  });
  query("#save-mask")?.addEventListener("click", async () => {
    const output = document.createElement("canvas");
    output.width = project.canvas.width;
    output.height = project.canvas.height;
    const out = output.getContext("2d")!;
    out.drawImage(canvas, 0, 0, output.width, output.height);
    const pixels = out.getImageData(0, 0, output.width, output.height);
    for (let index = 0; index < pixels.data.length; index += 4) {
      const alpha = pixels.data[index + 3];
      pixels.data[index] = alpha;
      pixels.data[index + 1] = alpha;
      pixels.data[index + 2] = alpha;
      pixels.data[index + 3] = 255;
    }
    out.putImageData(pixels, 0, 0);
    const blob = await new Promise<Blob | null>((resolve) => output.toBlob(resolve, "image/png"));
    if (!blob) return;
    const data = new FormData();
    data.append("mask_file", blob, "mask.png");
    busy("Guardando máscara", "Generando el recorte transparente…", 35);
    try {
      await post("/projects/" + project.project_id + "/layers/" + layer.id + "/mask/upload", data);
      await refreshProject(project.project_id);
      toast("Máscara dibujada guardada.", "success");
      await renderLayers();
    } catch (error) {
      toast(errorMessage(error), "error");
    } finally {
      idle();
    }
  });
}

function showNewLayer(project: Project): void {
  const markup = [
    '<section class="card elevated" id="new-layer-panel" style="margin-bottom:18px"><div class="card-head"><div><h2>Nueva capa</h2><p>Rectángulo manual sobre el KV</p></div><button class="icon-button" id="close-new-layer">×</button></div>',
    '<form id="new-layer-form" class="stack"><div class="form-grid"><label class="field"><span>Nombre</span><input name="name" value="Nueva capa"></label>',
    '<label class="field"><span>Categoría</span><select name="category">', optionList(CATEGORY_LABELS, "decoration").replace('<option value="background">Fondo</option>', ""), "</select></label></div>",
    '<div class="form-grid"><label class="field"><span>Tipo</span><select name="type"><option value="image">Imagen</option><option value="text">Texto</option></select></label>',
    '<label class="field"><span>Contenido si es texto</span><input name="content"></label></div>',
    '<div class="form-grid four"><label class="field"><span>X</span><input name="x" type="number" value="', String(Math.round(project.canvas.width * .1)), '"></label>',
    '<label class="field"><span>Y</span><input name="y" type="number" value="', String(Math.round(project.canvas.height * .1)), '"></label>',
    '<label class="field"><span>Ancho</span><input name="width" type="number" value="', String(Math.round(project.canvas.width * .3)), '"></label>',
    '<label class="field"><span>Alto</span><input name="height" type="number" value="', String(Math.round(project.canvas.height * .2)), '"></label></div>',
    switchHtml("auto_segment", "Segmentar automáticamente", true),
    '<button class="button" type="submit">Crear capa</button></form></section>',
  ].join("");
  content().insertAdjacentHTML("afterbegin", markup);
  query("#close-new-layer")?.addEventListener("click", () => query("#new-layer-panel")?.remove());
  query<HTMLFormElement>("#new-layer-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget as HTMLFormElement);
    busy("Creando capa", "Preparando máscara y recorte…", 30);
    try {
      const created = await post<Layer>("/projects/" + project.project_id + "/layers", {
        name: formValue(form, "name"),
        category: formValue(form, "category"),
        type: formValue(form, "type"),
        content: formValue(form, "content") || null,
        x: Number(formValue(form, "x")),
        y: Number(formValue(form, "y")),
        width: Number(formValue(form, "width")),
        height: Number(formValue(form, "height")),
        auto_segment: form.has("auto_segment"),
      });
      state.selectedLayerId = created.id;
      await refreshProject(project.project_id);
      toast("Capa creada.", "success");
      await renderLayers();
    } catch (error) {
      toast(errorMessage(error), "error");
    } finally {
      idle();
    }
  });
}

function productKey(file: File): string {
  return file.name + "::" + String(file.size) + "::" + String(file.lastModified);
}

function mergeUniqueFiles(current: File[], incoming: File[]): File[] {
  const byKey = new Map(current.map((file) => [productKey(file), file]));
  incoming.forEach((file) => {
    if (!byKey.has(productKey(file))) byKey.set(productKey(file), file);
  });
  return Array.from(byKey.values());
}

function productName(file: File): string {
  return file.name.replace(/\.[^.]+$/, "");
}

function productUrl(file: File): string {
  let url = productUrls.get(file);
  if (!url) {
    url = URL.createObjectURL(file);
    productUrls.set(file, url);
  }
  return url;
}

function selectedProductFiles(): File[] {
  return state.products.filter((file) => state.individualProducts.has(productKey(file)));
}

/* Un arte con menos capas que esto no se puede recomponer: no hay piezas que
   mover, solo una imagen que colocar. Los mismos números que usa el motor. */
const MIN_LAYERS_TO_RECOMPOSE = 3;
const MIN_SOURCE_COVERAGE = 0.45;

/** Qué cabe en cada formato, tomando el KV más apretado de la campaña.
 *
 * Lo que se elige aquí se aplica a todos, así que manda el que menos aguanta,
 * igual que con la cobertura. El cálculo es del backend: es la misma geometría
 * con la que después se compone la pieza, y duplicarla aquí sería garantizar
 * que las dos respuestas se separen. */
async function peorCupoPorFormato(ids: string[]): Promise<Record<string, FormatFit>> {
  if (!ids.length) return {};
  const respuestas = await Promise.all(
    ids.map((id) =>
      get<FormatFitResponse>("/projects/" + id + "/formats").catch(() => null)),
  );
  const peor: Record<string, FormatFit> = {};
  for (const respuesta of respuestas) {
    for (const cupo of respuesta?.formats || []) {
      const previo = peor[cupo.id];
      if (!previo || cupo.total - cupo.fits > previo.total - previo.fits) {
        peor[cupo.id] = cupo;
      }
    }
  }
  return peor;
}

/** Lo que sobra en un formato: los elementos que no entran con tamaño legible. */
function sobraEnFormato(spec: FormatPreset): string[] {
  const cupo = state.formatFit[spec.id];
  return cupo && cupo.total > cupo.fits ? cupo.dropped : [];
}

function coberturaEnFormato(sw: number, sh: number, tw: number, th: number): number {
  if (Math.min(sw, sh, tw, th) <= 0) return 0;
  const escala = Math.min(tw / sw, th / sh);
  return (sw * escala * sh * escala) / (tw * th);
}

function capasUtiles(project: Project): number {
  return project.layers.filter((layer) =>
    layer.category !== "background" && layer.visible &&
    (layer.type === "text" ? Boolean((layer.content || "").trim()) : Boolean(layer.src))
  ).length;
}

/** Cuánto del formato llena el KV tal como está: 1 = lo llena entero.
 *
 * Es la lectura del formato de origen que decide qué recomendar. Se toma el peor
 * KV de la campaña, porque lo que se elige aquí se aplica a todos. */
function coberturaDelKv(spec: FormatPreset): number {
  if (!state.campaign.length) return 1;
  return Math.min(...state.campaign.map((project) =>
    coberturaEnFormato(project.canvas.width, project.canvas.height, spec.width, spec.height)));
}

/** Encaja con la proporción del arte: sale sin tocar nada. */
function formatoRecomendado(spec: FormatPreset): boolean {
  return coberturaDelKv(spec) >= MIN_SOURCE_COVERAGE;
}

/** Hay que separar el arte en capas antes de poder llenar este formato.
 *
 * No es un "no se puede": el motor sabe separar el copy con OCR y los objetos
 * con SAM, y recomponer con las piezas sueltas. Solo es un aviso de que va a
 * hacerlo, porque tarda más y el resultado ya no es el arte original intacto. */
function necesitaSeparar(spec: FormatPreset): boolean {
  if (formatoRecomendado(spec)) return false;
  return state.campaign.some((project) => capasUtiles(project) < MIN_LAYERS_TO_RECOMPOSE);
}

function formatCard(spec: FormatPreset): string {
  const maxWidth = 46;
  const maxHeight = 64;
  const scale = Math.min(maxWidth / Math.max(1, spec.width), maxHeight / Math.max(1, spec.height));
  const width = Math.max(10, Math.round(spec.width * scale));
  const height = Math.max(10, Math.round(spec.height * scale));
  const safe = spec.safe_area || { left: .035, top: .035, right: .035, bottom: .035 };
  const style = [
    "--shape-w:" + String(width) + "px",
    "--shape-h:" + String(height) + "px",
    "--safe-l:" + String(Number(safe.left || 0) * 100) + "%",
    "--safe-t:" + String(Number(safe.top || 0) * 100) + "%",
    "--safe-r:" + String(Number(safe.right || 0) * 100) + "%",
    "--safe-b:" + String(Number(safe.bottom || 0) * 100) + "%",
  ].join(";");
  const encaja = formatoRecomendado(spec);
  const separa = necesitaSeparar(spec);
  const sobra = sobraEnFormato(spec);
  const cupo = state.formatFit[spec.id];
  // La capacidad manda sobre lo demás: que la proporción encaje no sirve de nada
  // si el copy no cabe. Decirlo aquí es decirlo antes de generar.
  const nota = sobra.length && cupo
    ? "Solo entran " + String(cupo.fits) + " de " + String(cupo.total) + " elementos · sobra " +
      esc(sobra.slice(0, 2).join(", ")) + (sobra.length > 2 ? " +" + String(sobra.length - 2) : "")
    : encaja
      ? "Encaja con tu KV"
      : separa
        ? "Se separará el arte · llena el " + String(Math.round(coberturaDelKv(spec) * 100)) + "%"
        : esc(spec.platform) + (spec.recommended ? " · recomendado" : "");
  return [
    '<label class="format-card', sobra.length ? " is-tight" : encaja ? " is-fit" : separa ? " needs-split" : "", '"><input class="format-check" type="checkbox" value="', attr(spec.id), '"',
    checked(state.selectedFormats.has(spec.id)), '><span class="format-shape" style="', attr(style), '"><i class="safe-zone"></i></span>',
    '<span class="format-copy"><strong>', esc(spec.placement), '</strong><span>', String(spec.width), "×", String(spec.height), " · ", esc(spec.ratio),
    '</span><span class="format-note">', nota, "</span></span></label>",
  ].join("");
}

/** Los formatos elegidos en los que no cabe el arte completo. */
function apretadosElegidos(): string[] {
  const catalog = state.capabilities?.format_catalog || [];
  return catalog
    .filter((spec) => state.selectedFormats.has(spec.id) && sobraEnFormato(spec).length)
    .map((spec) => String(spec.width) + "×" + String(spec.height));
}

function formatSelectorHtml(allowAuto: boolean): string {
  const catalog = state.capabilities?.format_catalog || [];
  const platforms = ["Todos", ...Array.from(new Set(catalog.map((item) => item.platform)))];
  const filtrados = state.formatPlatform === "Todos"
    ? catalog
    : catalog.filter((item) => item.platform === state.formatPlatform);
  // Recomendar es ordenar: primero los que salen del KV tal como está, después
  // los que obligan a separarlo. Leer la proporción del arte y no hacer nada con
  // ella era dejarle el trabajo al usuario.
  // Los que no aguantan el arte completo caen al final: primero lo que sale bien.
  const visible = [...filtrados].sort((a, b) =>
    (Number(sobraEnFormato(a).length > 0) - Number(sobraEnFormato(b).length > 0)) ||
    (Number(formatoRecomendado(b)) - Number(formatoRecomendado(a))));
  const filters = platforms.map((platform) =>
    '<button type="button" class="platform-filter' + (platform === state.formatPlatform ? " is-active" : "") +
    '" data-platform="' + attr(platform) + '">' + esc(platform) + "</button>"
  ).join("");
  return [
    '<section class="card"><div class="card-head"><div><h2>Formatos de salida</h2><p>Ubicaciones reales con sus áreas seguras</p></div><span class="badge">',
    String(state.selectedFormats.size), " ELEGIDOS</span></div>",
    allowAuto ? '<label class="choice" style="margin-bottom:14px"><input id="auto-formats" type="checkbox"' + checked(state.autoFormats) + '> Usar el tamaño original del KV</label>' : "",
    '<div id="manual-formats"', allowAuto && state.autoFormats ? " hidden" : "", '><div class="format-platforms">', filters, '</div><div class="format-grid">',
    visible.map(formatCard).join(""), "</div></div>",
    '<p class="muted tiny" style="margin:14px 0 0">Las líneas blancas marcan dónde deben quedar logo, producto, copy y legales.</p>',
    apretadosElegidos().length
      ? '<p class="notice" style="margin:12px 0 0"><strong>' + esc(apretadosElegidos().join(", ")) +
        '</strong>: en ' + (apretadosElegidos().length > 1 ? "esos formatos" : "ese formato") +
        ' no entra el arte completo con tamaño legible. Las piezas se generan igual, pero saldrán apretadas: quita los elementos que sobran en <strong>Revisar capas</strong> o elige un formato más grande.</p>'
      : "",
    catalog.some(necesitaSeparar)
      ? '<p class="notice" style="margin:12px 0 0">Los formatos marcados <strong>no encajan con la proporción de tu KV</strong>, así que para llenarlos el motor separará el arte en capas —copy con OCR, objetos recortados, fondo reconstruido— y lo recompondrá. Tarda más y el resultado ya no es el arte original intacto: si prefieres fidelidad, quédate con los de arriba.</p>'
      : "",
    "</section>",
  ].join("");
}

function productCard(file: File): string {
  const key = productKey(file);
  return [
    '<article class="product-card"><img src="', attr(productUrl(file)), '" alt="', attr(productName(file)), '">',
    '<strong>', esc(file.name), '</strong><label class="check"><input class="individual-product" type="checkbox" value="', attr(key), '"',
    checked(state.individualProducts.has(key)), "> Crear arte individual</label></article>",
  ].join("");
}

function arrangementLabel(value: string): string {
  return ({ auto: "Automática", horizontal: "En fila", vertical: "Apilados", overlap: "Superpuestos" } as Record<string, string>)[value] || value;
}

function groupCard(group: ProductGroup): string {
  const members = group.members.map((key) => state.products.find((file) => productKey(file) === key)).filter(Boolean) as File[];
  return [
    '<article class="group-card"><div class="card-head"><div><h3>', esc(group.name), '</h3><p>', String(members.length), " productos · ", esc(arrangementLabel(group.arrangement)),
    '</p></div><button type="button" class="danger-button remove-group" data-group="', attr(group.id), '">Quitar</button></div>',
    '<div class="group-preview ', attr(group.arrangement), '">', members.map((file) => '<img src="' + attr(productUrl(file)) + '" alt="">').join(""), "</div></article>",
  ].join("");
}

function productTarget(project: Project): Layer | null {
  const products = project.layers.filter((layer) => layer.category === "product");
  return products.find((layer) => layer.replaceable) || products.sort((a, b) => b.width * b.height - a.width * a.height)[0] || null;
}


/* ------------------------------------------------- dónde va el producto
   El motor deduce la zona del producto del hueco que ocupaba en el PSD, y casi
   siempre acierta. Casi: cuando el KV traía tres prendas y entra una sola, o
   cuando el arte se lleva a otra proporción, ese hueco deja el producto donde
   ya no debe ir. Quien lo ve es quien está mirando la pieza, así que aquí puede
   dibujar el recuadro encima del arte. Se guarda en fracciones del lienzo: la
   misma decisión vale para los cinco formatos de la tanda. */

/** Lado mínimo del recuadro. El mismo que valida el backend: por debajo de esto
 *  es un resbalón del ratón, no una decisión. */
const MIN_ZONE = 0.05;

/** KV cuyo recuadro se está dibujando. Puede no ser el activo del paso 2. */
function zoneProject(): Project | null {
  return (
    state.campaign.find((project) => project.project_id === state.zoneProject) ||
    activeProject()
  );
}

/** La zona que el motor usaría si nadie dibuja nada: el hueco del producto. */
function detectedZone(project: Project): ProductZone | null {
  const target = productTarget(project);
  if (!target) return null;
  const box = target.meta?.replacement_box as number[] | undefined;
  const [x, y, width, height] = box?.length === 4
    ? box
    : [target.x, target.y, target.width, target.height];
  return {
    x: x / Math.max(1, project.canvas.width),
    y: y / Math.max(1, project.canvas.height),
    width: width / Math.max(1, project.canvas.width),
    height: height / Math.max(1, project.canvas.height),
  };
}

function zoneStyle(zone: ProductZone): string {
  return [
    "left:" + (zone.x * 100).toFixed(2) + "%",
    "top:" + (zone.y * 100).toFixed(2) + "%",
    "width:" + (zone.width * 100).toFixed(2) + "%",
    "height:" + (zone.height * 100).toFixed(2) + "%",
  ].join(";");
}

function productZoneHtml(project: Project): string {
  const manual = project.product_zone || null;
  const detected = detectedZone(project);
  const shown = manual || detected;
  const others = state.campaign.length - 1;
  const tabs = state.campaign.length > 1
    ? '<div class="zone-kvs">' + state.campaign.map((item) =>
        '<button type="button" class="zone-kv' +
        (item.project_id === project.project_id ? " is-current" : "") +
        (item.product_zone ? " is-set" : "") + '" data-kv="' + attr(item.project_id) + '">' +
        esc(item.name) + (item.product_zone ? " ·&nbsp;✓" : "") + "</button>"
      ).join("") + "</div>"
    : "";
  return [
    '<section class="card" id="product-zone"><div class="card-head"><div><h2>Dónde va el producto</h2>',
    "<p>Arrastra sobre el arte para marcar la zona. Sin recuadro, la decide el motor</p></div>",
    '<span class="badge', manual ? " green" : "", '">', manual ? "ELEGIDA" : "AUTOMÁTICA", "</span></div>",
    tabs,
    '<div class="zone-workbench">',
    '<div class="zone-stage" id="zone-stage" data-project="', attr(project.project_id), '">',
    '<img src="', attr(thumbnailUrl(project.project_id, 720)), '" alt="Arte de ', attr(project.name),
    '" draggable="false" loading="lazy" decoding="async">',
    shown
      ? '<span class="zone-box' + (manual ? " is-manual" : " is-auto") + '" style="' + attr(zoneStyle(shown)) + '"></span>'
      : "",
    '<span class="zone-box is-live" id="zone-live" hidden></span>',
    "</div>",
    '<div class="zone-side">',
    manual
      ? '<p class="notice success">El producto entrará en el recuadro azul, en todos los formatos de la tanda.</p>'
      : detected
        ? '<p class="notice">Ahora mismo el producto entra en el hueco que ocupaba en el KV (recuadro punteado). Arrastra encima del arte si quieres otro sitio.</p>'
        : '<p class="notice error">Este KV todavía no tiene capa de producto, así que no hay hueco que heredar. Puedes marcar la zona igual: se aplicará en cuanto lo tenga.</p>',
    '<div class="button-row">',
    manual ? '<button class="ghost-button small" id="zone-reset">Volver a la zona detectada</button>' : "",
    manual && others > 0
      ? '<button class="ghost-button small" id="zone-copy-all">Aplicar a los otros ' + String(others) + " KV</button>"
      : "",
    "</div>",
    '<small class="muted tiny">El recuadro manda sobre el hueco del PSD y sobre el diseño ',
    "conservado. Lo que quede debajo se aparta al recomponer, o queda detrás si el KV se conserva.</small>",
    "</div></div></section>",
  ].join("");
}

async function saveProductZone(projectId: string, zone: ProductZone | null): Promise<void> {
  try {
    const response = await put<any>("/projects/" + projectId + "/product-zone", { zone });
    (response.warnings || []).forEach((warning: string) => toast(warning, "info"));
    await refreshProject(projectId);
    await renderProducts();
    toast(zone ? "Zona del producto guardada." : "El motor vuelve a decidir la zona.", "success");
  } catch (error) {
    toast(errorMessage(error), "error");
  }
}

function bindProductZone(): void {
  queryAll<HTMLButtonElement>(".zone-kv").forEach((button) => {
    button.addEventListener("click", async () => {
      state.zoneProject = button.dataset.kv || null;
      await renderProducts();
    });
  });

  const stage = query<HTMLElement>("#zone-stage");
  if (!stage) return;
  const projectId = stage.dataset.project!;
  const live = query<HTMLElement>("#zone-live", stage)!;
  let start: { x: number; y: number } | null = null;

  /** Punto del puntero en fracciones del arte, recortado a sus bordes. */
  const point = (event: PointerEvent) => {
    const rect = stage.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (event.clientX - rect.left) / Math.max(1, rect.width))),
      y: Math.min(1, Math.max(0, (event.clientY - rect.top) / Math.max(1, rect.height))),
    };
  };
  const rectFrom = (a: { x: number; y: number }, b: { x: number; y: number }): ProductZone => ({
    x: Math.min(a.x, b.x),
    y: Math.min(a.y, b.y),
    width: Math.abs(b.x - a.x),
    height: Math.abs(b.y - a.y),
  });

  stage.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    start = point(event);
    stage.setPointerCapture(event.pointerId);
    live.hidden = false;
    live.setAttribute("style", zoneStyle(rectFrom(start, start)));
  });
  stage.addEventListener("pointermove", (event) => {
    if (!start) return;
    live.setAttribute("style", zoneStyle(rectFrom(start, point(event))));
  });
  stage.addEventListener("pointerup", async (event) => {
    if (!start) return;
    const zone = rectFrom(start, point(event));
    start = null;
    if (zone.width < MIN_ZONE || zone.height < MIN_ZONE) {
      live.hidden = true;
      toast(
        "El recuadro es demasiado pequeño: arrastra una zona de al menos el 5% del arte.",
        "error",
      );
      return;
    }
    await saveProductZone(projectId, zone);
  });
  stage.addEventListener("pointercancel", () => {
    start = null;
    live.hidden = true;
  });

  query("#zone-reset")?.addEventListener("click", () => saveProductZone(projectId, null));
  query("#zone-copy-all")?.addEventListener("click", async () => {
    const zone = state.campaign.find((item) => item.project_id === projectId)?.product_zone;
    if (!zone) return;
    const otros = state.campaign.filter((item) => item.project_id !== projectId);
    busy("Aplicando la zona", "Copiándola al resto de KV…", 10);
    try {
      for (let index = 0; index < otros.length; index += 1) {
        busyProgress(
          10 + Math.round((index / Math.max(1, otros.length)) * 85),
          otros[index].name,
        );
        await put("/projects/" + otros[index].project_id + "/product-zone", { zone });
        await refreshProject(otros[index].project_id);
      }
      toast("La zona se aplicó a " + String(otros.length) + " KV más.", "success");
      await renderProducts();
    } catch (error) {
      toast(errorMessage(error), "error");
    } finally {
      idle();
    }
  });
}

/* ------------------------------------------- copy que cambia por producto
   La fila de artes de una promoción es el mismo KV ocho veces con otro producto
   y, sobre todo, con otro nombre y otro precio. Aquí se elige qué elementos del
   arte cambian de una salida a otra y qué dice cada uno en cada producto. */

const COMBO_PREFIX = "combo::";

/** KV sobre el que se escriben los textos por producto.
 *
 *  Deliberadamente **no** es el KV activo. Los ids de capa son por pieza, así
 *  que si la referencia cambiara al cambiar de KV activo en el paso 2, la tabla
 *  ya escrita se quedaba sin coincidencias: los campos aparecían vacíos y los
 *  textos se descartaban en silencio al generar. Se ancla a una pieza y de ahí
 *  se traduce al resto. */
function copyReference(): Project | null {
  const stored = state.campaign.find((item) => item.project_id === state.copySource);
  if (stored) return stored;
  // La referencia guardada ya no está en la campaña: lo escrito para ella no
  // significa nada en las piezas nuevas.
  if (state.copySource) {
    state.copySource = null;
    state.copyFields.clear();
    state.productTexts = {};
  }
  const fallback = activeProject();
  if (fallback) state.copySource = fallback.project_id;
  return fallback;
}

function copyTargets(): Array<{ key: string; label: string }> {
  return [
    ...selectedProductFiles().map((file) => ({ key: productKey(file), label: productName(file) })),
    ...validGroups().map((group) => ({ key: COMBO_PREFIX + group.id, label: group.name })),
  ];
}

function copyValue(key: string, layerId: string, fallback: string): string {
  const stored = state.productTexts[key];
  const value = stored ? stored[layerId] : undefined;
  return value === undefined ? fallback : value;
}

function setCopyValue(key: string, layerId: string, value: string): void {
  const stored = state.productTexts[key] || (state.productTexts[key] = {});
  stored[layerId] = value;
}

/* Un nombre de capa de Photoshop no dice nada: «Decoración 2», «logo marci
   copia». Para elegir qué copy cambia por producto hay que ver el elemento, así
   que cada opción enseña sus píxeles y un mapa señala dónde cae en el arte.
   Las dos cosas salen de datos que el cliente ya tiene: el PNG de la capa y su
   geometría. Ninguna pide un render nuevo al servidor. */

function copyPick(project: Project, item: ArtTextLayer, index: number): string {
  const marca = state.copyFields.has(item.id);
  const vista = item.src
    ? '<img class="copy-pick-art" src="' + attr(fileUrl(project.project_id, item.src)) +
      '" alt="" loading="lazy" decoding="async">'
    : '<span class="copy-pick-art is-text">' + esc((item.text || "◇").slice(0, 18)) + "</span>";
  const texto = (item.text || "").trim();
  return [
    '<label class="copy-pick', marca ? " is-on" : "", '">',
    '<span class="copy-pick-num">', String(index + 1), "</span>",
    '<input class="copy-field" type="checkbox" value="', attr(item.id), '"', checked(marca), ">",
    vista,
    '<span class="copy-pick-copy"><strong>', esc(item.name), "</strong><small>",
    texto ? esc(texto.slice(0, 40)) : esc(CATEGORY_LABELS[item.category] || item.category),
    "</small></span></label>",
  ].join("");
}

/** El arte con un recuadro numerado por elemento: responde “dónde está esto”. */
function copyLocator(project: Project, items: ArtTextLayer[]): string {
  const canvas = project.canvas;
  const cajas = items.map((item, index) => {
    const layer = project.layers.find((entry) => entry.id === item.id);
    if (!layer) return "";
    const style = [
      "left:" + ((layer.x / Math.max(1, canvas.width)) * 100).toFixed(2) + "%",
      "top:" + ((layer.y / Math.max(1, canvas.height)) * 100).toFixed(2) + "%",
      "width:" + ((layer.width / Math.max(1, canvas.width)) * 100).toFixed(2) + "%",
      "height:" + ((layer.height / Math.max(1, canvas.height)) * 100).toFixed(2) + "%",
    ].join(";");
    return [
      '<span class="copy-map-box', state.copyFields.has(item.id) ? " is-on" : "",
      '" style="', attr(style), '" data-layer="', attr(item.id), '">',
      '<i>', String(index + 1), "</i></span>",
    ].join("");
  }).join("");
  return [
    '<div class="copy-map"><img src="', attr(thumbnailUrl(project.project_id, 520)),
    '" alt="Arte de ', attr(project.name), '" loading="lazy" decoding="async">',
    cajas, "</div>",
  ].join("");
}

function productCopyHtml(project: Project): string {
  const texts = state.texts[project.project_id] || EMPTY_TEXTS;
  const items = texts.layers.filter((item) => item.editable);
  const others = state.campaign.length - 1;
  const targets = copyTargets();
  if (!items.length) {
    return [
      '<section class="card"><div class="card-head"><div><h2>Textos por producto</h2>',
      "<p>Un precio y un nombre distintos en cada arte</p></div></div>",
      '<div class="notice">Este KV no tiene copy separado que se pueda reescribir. ',
      'Revísalo en el <strong>paso 2</strong>, en “Textos y logos del arte”.</div></section>',
    ].join("");
  }

  const chooser = items.map((item, index) => copyPick(project, item, index)).join("");

  const chosen = items.filter((item) => state.copyFields.has(item.id));
  let table = "";
  if (chosen.length && targets.length) {
    const head = chosen.map((item) => [
      "<th><span class=\"copy-col\">",
      item.src
        ? '<img src="' + attr(fileUrl(project.project_id, item.src)) + '" alt="" loading="lazy">'
        : "",
      "<span>", String(items.indexOf(item) + 1), ". ", esc(item.name), "</span></span></th>",
    ].join("")).join("");
    const rows = targets.map((target) => [
      "<tr><td><strong>", esc(target.label), "</strong></td>",
      chosen.map((item) => [
        '<td><input class="copy-cell" data-target="', attr(target.key), '" data-layer="', attr(item.id),
        '" value="', attr(copyValue(target.key, item.id, item.text)), '"></td>',
      ].join("")).join(""),
      "</tr>",
    ].join("")).join("");
    table = [
      '<div class="inventory" style="margin-top:16px"><table><thead><tr><th>Producto</th>',
      head, "</tr></thead><tbody>", rows, "</tbody></table></div>",
      '<p class="muted tiny" style="margin-top:10px">Un campo vacío deja el texto original del KV. ',
      "Cada arte se genera con su propia fila, así que el precio de uno no se queda en el siguiente.</p>",
    ].join("");
  } else if (chosen.length) {
    table = '<div class="notice" style="margin-top:16px">Marca arriba los productos que llevan arte individual para escribir su texto.</div>';
  }

  return [
    '<section class="card"><div class="card-head"><div><h2>Textos por producto · opcional</h2>',
    "<p>Qué parte del copy cambia en cada arte</p></div><span class=\"badge",
    state.copyFields.size ? " green" : "", '">', String(state.copyFields.size), " DE ",
    String(items.length), "</span></div>",
    // Quien salta directo aquí no ha visto el aviso del paso 2.
    !texts.brand_font && state.copyFields.size
      ? '<div class="notice warning" style="margin-bottom:14px">Este KV no tiene tipografía de marca: ' +
        "el copy saldrá con las letras del sistema. Súbela en el <strong>paso 2</strong>, " +
        "en “Textos y logos del arte”.</div>"
      : "",
    others > 0
      ? '<p class="muted tiny" style="margin-bottom:12px">Los textos se escriben sobre <strong>' +
        esc(project.name) + "</strong> y se traducen a los otros " + String(others) +
        " KV por categoría y posición: las piezas de un mismo PSD repiten el diseño.</p>"
      : "",
    '<span class="label">Elementos que cambian de un producto a otro</span>',
    '<p class="muted tiny" style="margin:6px 0 12px">Los nombres los pone Photoshop y no siempre ',
    "dicen qué son. Cada opción enseña sus píxeles, y el número la sitúa en el arte de la derecha.</p>",
    '<div class="copy-choose"><div class="copy-picks">', chooser, "</div>",
    copyLocator(project, items), "</div>",
    table,
    "</section>",
  ].join("");
}

function bindProductCopy(): void {
  queryAll<HTMLInputElement>(".copy-field").forEach((box) => {
    box.addEventListener("change", async () => {
      if (box.checked) state.copyFields.add(box.value);
      else state.copyFields.delete(box.value);
      await renderProducts();
    });
  });
  queryAll<HTMLInputElement>(".copy-cell").forEach((field) => {
    field.addEventListener("input", () => {
      setCopyValue(field.dataset.target!, field.dataset.layer!, field.value);
    });
  });
}

/** Copy de esta tanda, ya traducido a las capas del KV que se va a producir. */
function textOverrides(project: Project, targetKey: string): Array<Record<string, string>> | null {
  const source = copyReference();
  if (!source || !state.copyFields.size) return null;
  const overrides: Array<Record<string, string>> = [];
  state.copyFields.forEach((layerId) => {
    const value = (state.productTexts[targetKey] || {})[layerId];
    if (value === undefined || !value.trim()) return;
    const twin = twinLayerId(source, project, layerId);
    if (twin) overrides.push({ layer_id: twin, content: value.trim() });
  });
  return overrides;
}

/** Resuelve las columnas comunes de una matriz contra las categorías del PSD. */
function matrixTextOverrides(project: Project, file: File): Array<Record<string, string>> {
  const row = state.productionMatrix.find((item) => matrixFileMatches(item, file));
  if (!row) return [];
  const values: Record<string, string> = { headline: row.headline, price: row.price, cta: row.cta };
  return project.layers.filter((layer) => Boolean(values[layer.category]))
    .map((layer) => ({ layer_id: layer.id, content: values[layer.category] }));
}

function matrixFileMatches(row: MatrixRow, file: File): boolean {
  return matrixFilenameMatches(row, file.name);
}

function matrixProductFile(row: MatrixRow): File | undefined {
  return state.products.find((file) => matrixFileMatches(row, file));
}

/* ---------------------------------------------------------------- paso 3
   Productos: qué se pone, si va individual o en combinación, y en qué posición.
   El paso 4 solo decide el cómo (modelo, contexto, formatos). */
async function renderProducts(): Promise<void> {
  if (state.campaignWorkspace) {
    renderTemplateProposals();
    return;
  }
  const reference = copyReference();
  const zoneKv = zoneProject();
  if (reference) await loadTexts(reference.project_id);
  const missingTargets = state.campaign.filter((project) => !productTarget(project));
  const pendingReviews = state.campaign.filter((project) => !layersConfirmed(project));
  const productCards = state.products.map(productCard).join("");
  const groups = state.groups.map(groupCard).join("");
  const productChoices = state.products.map((file) =>
    '<label class="choice"><input class="group-member" type="checkbox" value="' + attr(productKey(file)) + '"> ' + esc(productName(file)) + "</label>"
  ).join("");
  const individuals = selectedProductFiles().length;

  const targetsNotice = missingTargets.length
    ? [
        '<div class="notice error" style="margin-bottom:16px">',
        "<strong>Falta identificar el producto en ", String(missingTargets.length), " de ",
        String(state.campaign.length), " KV.</strong> Sin una capa marcada como <em>Producto</em> ",
        "el sistema no sabe qué pieza retirar. Puedes continuar: al generar, esos KV se omitirán.",
        '<div style="margin:10px 0">',
        missingTargets.map((project) => '<span class="badge red" style="margin:0 6px 6px 0">' + esc(project.name) + "</span>").join(""),
        "</div>",
        '<p class="tiny" style="margin-bottom:10px">Dos formas de arreglarlo: detectarlo con IA aquí, o volver al ',
        '<strong>paso 2</strong> y marcar la capa correcta como “Producto original · eliminar y reemplazar”.</p>',
        '<div class="button-row">',
        '<button class="button" id="detect-all-products">Detectar producto en los ',
        String(missingTargets.length), " KV que faltan</button>",
        '<button class="ghost-button" id="back-to-layers">Ir al paso 2 a marcarlo</button>',
        "</div>",
        missingTargets.length > 1
          ? '<details style="margin-top:12px"><summary>Detectar en uno solo</summary><div class="button-row" style="margin-top:10px">' +
            missingTargets.map((project) => '<button class="ghost-button detect-product" data-project="' + attr(project.project_id) + '">' + esc(project.name) + "</button>").join("") +
            "</div></details>"
          : "",
        "</div>",
      ].join("")
    : '<div class="notice success" style="margin-bottom:16px">Los ' + String(state.campaign.length) + ' KV tienen su producto identificado: se retirará el original y entrará el nuevo.</div>';

  content().innerHTML = [
    stepBar("products"),
    pageHead(
      "03 · PLANTILLA",
      "Convierte el PSD en una plantilla reutilizable",
      "Confirma qué queda fijo y qué cambia. Luego usa la misma base para todos los productos y formatos.",
    ),
    '<section class="template-callout"><div><span class="kicker">BASE APROBABLE</span><h2>Guardar plantilla de ', esc(state.campaignBrief.name || reference?.name || "esta campaña"), '</h2><p>Los campos detectados —producto, precio, titular y CTA— quedan listos para alimentar la matriz.</p></div><button class="button" id="save-template"', reference ? "" : " disabled", '>Guardar como plantilla</button></section>',
    targetsNotice,
    pendingReviews.length
      ? '<div class="notice warning" style="margin-bottom:16px">Hay ' + String(pendingReviews.length) +
        ' KV con capas sin confirmar. Esto ya no bloquea la generación; puedes revisarlos después si lo necesitas.</div>'
      : "",
    '<section class="card elevated"><div class="card-head"><div><h2>Catálogo de productos</h2><p>PNG con transparencia da el mejor recorte</p></div><span class="badge green">',
    String(state.products.length), " CARGADOS</span></div>",
    '<label class="dropzone compact"><input id="product-files" type="file" multiple accept=".png,.jpg,.jpeg,.webp,.tif,.tiff,.avif"><span class="drop-icon">⇧</span><strong>Sube los productos</strong><span>PNG, JPG, WEBP, TIFF o AVIF · puedes elegir varios a la vez</span></label>',
    productCards
      ? '<div class="product-grid" style="margin-top:16px">' + productCards + "</div>"
      : '<div class="notice" style="margin-top:16px">Aún no has cargado productos.</div>',
    "</section>",

    '<div class="spacer"></div>',
    zoneKv ? productZoneHtml(zoneKv) : "",

    '<div class="spacer"></div>',
    '<section class="card"><div class="card-head"><div><h2>Combos opcionales</h2><p>Solo si varios productos deben aparecer juntos en un mismo arte</p></div></div>',
    '<div class="notice" style="margin-bottom:14px"><strong>Resumen:</strong> ',
    String(individuals), " producto(s) con arte individual y ", String(validGroups().length), " combo(s).</div>",
    state.products.length >= 2 ? [
      '<div class="stack"><label class="field"><span>Nombre de la combinación</span><input id="group-name" placeholder="Ej. Combo familiar"></label>',
      '<div><span class="label">Productos que van juntos</span><div class="choice-row" style="margin-top:8px">', productChoices, "</div></div>",
      '<label class="field"><span>Posición de estos productos</span><select id="group-arrangement">',
      optionList(ARRANGEMENT_OPTIONS, "auto"), "</select></label>",
      '<button class="button" id="add-group">Añadir combinación</button></div>',
    ].join("") : '<div class="notice">Carga al menos dos productos para poder combinarlos.</div>',
    groups ? '<div class="stack" style="margin-top:16px">' + groups + "</div>" : "",
    "</section>",

    '<div class="spacer"></div>',
    reference ? productCopyHtml(reference) : "",

    stepFooter("products", "Elegir modelo y generar"),
  ].join("");

  bindStepBar();
  bindStepFooter("products");
  bindProductStep();
  bindProductZone();
  bindProductCopy();
  query("#save-template")?.addEventListener("click", () => void saveCampaignTemplate(reference));
}

function templateWireframe(candidate: TemplateCandidate): string {
  const fields = candidate.fields.slice(0, 7);
  const product = fields.find((field) => field.kind === "image");
  const text = fields.filter((field) => field.kind === "text");
  return [
    '<div class="template-wireframe" aria-label="Estructura de ', attr(candidate.name), '">',
    '<span class="wire-brand">MARCA</span>',
    product ? '<span class="wire-product">PRODUCTO</span>' : '',
    text.map((field, index) => '<span class="wire-text wire-' + String(index + 1) + '">' + esc(field.label) + '</span>').join(""),
    '<span class="wire-safe">Área adaptable</span></div>',
  ].join("");
}

/** La aprobación no debe sentirse como una caja negra: la persona puede ver
 * qué documentos, PSD o artes se usaron como evidencia de cada sistema. La
 * miniatura de arriba representa la retícula propuesta; nunca se presenta como
 * una copia literal ni como un producto ya colocado. */
function templateEvidenceHtml(candidate: TemplateCandidate): string {
  const sourceIds = candidate.source_ids || [];
  const sources = sourceIds
    .map((sourceId) => state.campaignSources.find((source) => source.source_id === sourceId))
    .filter((source): source is CampaignSource => Boolean(source));
  if (!sources.length) {
    return '<div class="proposal-evidence muted tiny">Estructura propuesta con el brief y el material disponible de la campaña.</div>';
  }
  const labels = sources.slice(0, 4).map((source) =>
    '<span class="field-pill">' + esc(sourceKindLabel(source)) + ' · ' + esc(source.filename) + '</span>'
  ).join("");
  const remaining = sources.length - Math.min(sources.length, 4);
  return '<div class="proposal-evidence"><strong>Material analizado</strong><div class="field-pills">' +
    labels + (remaining ? '<span class="field-pill">+' + String(remaining) + ' archivo' + (remaining === 1 ? '' : 's') + '</span>' : '') +
    '</div><small>La vista previa enseña la estructura de la plantilla, no una copia literal ni un producto definitivo.</small></div>';
}

function renderTemplateProposals(): void {
  const candidates = state.templateCandidates;
  if (!candidates.length) {
    content().innerHTML = [stepBar("products"), pageHead("03 · PLANTILLAS", "Todavía no hay propuestas", "Genera primero el brief de campaña para que la IA decida qué sistemas necesita."), emptyState("◇", "Sin plantillas", "El análisis debe producir entre 3 y 5 propuestas sin productos.", '<button class="button" id="go-intelligence">Volver al brief</button>')].join("");
    bindStepBar();
    query("#go-intelligence")?.addEventListener("click", () => navigate("layers"));
    return;
  }
  const approved = candidates.filter((item) => item.status === "approved").length;
  const cards = candidates.map((candidate, index) => {
    const fields = candidate.fields.map((field) => '<span class="field-pill ' + (field.required ? "required" : "") + '">' + esc(field.label) + (field.required ? " *" : "") + '</span>').join("");
    const preview = candidate.preview_urls?.portrait || candidate.preview_url || candidate.preview;
    const aspects: Array<[string, string]> = [
      ["portrait", "4:5"], ["square", "1:1"], ["story", "9:16"], ["landscape", "Horizontal"],
    ];
    const aspectButtons = candidate.preview_urls
      ? '<div class="preview-aspects">' + aspects.filter(([key]) => candidate.preview_urls?.[key]).map(([key, label], aspectIndex) =>
          '<button type="button" class="preview-aspect' + (aspectIndex === 0 ? ' is-active' : '') +
          '" data-target="preview-' + attr(candidate.candidate_id) + '" data-src="' +
          attr(candidate.preview_urls?.[key] || "") + '">' + esc(label) + '</button>'
        ).join("") + '</div>'
      : '';
    return [
      '<article class="proposal-card ', candidate.status, '"><div class="proposal-preview">',
      preview ? '<img id="preview-' + attr(candidate.candidate_id) + '" src="' + attr(preview) + '" alt="Vista previa de ' + attr(candidate.name) + '" loading="lazy">' : templateWireframe(candidate),
      aspectButtons,
      '<span class="proposal-index">', String(index + 1).padStart(2, "0"), '</span><span class="proposal-status">',
      candidate.status === "approved" ? "✓ Aprobada" : candidate.status === "rejected" ? "Descartada" : "Propuesta", '</span></div>',
      '<div class="proposal-body"><span class="kicker">', esc(candidate.category || "Plantilla estática"), '</span><h2>', esc(candidate.name), '</h2><p>', esc(candidate.description), '</p>',
      candidate.rationale ? '<div class="proposal-reason"><strong>Por qué funciona</strong><span>' + esc(candidate.rationale) + '</span></div>' : '',
      templateEvidenceHtml(candidate),
      '<div class="proposal-meta"><span>', String(candidate.supported_product_count?.min ?? 1), '–', String(candidate.supported_product_count?.max ?? 1), ' productos</span><span>Adaptable por formato</span></div>',
      candidate.blueprint ? '<div class="field-pills blueprint-pills"><span class="field-pill">' + esc(candidate.blueprint.archetype || "retícula adaptable") + '</span><span class="field-pill">' + esc(candidate.blueprint.background_style || "fondo de campaña") + '</span><span class="field-pill">' + esc(candidate.blueprint.accent_style || "sistema visual") + '</span></div>' : '',
      '<div class="field-pills">', fields || '<span class="muted tiny">Los campos se definirán al materializar la plantilla.</span>', '</div>',
      candidate.warnings.length ? '<div class="notice warning compact">' + esc(candidate.warnings.join(" · ")) + '</div>' : '',
      '<label class="field proposal-note"><span>Corrección para la IA · opcional</span><input data-note="', attr(candidate.candidate_id), '" value="', attr(candidate.decision_notes || ""), '" placeholder="Ej. más aire para el precio"></label>',
      '<div class="button-row"><button class="ghost-button revise-candidate" data-id="', attr(candidate.candidate_id), '">Aplicar corrección y redibujar</button>',
      '<button class="button approve-candidate" data-id="', attr(candidate.candidate_id), '"', candidate.status === "approved" ? " disabled" : "", '>✓ Aprobar plantilla</button>',
      '<button class="ghost-button reject-candidate" data-id="', attr(candidate.candidate_id), '"', candidate.status === "rejected" ? " disabled" : "", '>Descartar</button></div></div></article>',
    ].join("");
  }).join("");
  content().innerHTML = [
    stepBar("products"),
    pageHead("03 · PLANTILLAS SIN PRODUCTOS", "Aprueba el sistema antes de producir", "La IA dejó espacios inteligentes para los campos del brief. Los productos reales entran después, exclusivamente desde la matriz."),
    '<section class="approval-summary"><div><strong>', String(approved), ' de ', String(candidates.length), ' aprobadas</strong><span>Necesitas al menos una para habilitar producción.</span></div><div class="approval-legend"><span><i class="required-dot"></i> obligatorio según brief</span><span>Los campos opcionales desaparecen sin dejar huecos</span></div></section>',
    '<div class="proposal-grid">', cards, '</div>',
    approved ? stepFooter("products", "Abrir matriz de producción") : '<div class="notice warning" style="margin-top:18px">Revisa las propuestas y aprueba al menos una. La matriz no puede producir con plantillas sin aprobar.</div>',
  ].join("");
  bindStepBar();
  if (approved) bindStepFooter("products");
  queryAll<HTMLButtonElement>(".preview-aspect").forEach((button) => {
    button.addEventListener("click", () => {
      const image = document.getElementById(button.dataset.target || "") as HTMLImageElement | null;
      if (!image || !button.dataset.src) return;
      image.src = button.dataset.src;
      button.parentElement?.querySelectorAll(".preview-aspect").forEach((item) =>
        item.classList.toggle("is-active", item === button)
      );
    });
  });
  queryAll<HTMLButtonElement>(".revise-candidate").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!state.activeClientId || !state.campaignWorkspace) return;
      const id = button.dataset.id || "";
      const note = query<HTMLInputElement>('[data-note="' + CSS.escape(id) + '"]')?.value.trim() || "";
      if (!note) {
        toast("Escribe qué debe cambiar antes de pedir una nueva propuesta.", "error");
        return;
      }
      busy("Corrigiendo plantilla", "La IA está reinterpretando la retícula y sus adaptaciones…", 42);
      try {
        const regenerated = await reviseTemplateCandidate(
          state.activeClientId,
          state.campaignWorkspace.campaign_id,
          id,
          note,
        );
        state.campaignIntelligence = regenerated.brief;
        state.templateCandidates = regenerated.template_candidates;
        state.productionMatrixPlans = [];
        saveSession();
        regenerated.warnings.forEach((warning) => toast(warning, "info"));
        toast("Corrección aplicada. Revisa el nuevo preview antes de aprobar.", "success");
        renderTemplateProposals();
      } catch (error) {
        toast(errorMessage(error), "error");
      } finally {
        idle();
      }
    });
  });
  queryAll<HTMLButtonElement>(".approve-candidate, .reject-candidate").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!state.activeClientId || !state.campaignWorkspace) return;
      const id = button.dataset.id || "";
      const decision = button.classList.contains("approve-candidate") ? "approve" : "reject";
      const note = query<HTMLInputElement>('[data-note="' + CSS.escape(id) + '"]')?.value.trim() || "";
      busy(decision === "approve" ? "Aprobando plantilla" : "Descartando propuesta", "Guardando la decisión en la memoria del cliente…", 45);
      try {
        const updated = await decideTemplateCandidate(state.activeClientId, state.campaignWorkspace.campaign_id, id, decision, note);
        if (!updated) throw new Error("Este despliegue todavía no puede guardar la aprobación de plantillas.");
        state.templateCandidates = state.templateCandidates.map((item) => item.candidate_id === id ? updated : item);
        state.productionMatrixPlans = [];
        saveSession();
        toast(decision === "approve" ? "Plantilla aprobada y guardada en el cliente." : "Propuesta descartada.", "success");
        renderTemplateProposals();
      } catch (error) { toast(errorMessage(error), "error"); } finally { idle(); }
    });
  });
}

/** La biblioteca permanente vive en el backend, no en la sesión efímera del PSD. */
async function saveCampaignTemplate(reference: Project | null): Promise<void> {
  if (!reference) return;
  const brandName = state.campaignBrief.client || "Marca sin nombre";
  busy("Guardando plantilla", "Convirtiendo capas del PSD en campos editables…", 35);
  try {
    const listed = await get<any>("/brands");
    const brands = listed.brands || [];
    let brand = brands.find((item: any) => String(item.name).toLowerCase() === brandName.toLowerCase());
    if (!brand) brand = (await post<any>("/brands", { name: brandName })).brand;
    await post("/brands/" + brand.brand_id + "/templates/from-project", {
      project_id: reference.project_id,
      name: state.campaignBrief.name || reference.name,
      erase_slots_from_plate: true,
      classify_with_vision: true,
    });
    toast("Plantilla guardada en la biblioteca de " + brandName + ".", "success");
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    idle();
  }
}

function bindProductStep(): void {
  query<HTMLInputElement>("#product-files")?.addEventListener("change", async (event) => {
    const files = Array.from((event.currentTarget as HTMLInputElement).files || []);
    const existing = new Set(state.products.map(productKey));
    state.products = mergeUniqueFiles(state.products, files);
    // Los productos nuevos se marcan por defecto, sin alterar los anteriores ni
    // las combinaciones que el usuario ya había preparado.
    files.forEach((file) => {
      if (!existing.has(productKey(file))) state.individualProducts.add(productKey(file));
    });
    await renderProducts();
  });
  queryAll<HTMLInputElement>(".individual-product").forEach((checkBox) => {
    checkBox.addEventListener("change", async () => {
      if (checkBox.checked) state.individualProducts.add(checkBox.value);
      else state.individualProducts.delete(checkBox.value);
      await renderProducts();
    });
  });
  query("#add-group")?.addEventListener("click", async () => {
    const members = queryAll<HTMLInputElement>(".group-member:checked").map((item) => item.value);
    if (members.length < 2) {
      toast("Elige al menos dos productos para la combinación.", "error");
      return;
    }
    const name = query<HTMLInputElement>("#group-name")!.value.trim() || "Combinación " + String(state.groups.length + 1);
    state.groups.push({
      id: "group-" + Date.now().toString(36),
      name,
      members,
      arrangement: query<HTMLSelectElement>("#group-arrangement")!.value as ProductGroup["arrangement"],
    });
    // Un combo es una única salida con todos sus miembros. Si el usuario también
    // quiere piezas individuales puede volver a marcarlas explícitamente después.
    members.forEach((key) => state.individualProducts.delete(key));
    toast(
      "Combinación creada. Sus productos saldrán juntos; se desmarcaron las salidas individuales.",
      "success",
    );
    await renderProducts();
  });
  queryAll<HTMLButtonElement>(".remove-group").forEach((button) => {
    button.addEventListener("click", async () => {
      state.groups = state.groups.filter((group) => group.id !== button.dataset.group);
      await renderProducts();
    });
  });
  queryAll<HTMLButtonElement>(".detect-product").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = button.dataset.project!;
      busy("Detectando producto", "Separando el sujeto principal del fondo…", 15);
      try {
        const result = await post<any>("/projects/" + id + "/layers/detect-product", { force: false });
        await refreshProject(id);
        (result.warnings || []).forEach((warning: string) => toast(warning, "info"));
        if (result.detected) toast("Producto detectado y listo para reemplazar.", "success");
        else toast("No se pudo detectar el producto. Márcalo a mano en el paso 2.", "error");
        await renderProducts();
      } catch (error) {
        toast(errorMessage(error), "error");
      } finally {
        idle();
      }
    });
  });

  query("#back-to-layers")?.addEventListener("click", () => navigate("layers"));

  query("#detect-all-products")?.addEventListener("click", async () => {
    // Uno por uno pero en una sola acción: son varias piezas del mismo PSD y
    // pedirlo KV a KV era la parte tediosa.
    const pendientes = missingProductTargets();
    busy("Detectando el producto", "Separando el sujeto principal del fondo…", 4);
    const failed: string[] = [];
    try {
      for (let index = 0; index < pendientes.length; index += 1) {
        const project = pendientes[index];
        busyProgress(
          4 + Math.round((index / Math.max(1, pendientes.length)) * 92),
          project.name + " · " + String(index + 1) + " de " + String(pendientes.length),
        );
        try {
          const result = await post<any>(
            "/projects/" + project.project_id + "/layers/detect-product",
            { force: false },
          );
          await refreshProject(project.project_id);
          (result.warnings || []).forEach((warning: string) => toast(warning, "info"));
          if (!result.detected) failed.push(project.name);
        } catch {
          failed.push(project.name);
        }
      }
      const ok = pendientes.length - failed.length;
      if (ok) toast("Producto detectado en " + String(ok) + " KV.", "success");
      if (failed.length) {
        toast(
          "En " + failed.join(", ") + " no se pudo. Marca la capa a mano en el paso 2.",
          "error",
        );
      }
      await renderProducts();
    } finally {
      idle();
    }
  });
}

/* ---------------------------------------------------------------- paso 4
   Cómo se genera: formatos, modelo, contexto y el botón final. */

function matrixFormats(row: MatrixRow): string[] {
  const aliases: Record<string, string> = {
    feed: "meta_feed_4_5", feed_vertical: "meta_feed_4_5",
    post: "meta_feed_4_5", post_vertical: "meta_feed_4_5",
    instagram_feed: "meta_instagram_feed_3_4",
    cuadrado: "meta_feed_square", square: "meta_feed_square",
    story: "meta_stories", stories: "meta_stories",
    historia: "meta_stories", historias: "meta_stories",
    reel: "meta_reels", reels: "meta_reels",
    horizontal: "meta_feed_landscape", landscape: "meta_feed_landscape",
  };
  const formats = row.formats.split(/[|;]/).map((item) => item.trim()).filter(Boolean)
    .map((item) => aliases[matrixKey(item)] || item.toLowerCase().replace(/\s+/g, ""));
  return formats.filter((item, index) => formats.indexOf(item) === index);
}

/** Misma normalización que el motor: acentos, guiones y mayúsculas no pueden
 * hacer que una foto correctamente cargada parezca ausente en la interfaz. */
function productMatchKey(value: string): string {
  return value.normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

function matrixProductTokens(row: MatrixRow): string[] {
  return (row.image || row.product || "").split(/[|;]/)
    .map((item) => item.trim()).filter(Boolean);
}

function matrixFilenameMatches(row: MatrixRow, filename: string): boolean {
  return matrixProductTokens(row).some((token) => matrixTokenMatchesFilename(token, filename));
}

function matrixTokenMatchesFilename(token: string, filename: string): boolean {
  const full = productMatchKey(filename);
  const stem = productMatchKey(filename.replace(/\.[^.]+$/, ""));
  const key = productMatchKey(token);
  return key === full || key === stem ||
    (key.length >= 3 && (full.includes(key) || key.includes(full) || stem.includes(key) || key.includes(stem)));
}

function matrixRequestedPieces(): number {
  const defaultFormatCount = Math.max(1, state.selectedFormats.size);
  return state.productionMatrix.reduce(
    (total, row) => total + (matrixFormats(row).length || defaultFormatCount) * Math.max(1, row.proposals),
    0,
  );
}

function campaignProductAssets(): CampaignProductionAsset[] {
  return state.campaignWorkspace?.production_assets || [];
}

function matrixMatchedAssets(row: MatrixRow): CampaignProductionAsset[] {
  return campaignProductAssets().filter((asset) => matrixFilenameMatches(row, asset.filename));
}

function matrixAmbiguousTokens(row: MatrixRow): string[] {
  return matrixProductTokens(row).filter((token) =>
    campaignProductAssets().filter((asset) => matrixTokenMatchesFilename(token, asset.filename)).length > 1,
  );
}

function matrixRowReady(row: MatrixRow): boolean {
  return matrixMatchedAssets(row).length >= matrixExpectedProductCount(row) &&
    matrixAmbiguousTokens(row).length === 0;
}

function matrixPlanForRow(row: MatrixRow): MatrixProductionPlan | null {
  return state.productionMatrixPlans.find((plan) => plan.row_number === row.rowNumber) || null;
}

function matrixPlanReady(row: MatrixRow): boolean {
  // Una matriz guardada por una versión anterior no trae planes. No la
  // inutilizamos: el servidor volverá a aplicar el mismo preflight al generar.
  if (!state.productionMatrixPlans.length) return true;
  const plan = matrixPlanForRow(row);
  if (!plan || plan.status !== "ready") return false;
  // Si alguien cambió la aprobación después de validar la matriz, ese plan ya
  // no describe la biblioteca actual y debe revisarse en el servidor.
  return !plan.template || state.templateCandidates.some(
    (candidate) => candidate.candidate_id === plan.template?.candidate_id && candidate.status === "approved",
  );
}

/** Una respuesta parcial nunca puede parecer una validación correcta. El
 * servidor suele devolver un plan por fila, pero este guard evita que una
 * respuesta interrumpida habilite producción sin que la persona lo note. */
function matrixRowsNeedingPlan(rows = state.productionMatrix): MatrixRow[] {
  if (!state.productionMatrixPlans.length) return [];
  return rows.filter((row) => !matrixPlanReady(row));
}

function matrixPlanProblem(row: MatrixRow): string {
  const plan = matrixPlanForRow(row);
  return plan?.message ||
    "La fila " + String(row.rowNumber) +
    " no recibió una plantilla compatible. Vuelve a validar la matriz.";
}

function matrixPlanReviewHtml(rows: MatrixRow[]): string {
  if (!rows.length) return "";
  if (!state.productionMatrixPlans.length) {
    return '<div class="notice compact matrix-plan-review"><strong>Compatibilidad pendiente de confirmar:</strong> esta matriz se guardó antes de la revisión automática. El servidor la comprobará otra vez al generar.</div>';
  }
  const items = rows.slice(0, 12).map((row) => {
    const plan = matrixPlanForRow(row);
    if (!plan) {
      return '<li><strong>Fila ' + String(row.rowNumber) + ':</strong> no se recibió el plan de esta fila; vuelve a validar la matriz.</li>';
    }
    const status = matrixPlanReady(row)
      ? '✓ compatible'
      : plan.status === "needs_approval" ? 'requiere aprobación' : 'incompatible';
    const template = plan.template ? ' · ' + esc(plan.template.name) : '';
    const fillable = plan.ai_fillable_fields.length
      ? ' · IA puede completar: ' + esc(plan.ai_fillable_fields.join(", "))
      : '';
    return '<li class="matrix-plan-' + esc(plan.status) + '"><strong>Fila ' +
      String(row.rowNumber) + ' · ' + status + '</strong>' + template + fillable +
      '<small>' + esc(plan.message) + '</small></li>';
  }).join("");
  const remaining = rows.length - Math.min(rows.length, 12);
  const issues = matrixRowsNeedingPlan(rows).length;
  return '<section class="notice ' + (issues ? 'warning ' : 'success ') +
    'compact matrix-plan-review"><strong>Plantilla por fila</strong><span>' +
    (issues
      ? ' Corrige o aprueba una plantilla antes de producir.'
      : ' Todas las filas tienen una plantilla compatible.') +
    '</span><ul>' + items + (remaining ? '<li>… y ' + String(remaining) + ' filas más.</li>' : '') +
    '</ul></section>';
}

function matrixExpectedProductCount(row: MatrixRow): number {
  const products = row.product.split(/[|;]/).map((item) => item.trim()).filter(Boolean);
  const images = row.image.split(/[|;]/).map((item) => item.trim()).filter(Boolean);
  // Una fila institucional puede traer únicamente titular, fecha o legal. La
  // imagen se exige cuando la propia fila declara un producto/archivo, no por
  // defecto: el servidor elegirá una plantilla aprobada compatible con cero
  // productos.
  return Math.max(products.length, images.length);
}

/** No todos los formatos que el motor acepta se pueden dibujar en todos los
 * navegadores. TIFF es el caso habitual; para esos archivos mostramos una ficha
 * deliberada, no el icono de imagen rota del navegador. AVIF/GIF se intentan y
 * también caen en esta ficha si el navegador concreto no los soporta. */
const BROWSER_PRODUCT_PREVIEW_EXTENSIONS = new Set([
  "png", "jpg", "jpeg", "webp", "gif", "avif", "bmp",
]);

function productPreviewExtension(file: File | CampaignProductionAsset): string {
  const asset = "asset_id" in file;
  const declared = asset ? file.extension : "";
  const filename = asset ? file.filename : file.name;
  const inferred = filename.split(".").pop() || "";
  return (declared || inferred).replace(/^\./, "").trim().toLowerCase();
}

function matrixProductPreviewFallbackHtml(extension: string, filename: string): string {
  const format = (extension || "archivo").toUpperCase();
  return [
    '<span class="matrix-product-file-preview" role="img" aria-label="',
    attr("Vista previa no disponible para " + filename), '"><b>', esc(format),
    '</b><small>Sin miniatura</small></span>',
  ].join("");
}

function matrixProductPreviewHtml(file: File | CampaignProductionAsset): string {
  const asset = "asset_id" in file;
  const filename = asset ? file.filename : file.name;
  const extension = productPreviewExtension(file);
  if (!BROWSER_PRODUCT_PREVIEW_EXTENSIONS.has(extension)) {
    return matrixProductPreviewFallbackHtml(extension, filename);
  }
  const url = asset ? file.preview_url : productUrl(file);
  return [
    '<img class="matrix-product-preview" src="', attr(url), '" alt="', attr(filename),
    '" data-preview-extension="', attr(extension), '" data-preview-filename="', attr(filename),
    '" loading="lazy" decoding="async">',
  ].join("");
}

function matrixProductPreviewFallback(extension: string, filename: string): HTMLSpanElement {
  const preview = document.createElement("span");
  preview.className = "matrix-product-file-preview";
  preview.setAttribute("role", "img");
  preview.setAttribute("aria-label", "Vista previa no disponible para " + filename);
  const format = document.createElement("b");
  format.textContent = (extension || "archivo").toUpperCase();
  const detail = document.createElement("small");
  detail.textContent = "Sin miniatura";
  preview.append(format, detail);
  return preview;
}

function bindMatrixProductPreviewFallbacks(): void {
  queryAll<HTMLImageElement>(".matrix-product-preview").forEach((image) => {
    image.addEventListener("error", () => {
      image.replaceWith(matrixProductPreviewFallback(
        image.dataset.previewExtension || "",
        image.dataset.previewFilename || "imagen",
      ));
    }, { once: true });
  });
}

function matrixProductCard(file: File | CampaignProductionAsset): string {
  const asset = "asset_id" in file;
  const filename = asset ? file.filename : file.name;
  const linked = state.productionMatrix.filter((row) => matrixFilenameMatches(row, filename));
  const label = linked.length
    ? "Vinculada a " + linked.slice(0, 2).map((row) => row.product || row.image).join(" · ") +
      (linked.length > 2 ? " y " + String(linked.length - 2) + " más" : "")
    : state.productionMatrix.length
      ? "No coincide con una fila todavía"
      : "Esperando la matriz para vincularse";
  return [
    '<article class="matrix-product-card">', matrixProductPreviewHtml(file),
    '<div><strong>', esc(filename), '</strong><span class="', linked.length ? 'is-linked' : 'is-unlinked', '">',
    esc(label), '</span><small>', asset
      ? readableSize(file.size_bytes) + " · " + String(file.width) + "×" + String(file.height)
      : readableSize(file.size), '</small></div>',
    '<button type="button" class="matrix-product-remove" ', asset
      ? 'data-asset-id="' + attr(file.asset_id) + '"'
      : 'data-product-key="' + attr(productKey(file)) + '"',
    ' aria-label="Quitar ', attr(filename), '" title="Quitar imagen">×</button></article>',
  ].join("");
}

function renderCampaignProduction(): void {
  const rows = state.productionMatrix;
  const matched = rows.filter(
    (row) => matrixRowReady(row) && matrixPlanReady(row),
  ).length;
  const approved = state.templateCandidates.filter((item) => item.status === "approved");
  const exact = matrixRequestedPieces();
  const needsDefaultFormat = rows.some((row) => matrixFormats(row).length === 0);
  const planIssueRows = matrixRowsNeedingPlan(rows);
  state.autoFormats = false;
  content().innerHTML = [
    stepBar("generate"),
    pageHead(
      "04 · PRODUCCIÓN AUTOMÁTICA",
      "La matriz decide el contenido; la IA elige la plantilla",
      "Cada fila puede pedir su propio formato, cantidad y combinación. Los campos vacíos se completan solo si el brief lo permite; “no poner” los elimina y recompone el diseño.",
    ),
    '<section class="production-command"><div><span class="kicker">ORDEN DE PRODUCCIÓN</span><h2>',
    rows.length ? String(exact) + " artes planificados" : "Sube la matriz para calcular la tanda",
    '</h2><p>', String(approved.length), ' plantillas aprobadas disponibles para selección automática.</p></div>',
    '<div class="production-command-stats"><span><strong>', String(rows.length), '</strong> filas</span><span><strong>',
    String(matched), '</strong> filas listas</span><span><strong>', String(exact), '</strong> salidas</span></div></section>',
    productionMatrixHtml(),
    '<div class="spacer"></div>',
    '<div class="production-format-note"><strong>Formatos por defecto</strong><span>Solo se usan cuando una fila deja “formatos” vacío. La medida escrita en la matriz siempre manda.</span></div>',
    formatSelectorHtml(false),
    '<div class="spacer"></div>',
    '<section class="card production-review"><div><div class="card-head"><div><h2>Revisión antes de producir</h2>',
    '<p>No se hace una llamada de imagen por pieza. El producto se recorta, mejora y compone localmente; el copy faltante usa como máximo una llamada de IA para toda la tanda.</p></div></div>',
    '<label class="choice ai-copy-choice"><input id="campaign-ai-copy" type="checkbox" checked> Completar con IA titular, subtítulo o CTA cuando la celda esté vacía y la plantilla lo admita</label>',
    '<div class="notice compact"><strong>Nunca se inventan:</strong> precio, descuento, cuota ni vigencia. Los legales solo se completan desde el brief que aprobaste; si no hay copy, el campo desaparece.</div></div>',
    '<button class="button large" id="run-campaign-production"',
    (!state.productionMatrixFile && !state.productionMatrixDraftId) || !rows.length || matched < rows.length || planIssueRows.length || !approved.length ||
      (needsDefaultFormat && !state.selectedFormats.size) ? " disabled" : "",
    '>Generar ', String(exact || 0), ' artes <span>→</span></button></section>',
    state.productionBatch
      ? '<div class="notice success" style="margin-top:18px"><strong>Última tanda:</strong> ' +
        String(state.productionBatch.total_pieces) + ' artes listos. <button class="ghost-button" id="open-last-batch">Ver entregables</button></div>'
      : '',
    state.productionTask && !["COMPLETED", "FAILED"].includes(state.productionTask.state)
      ? '<div class="notice compact" style="margin-top:12px"><strong>Tanda en curso:</strong> ' +
        esc(state.productionTask.meta.status || "El worker sigue preparando los artes.") +
        ' <button class="ghost-button" id="resume-production-task">Ver progreso</button></div>'
      : '',
  ].join("");
  bindStepBar();
  bindProductionMatrix();
  bindFormatSelector();
  query("#open-last-batch")?.addEventListener("click", () => navigate("results"));
  query("#resume-production-task")?.addEventListener("click", () => void resumeCampaignProductionTask());
  query("#run-campaign-production")?.addEventListener("click", () => void runCampaignProduction());
}

async function runCampaignProduction(): Promise<void> {
  if (!state.activeClientId || !state.campaignWorkspace ||
    (!state.productionMatrixFile && !state.productionMatrixDraftId)) return;
  const planIssueRows = matrixRowsNeedingPlan();
  if (planIssueRows.length) {
    toast(matrixPlanProblem(planIssueRows[0]), "error");
    return;
  }
  const incomplete = state.productionMatrix.filter((row) => !matrixRowReady(row));
  if (incomplete.length) {
    const ambiguous = incomplete.flatMap(matrixAmbiguousTokens);
    toast(
      ambiguous.length
        ? "Hay nombres ambiguos: " + Array.from(new Set(ambiguous)).join(", ") + ". Escribe el archivo exacto en la columna imagen."
        : "Falta una imagen por cada producto indicado en la matriz, incluidos los combos.",
      "error",
    );
    return;
  }
  const useAi = query<HTMLInputElement>("#campaign-ai-copy")?.checked !== false;
  busy("Produciendo la campaña", "Subiendo matriz y productos…", 5);
  try {
    const started = await produceCampaign(
      state.activeClientId,
      state.campaignWorkspace.campaign_id,
      state.productionMatrixFile,
      state.productionMatrixDraftId,
      [],
      campaignProductAssets().map((asset) => asset.asset_id),
      Array.from(state.selectedFormats),
      useAi,
      (sent, total) => busyProgress(
        total ? 5 + Math.round((sent / total) * 50) : 25,
        total ? "Subiendo " + readableSize(sent) + " de " + readableSize(total) : "Subiendo archivos…",
      ),
      () => busyProgress(62, "Eligiendo plantillas y adaptando todos los formatos…"),
    );
    let batch: ProductionBatch;
    if ("task_id" in started) {
      // La API ya dejó matriz y fotos a salvo en el servidor. A partir de aquí
      // cerrar o recargar la pestaña no duplica la tanda: se puede retomar con
      // su task_id y consultar los entregables al volver.
      state.productionTask = started;
      saveSession();
      busyProgress(
        Math.max(6, Number(started.meta.progress || 6)),
        started.meta.status || "La tanda quedó en cola; el worker la está preparando…",
      );
      batch = await pollCampaignProductionTask(
        state.activeClientId,
        state.campaignWorkspace.campaign_id,
        started.task_id,
        busyProgress,
      );
      state.productionTask = null;
    } else {
      batch = started;
      state.productionTask = null;
    }
    state.productionBatch = batch;
    saveSession();
    batch.warnings.forEach((warning) => toast(warning, "info"));
    toast(String(batch.total_pieces) + " artes generados y empaquetados.", "success");
    await navigate("results");
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    idle();
  }
}

async function resumeCampaignProductionTask(): Promise<void> {
  if (!state.activeClientId || !state.campaignWorkspace || !state.productionTask) return;
  const task = state.productionTask;
  busy(
    "Produciendo la campaña",
    task.meta.status || "Reconectando con el worker…",
    Math.max(5, Number(task.meta.progress || 5)),
  );
  try {
    const batch = await pollCampaignProductionTask(
      state.activeClientId,
      state.campaignWorkspace.campaign_id,
      task.task_id,
      busyProgress,
    );
    state.productionBatch = batch;
    state.productionTask = null;
    saveSession();
    toast(String(batch.total_pieces) + " artes generados y empaquetados.", "success");
    await navigate("results");
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    idle();
  }
}

async function renderGenerate(): Promise<void> {
  if (state.campaignWorkspace) {
    renderCampaignProduction();
    return;
  }
  const active = activeProject()!;
  state.generationMode = "catalog";
  content().innerHTML = [
    stepBar("generate"),
    pageHead("04 · PRODUCCIÓN", "Matriz → formatos → artes", "Carga el pedido de producción y genera todas las adaptaciones desde la plantilla aprobada."),
    productionMatrixHtml(),
    '<div class="spacer"></div>',
    '<section class="card"><div class="card-head"><div><h2>1. Productos</h2><p>',
    String(selectedProductFiles().length + validGroups().length),
    ' seleccionados · ', String(state.campaign.length), ' KV de referencia</p></div>',
    '<button class="ghost-button" id="edit-generation-products">Cambiar productos</button></div></section>',
    '<div class="spacer"></div>', formatSelectorHtml(true),
    '<div class="spacer"></div>', generationOptionsHtml("catalog"),
  ].join("");
  query("#edit-generation-products")?.addEventListener("click", () => navigate("products"));
  bindStepBar();
  bindGenerate(active);
  bindProductionMatrix();
}

function productionMatrixHtml(): string {
  const rows = state.productionMatrix;
  const isCampaign = Boolean(state.campaignWorkspace);
  const productFiles: Array<File | CampaignProductionAsset> = isCampaign
    ? campaignProductAssets()
    : state.products;
  const matched = rows.filter((row) => state.campaignWorkspace
    ? matrixRowReady(row) && matrixPlanReady(row)
    : matrixProductFile(row)
  ).length;
  const preview = rows.slice(0, 6).map((row) => '<tr><td>' + esc(row.product || "Institucional") + '</td><td>' + esc(row.image || "—") + '</td><td>' + esc(row.headline || "IA / vacío") + '</td><td>' + esc(row.price || "—") + '</td><td>' + esc(row.formats || "Por defecto") + '</td><td>' + String(row.proposals) + '</td></tr>').join("");
  const matchWarnings = state.campaignWorkspace ? rows.flatMap((row) => {
    const ambiguous = matrixAmbiguousTokens(row);
    if (ambiguous.length) return [
      'La fila “' + esc(row.product || row.image || "sin nombre") + '” coincide con varias imágenes para: ' +
      esc(ambiguous.join(", ")) + ". Especifica el archivo exacto en “imagen”.",
    ];
    if (matrixMatchedAssets(row).length < matrixExpectedProductCount(row)) return [
      'Falta una imagen para la fila “' + esc(row.product || row.image || "sin nombre") + '”.',
    ];
    return [];
  }) : [];
  return [
    '<section class="card matrix-card"><div class="card-head"><div><h2>Matriz de producción</h2><p>Una fila puede pedir uno o varios productos, formatos y propuestas. “No poner” elimina ese campo.</p></div><span class="badge', rows.length ? ' green' : '', '">', String(rows.length), ' FILAS</span></div>',
    '<div class="matrix-upload"><label class="dropzone compact"><input id="production-matrix" type="file" accept=".csv,.tsv,.xlsx,text/csv,text/tab-separated-values,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"><span class="drop-icon">⇧</span><strong>1. Subir matriz CSV, TSV o XLSX</strong><span>producto, imagen, titular, precio, CTA, formatos, propuestas</span></label>',
    '<label class="dropzone compact" id="matrix-product-drop"><input id="matrix-product-files" type="file" multiple accept=".png,.jpg,.jpeg,.webp,.bmp,.gif,.tif,.tiff,.avif,image/png,image/jpeg,image/webp,image/bmp,image/gif,image/tiff,image/avif"><span class="drop-icon">⇧</span><strong>2. Añadir imágenes de producto</strong><span>PNG, JPG, WEBP, BMP, GIF, TIFF o AVIF · también puedes arrastrarlas aquí</span></label></div>',
    '<div class="matrix-manual-launch"><div><strong>¿No tienes matriz?</strong><span>Escríbela aquí: una fila puede ser institucional, de un producto o un arte grupal.</span></div><button class="ghost-button add-manual-matrix-row">+ Crear fila manual</button></div>',
    '<div class="matrix-guide" style="margin-top:14px"><strong>Estado de la tanda</strong><span>', String(rows.length), ' filas · ', String(matched), ' listas para producir</span><small>',
    state.productionMatrixFile
      ? esc(state.productionMatrixFile.name)
      : state.productionMatrixDraftId
        ? "Matriz validada y guardada en esta campaña."
        : "Todavía no has subido la matriz.", '</small></div>',
    '<section class="matrix-product-inventory"><div><strong>Imágenes cargadas para esta tanda</strong><span>', String(productFiles.length), productFiles.length === 1 ? ' imagen' : ' imágenes', '</span></div>',
    productFiles.length
      ? '<div class="matrix-product-list">' + productFiles.map(matrixProductCard).join("") + '</div>'
      : '<p class="muted tiny">Aquí aparecerán con miniatura y estado de vínculo. Quedan guardadas dentro de esta campaña y puedes volver después sin cargarlas otra vez.</p>',
    '</section>',
    rows.length ? manualMatrixEditorHtml(rows, campaignProductAssets()) : '',
    state.campaignWorkspace ? matrixPlanReviewHtml(rows) : '',
    matchWarnings.length ? '<div class="notice warning compact matrix-match-warnings"><strong>Revisa las imágenes:</strong><ul>' + matchWarnings.map((warning) => '<li>' + warning + '</li>').join("") + '</ul></div>' : '',
    rows.length ? '<div class="inventory matrix-preview"><table><thead><tr><th>Producto</th><th>Imagen</th><th>Titular</th><th>Precio</th><th>Formatos</th><th>Propuestas</th></tr></thead><tbody>' + preview + '</tbody></table></div><div class="button-row" style="margin-top:12px"><button class="ghost-button" id="clear-production-matrix">Quitar matriz</button><span class="muted tiny">Vacío = la IA puede completar si corresponde · “no poner” = se omite.</span></div>' : '',
    '</section>',
  ].join("");
}

/** Editor visible para que la matriz no sea una barrera de Excel. La misma
 * sintaxis que entiende el CSV (``Producto A | Producto B``) representa un
 * arte grupal y conserva el contrato del backend para combos. */
function manualMatrixEditorHtml(
  rows: MatrixRow[],
  assets: CampaignProductionAsset[],
): string {
  const assetNames = assets.map((asset) => '<option value="' + attr(asset.filename) + '"></option>').join("");
  const approved = state.templateCandidates.filter((candidate) => candidate.status === "approved");
  const templateOptions = '<option value="">Automática · IA elige la compatible</option>' +
    approved.map((candidate) => '<option value="' + attr(candidate.name) + '">' + esc(candidate.name) + '</option>').join("");
  const input = (row: MatrixRow, field: keyof MatrixRow, label: string, options: {
    placeholder?: string; type?: string; list?: string; min?: number; value?: string | number;
  } = {}) => '<label class="field"><span>' + esc(label) + '</span><input class="manual-matrix-field" data-row="' +
    String(row.rowNumber) + '" data-field="' + attr(String(field)) + '"' +
    ' type="' + attr(options.type || "text") + '" value="' + attr(String(options.value ?? row[field] ?? "")) + '"' +
    (options.placeholder ? ' placeholder="' + attr(options.placeholder) + '"' : '') +
    (options.list ? ' list="' + attr(options.list) + '"' : '') +
    (options.min !== undefined ? ' min="' + String(options.min) + '"' : '') + '></label>';
  const select = '<label class="field"><span>Plantilla</span><select class="manual-matrix-field" data-row="ROW" data-field="template">OPTIONS</select></label>';
  const cards = rows.map((row) => {
    const template = select.replace("ROW", String(row.rowNumber)).replace(
      "OPTIONS",
      templateOptions.replace('value="' + attr(row.template) + '"', 'value="' + attr(row.template) + '" selected'),
    );
    return '<article class="manual-matrix-row"><div class="manual-matrix-row-head"><div><strong>Fila ' + String(row.rowNumber) + '</strong><span>Vacío = opcional · “no poner” = ocultar</span></div><button class="icon-button remove-manual-matrix-row" data-row="' + String(row.rowNumber) + '" title="Quitar fila" aria-label="Quitar fila ' + String(row.rowNumber) + '">×</button></div>' +
      '<div class="manual-matrix-grid">' +
      input(row, "product", "Producto(s) / arte grupal", { placeholder: "Silla | Mesa | Lámpara" }) +
      input(row, "image", "Archivo(s) de imagen", { placeholder: "silla.png | mesa.png", list: "matrix-asset-names" }) +
      input(row, "headline", "Titular", { placeholder: "Vacío = IA si la plantilla lo permite" }) +
      input(row, "subtitle", "Subtítulo") +
      input(row, "price", "Precio actual") +
      input(row, "previousPrice", "Precio anterior") +
      input(row, "installment", "Cuota") +
      input(row, "discount", "Descuento") +
      input(row, "cta", "CTA") +
      input(row, "validity", "Vigencia") +
      input(row, "legal", "Legal") +
      input(row, "formats", "Formatos", { placeholder: "feed | story | 1080x1350" }) +
      input(row, "proposals", "Propuestas", { type: "number", min: 1, value: Math.max(1, row.proposals) }) +
      template +
      input(row, "notes", "Notas de composición", { placeholder: "Ej. producto principal a la derecha" }) +
      '</div></article>';
  }).join("");
  return '<section class="manual-matrix-editor"><div class="card-head"><div><span class="kicker">EDITOR MANUAL</span><h3>Contenido de cada arte</h3><p>Para un combo, separa productos y archivos con <strong>|</strong> en el mismo orden. Una fila sin producto crea una pieza institucional.</p></div><div class="button-row"><button class="ghost-button add-manual-matrix-row">+ Añadir fila</button><button class="button" id="validate-manual-matrix">Validar matriz manual</button></div></div><datalist id="matrix-asset-names">' + assetNames + '</datalist><div class="manual-matrix-rows">' + cards + '</div><div class="manual-matrix-status muted tiny">Edita y luego valida: la IA comprobará plantilla, formatos y campos antes de producir.</div></section>';
}

function blankManualMatrixRow(): MatrixRow {
  const rowNumber = Math.max(1, ...state.productionMatrix.map((row) => row.rowNumber)) + 1;
  return {
    rowNumber: Math.max(2, rowNumber), product: "", image: "", headline: "", subtitle: "", price: "",
    previousPrice: "", installment: "", discount: "", cta: "", legal: "", validity: "", formats: "",
    proposals: 1, notes: "", template: "", omit: [],
  };
}

function matrixCsvCell(value: string | number): string {
  return '"' + String(value ?? "").replace(/"/g, '""') + '"';
}

function manualMatrixFile(rows = state.productionMatrix): File {
  const headers = [
    "producto", "imagen", "titular", "subtitulo", "precio_actual", "precio_anterior", "cuota", "descuento",
    "cta", "legal", "vigencia", "formatos", "cantidad_propuestas", "notas", "plantilla",
  ];
  const content = [headers.join(","), ...rows.map((row) => [
    row.product, row.image, row.headline, row.subtitle, row.price, row.previousPrice, row.installment,
    row.discount, row.cta, row.legal, row.validity, row.formats, Math.max(1, row.proposals), row.notes, row.template,
  ].map(matrixCsvCell).join(","))].join("\n");
  return new File([content], "matriz-manual.csv", { type: "text/csv" });
}

function applyCampaignMatrixPreview(file: File, preview: Awaited<ReturnType<typeof previewCampaignMatrix>>): void {
  state.productionMatrix = preview.rows.map((row: any) => ({
    rowNumber: Number(row?.row_number || 0),
    product: String(row?.producto || ""), image: String(row?.imagen || ""), headline: String(row?.titular || ""),
    subtitle: String(row?.subtitulo || ""), price: String(row?.precio_actual || ""),
    previousPrice: String(row?.precio_anterior || ""), installment: String(row?.cuota || ""),
    discount: String(row?.descuento || ""), cta: String(row?.cta || ""), legal: String(row?.legal || ""),
    validity: String(row?.vigencia || ""), formats: Array.isArray(row?.formatos) ? row.formatos.join("|") : "",
    proposals: Number(row?.cantidad_propuestas || 1), notes: String(row?.notas || ""),
    template: String(row?.plantilla || ""), omit: Array.isArray(row?.suppressed_fields) ? row.suppressed_fields.map(String) : [],
  }));
  state.productionMatrixPlans = preview.plans;
  state.productionMatrixFile = file;
  state.productionMatrixDraftId = preview.matrixDraftId;
}

function matrixKey(value: string): string {
  return value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

function bindProductionMatrix(): void {
  bindMatrixProductPreviewFallbacks();
  query<HTMLInputElement>("#production-matrix")?.addEventListener("change", async (event) => {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    // La referencia File queda en estado; liberar el control permite elegir la
    // misma matriz tras corregirla o después de un error de validación.
    input.value = "";
    if (!file) return;
    if (!state.activeClientId || !state.campaignWorkspace) {
      toast("Primero abre una campaña para validar la matriz.", "error");
      return;
    }
    try {
      const preview = await previewCampaignMatrix(
        state.activeClientId,
        state.campaignWorkspace.campaign_id,
        file,
        Array.from(state.selectedFormats),
      );
      applyCampaignMatrixPreview(file, preview);
      saveSession();
      toast(String(state.productionMatrix.length) + " filas importadas en la matriz.", "success");
      await renderGenerate();
    } catch (error) {
      toast(errorMessage(error), "error");
    }
  });
  const addManualRow = async (): Promise<void> => {
    state.productionMatrix = [...state.productionMatrix, blankManualMatrixRow()];
    state.productionMatrixPlans = [];
    state.productionMatrixDraftId = null;
    state.productionMatrixFile = null;
    saveSession();
    await renderGenerate();
  };
  queryAll<HTMLButtonElement>(".add-manual-matrix-row").forEach((button) => {
    button.addEventListener("click", () => void addManualRow());
  });
  queryAll<HTMLInputElement | HTMLSelectElement>(".manual-matrix-field").forEach((field) => {
    const update = () => {
      const rowNumber = Number(field.dataset.row || 0);
      const key = field.dataset.field as keyof MatrixRow | undefined;
      const row = state.productionMatrix.find((item) => item.rowNumber === rowNumber);
      if (!row || !key) return;
      if (key === "proposals") {
        row.proposals = Math.max(1, Math.trunc(Number(field.value) || 1));
        field.value = String(row.proposals);
      } else if (key !== "rowNumber" && key !== "omit") {
        (row as any)[key] = field.value;
      }
      // Una edición manual invalida el archivo y el preflight anterior. El
      // botón final queda bloqueado hasta que se valide de nuevo, en vez de
      // producir silenciosamente la versión vieja del CSV.
      state.productionMatrixPlans = [];
      state.productionMatrixDraftId = null;
      state.productionMatrixFile = null;
      query<HTMLButtonElement>("#run-campaign-production")?.setAttribute("disabled", "");
      const status = query<HTMLElement>(".manual-matrix-status");
      if (status) status.textContent = "Cambios sin validar. Pulsa “Validar matriz manual” antes de producir.";
      saveSession();
    };
    field.addEventListener("input", update);
    field.addEventListener("change", update);
  });
  queryAll<HTMLButtonElement>(".remove-manual-matrix-row").forEach((button) => {
    button.addEventListener("click", async () => {
      const rowNumber = Number(button.dataset.row || 0);
      state.productionMatrix = state.productionMatrix.filter((row) => row.rowNumber !== rowNumber);
      state.productionMatrixPlans = [];
      state.productionMatrixDraftId = null;
      state.productionMatrixFile = null;
      saveSession();
      await renderGenerate();
    });
  });
  query<HTMLButtonElement>("#validate-manual-matrix")?.addEventListener("click", async () => {
    if (!state.activeClientId || !state.campaignWorkspace || !state.productionMatrix.length) return;
    const file = manualMatrixFile();
    busy("Validando matriz manual", "Comprobando campos, combos y plantillas aprobadas…", 35);
    try {
      const preview = await previewCampaignMatrix(
        state.activeClientId,
        state.campaignWorkspace.campaign_id,
        file,
        Array.from(state.selectedFormats),
      );
      applyCampaignMatrixPreview(file, preview);
      saveSession();
      toast(String(state.productionMatrix.length) + " filas manuales validadas.", "success");
      await renderGenerate();
    } catch (error) {
      toast(errorMessage(error), "error");
    } finally {
      idle();
    }
  });
  const addProductImages = async (incoming: File[]): Promise<void> => {
    const accepted = incoming.filter((file) => PRODUCT_IMAGE_EXTENSIONS.test(file.name));
    const unsupported = incoming.filter((file) => !PRODUCT_IMAGE_EXTENSIONS.test(file.name));
    if (unsupported.length) {
      const iphone = unsupported.filter((file) => HEIC_EXTENSIONS.test(file.name));
      const remaining = unsupported.filter((file) => !HEIC_EXTENSIONS.test(file.name));
      if (iphone.length) {
        toast("HEIC/HEIF aún no puede procesarse aquí. Exporta esas fotos como JPG o PNG y vuelve a añadirlas.", "error");
      }
      if (remaining.length) {
        toast("No se admiten: " + remaining.map((file) => file.name).join(", ") + ". Usa PNG, JPG, WEBP, BMP, GIF, TIFF o AVIF.", "error");
      }
    }
    if (!accepted.length) return;
    if (state.activeClientId && state.campaignWorkspace) {
      const previous = new Set(campaignProductAssets().map((asset) => asset.asset_id));
      busy("Guardando imágenes de producto", "Subiendo para esta campaña…", 12);
      try {
        const uploaded = await uploadCampaignProductAssets(
          state.activeClientId,
          state.campaignWorkspace.campaign_id,
          accepted,
          (sent, total) => busyProgress(
            total ? Math.max(12, Math.round(sent / total * 88)) : 45,
            total ? "Subiendo " + readableSize(sent) + " de " + readableSize(total) : "Subiendo imágenes…",
          ),
        );
        state.campaignWorkspace = {
          ...state.campaignWorkspace,
          production_assets: uploaded.assets,
        };
        const added = uploaded.assets.filter((asset) => !previous.has(asset.asset_id));
        uploaded.warnings.forEach((warning) => toast(warning, "info"));
        toast(
          added.length
            ? String(added.length) + (added.length === 1 ? " imagen guardada en la campaña." : " imágenes guardadas en la campaña.")
            : "Esas imágenes ya estaban guardadas en esta campaña.",
          added.length ? "success" : "info",
        );
      } catch (error) {
        toast(errorMessage(error), "error");
      } finally {
        idle();
      }
    } else {
      const before = new Set(state.products.map(productKey));
      state.products = mergeUniqueFiles(state.products, accepted);
      const added = accepted.filter((file) => !before.has(productKey(file)));
      for (const file of added) {
        if (state.productionMatrix.some((row) => matrixFileMatches(row, file))) {
          state.individualProducts.add(productKey(file));
        }
      }
      toast(String(added.length) + (added.length === 1 ? " imagen añadida." : " imágenes añadidas."), "success");
    }
    saveSession();
    await renderGenerate();
  };
  query<HTMLInputElement>("#matrix-product-files")?.addEventListener("change", async (event) => {
    const input = event.currentTarget as HTMLInputElement;
    const files = Array.from(input.files || []);
    // Permite seleccionar el mismo archivo otra vez después de quitarlo.
    input.value = "";
    await addProductImages(files);
  });
  const productDrop = query<HTMLElement>("#matrix-product-drop");
  if (productDrop) {
    ["dragenter", "dragover"].forEach((name) => productDrop.addEventListener(name, (event) => {
      event.preventDefault();
      productDrop.classList.add("is-dragging");
    }));
    ["dragleave", "dragend"].forEach((name) => productDrop.addEventListener(name, () => productDrop.classList.remove("is-dragging")));
    productDrop.addEventListener("drop", (event) => {
      event.preventDefault();
      productDrop.classList.remove("is-dragging");
      void addProductImages(Array.from((event as DragEvent).dataTransfer?.files || []));
    });
  }
  queryAll<HTMLButtonElement>(".matrix-product-remove").forEach((button) => {
    button.addEventListener("click", async () => {
      const assetId = button.dataset.assetId || "";
      if (assetId && state.activeClientId && state.campaignWorkspace) {
        busy("Quitando imagen", "Actualizando los activos de producción…", 35);
        try {
          await deleteCampaignProductAsset(
            state.activeClientId,
            state.campaignWorkspace.campaign_id,
            assetId,
          );
          state.campaignWorkspace = {
            ...state.campaignWorkspace,
            production_assets: campaignProductAssets().filter((asset) => asset.asset_id !== assetId),
          };
          toast("Imagen quitada de la campaña.", "success");
          saveSession();
          await renderGenerate();
        } catch (error) {
          toast(errorMessage(error), "error");
        } finally {
          idle();
        }
        return;
      }
      const key = button.dataset.productKey || "";
      const removed = state.products.find((file) => productKey(file) === key);
      state.products = state.products.filter((file) => productKey(file) !== key);
      state.individualProducts.delete(key);
      if (removed) productUrls.delete(removed);
      saveSession();
      await renderGenerate();
    });
  });
  query("#clear-production-matrix")?.addEventListener("click", async () => {
    state.productionMatrix = [];
    state.productionMatrixPlans = [];
    state.productionMatrixDraftId = null;
    state.productionMatrixFile = null;
    saveSession();
    await renderGenerate();
  });
}

/** El nombre del motor tal como se lee en pantalla. */
function engineLabel(engine: string): string {
  if (engine === "magnific") return "Magnific";
  if (engine === "openai") return "OpenAI";
  if (engine === "opencv") return "el motor local";
  return "Automático";
}

function engineOption(value: string, label: string, selected: string): string {
  return '<option value="' + attr(value) + '"' +
    (value === selected ? " selected" : "") + ">" + esc(label) + "</option>";
}

/** Modelos del motor elegido. Cada entrada del catálogo trae su `provider`.
 *
 *  Mezclarlos era ofrecer los quince de Magnific con OpenAI seleccionado y
 *  descartar la elección en silencio al generar. */
function modelOptionsFor(engine: string): string {
  return (state.capabilities?.image_models || [])
    .filter((model) => String(model.provider || "magnific") === engine)
    .map((model) =>
      '<option value="' + attr(model.id) + '">' + esc(model.label || model.id) + "</option>"
    )
    .join("");
}

/** ¿Ese motor elige modelo? El local no, y «Automático» lo decide el servidor. */
function engineTakesModel(engine: string): boolean {
  return Boolean(modelOptionsFor(engine));
}

function generationOptionsHtml(mode: "catalog" | "compose"): string {
  const engine = state.generationEngine;
  const models = modelOptionsFor(engine);
  const countMin = 1;
  const countMax = mode === "catalog" ? 6 : 30;
  const countValue = state.proposalsPerFormat;
  const layouts = (state.capabilities?.layouts || []).map((layout) =>
    '<label class="choice"><input class="layout-check" type="checkbox" value="' + attr(layout.key) + '"> ' + esc(layout.label) + "</label>"
  ).join("");
  return [
    // El modelo y el contexto ya no viven dentro de un acordeón cerrado: eran
    // justo las dos decisiones que el usuario no encontraba.
    '<section class="card elevated"><div class="card-head"><div><h2>3. Indicaciones y modelo</h2><p>Conserva la composición revisada o describe los cambios que quieres.</p></div></div>',
    '<label class="choice" style="margin-bottom:14px"><input id="regenerate-background" type="checkbox"> Rehacer el fondo con IA</label>',
    '<div class="form-grid"><label class="field"><span>Motor del fondo</span><select id="generation-provider">',
    engineOption("opencv", "Conservar el fondo · sin IA", engine),
    engineOption("magnific", "Magnific · eliges el modelo (con costo)", engine),
    engineOption("openai", "OpenAI · IA de imagen (con costo)", engine),
    engineOption("auto", "Automático · el que tenga clave", engine),
    '</select></label>',
    '<label class="field"><span>Modelo de IA</span><select id="generation-model"',
    models ? "" : " disabled",
    '><option value="">Predeterminado</option>', models,
    '</select><small>',
    models
      ? "Modelos de " + esc(engineLabel(engine)) + ". Cambia el motor para ver otros."
      : "Lo elige el servidor según las claves que tenga.",
    "</small></label></div>",
    '<label class="field" style="margin-top:14px"><span>Contexto · qué quieres del arte</span>',
    '<textarea id="generation-instruction" placeholder="Producto grande, titular arriba, composición minimal"></textarea>',
    "<small>Entiende: producto grande o pequeño, titular arriba, centrado, vertical, diagonal, dividido, izquierda, derecha, minimal.</small></label>",
    '<label class="field" style="margin-top:14px"><span>Dirección visual del fondo · opcional</span>',
    '<textarea id="generation-background-prompt" placeholder="Fondo deportivo premium, luces azules, profundidad, sin texto ni logos"></textarea>',
    "<small>Solo se usa si se rehace el fondo.</small></label></section>",

    '<div class="spacer"></div>',
    '<section class="card"><div class="card-head"><div><h2>Variación</h2><p>Cuántas propuestas por formato y cuánto pueden alejarse</p></div></div>',
    // Conservar el diseño era una deducción silenciosa —«no pidió fondo nuevo ni
    // escribió nada, luego quiere el KV intacto»— y decidía sola cuántas piezas
    // salían. Ahora es una casilla: el usuario ve qué contrato está eligiendo.
    mode === "catalog"
      ? '<label class="choice" style="margin-bottom:14px"><input id="keep-template" type="checkbox"' +
        checked(state.keepTemplate) + "> Conservar el diseño del KV · solo cambia el producto" +
        '<small class="muted tiny" style="display:block">Una propuesta por formato: el diseño aprobado ' +
        "solo tiene una composición por medida, y no se aplican ni las indicaciones ni el fondo nuevo. " +
        "Desmárcalo para recibir varias propuestas distintas.</small></label>"
      : "",
    '<div class="form-grid"><label class="field"><span>Propuestas por formato</span><input id="generation-count" type="number" min="', String(countMin), '" max="', String(countMax), '" value="', String(countValue), '"', mode === "catalog" && state.keepTemplate ? " disabled" : "", "></label>",
    '<label class="field"><span>Cuánto se pueden alejar del original</span><select id="generation-intensity">',
    '<option value="conservative">Parecidas al original</option><option value="moderate" selected>Equilibradas</option>',
    '<option value="creative">Muy distintas entre sí</option></select></label></div>',
    '<p class="notice" id="generation-total" style="margin-top:14px">', generationTotalsText(mode), "</p>",
    '<label class="field" style="margin-top:14px"><span>Semilla</span><input id="generation-seed" type="number" min="0" max="2147483647" value="42"><small>La misma semilla repite el mismo resultado.</small></label>',
    mode === "compose" && layouts ? '<div style="margin-top:14px"><span class="label">Familias de layout · vacío = todas</span><div class="choice-row" style="margin-top:8px">' + layouts + "</div></div>" : "",
    "</section>",
    '<button class="button large full" id="run-generation" style="margin-top:20px">✦ ',
    mode === "catalog" ? "Generar artes por producto" : "Generar variantes", "</button>",
  ].join("");
}

/** Tope de piezas de una tanda. El mismo que aplica el backend: decirlo antes
 *  de generar evita la sorpresa de pedir cuarenta y recibir treinta. */
const MAX_PIECES = 30;

/** Qué va a producir el botón, en piezas, con los ajustes que hay en pantalla.
 *
 *  Pedir cinco formatos y recibir uno era el fallo; no poder saberlo antes de
 *  pulsar era la razón de que nadie lo detectara hasta el final de la tanda. */
function generationPlan(mode: "catalog" | "compose" = state.generationMode): {
  formatos: number;
  porFormato: number;
  piezas: number;
  salidas: number;
  recortado: boolean;
} {
  // Al pintar la tarjeta todavía no hay casilla que consultar, así que el
  // estado es la fuente; una vez en pantalla manda lo que el usuario tocó.
  // Conservar el diseño solo existe en el modo catálogo.
  const fiel = mode === "catalog"
    && Boolean(query<HTMLInputElement>("#keep-template")?.checked ?? state.keepTemplate);
  const campo = Number(
    query<HTMLInputElement>("#generation-count")?.value || state.proposalsPerFormat,
  );
  // Sin formatos elegidos manda el KV: su tamaño nativo más los de redes que el
  // arte aguante. Son dos o tres; se cuenta uno para no prometer de más.
  const formatos = state.autoFormats ? 1 : Math.max(0, state.selectedFormats.size);
  const pedido = fiel ? 1 : Math.max(1, campo);
  const porFormato = formatos
    ? Math.max(1, Math.min(pedido, Math.floor(MAX_PIECES / formatos)))
    : pedido;
  const salidas = Math.max(1, selectedProductFiles().length + validGroups().length);
  return {
    formatos,
    porFormato,
    piezas: formatos * porFormato,
    salidas,
    recortado: porFormato < pedido,
  };
}

function generationTotalsText(mode: "catalog" | "compose" = state.generationMode): string {
  const plan = generationPlan(mode);
  if (!plan.formatos) {
    return "<strong>Ningún formato elegido.</strong> Marca al menos uno arriba.";
  }
  const kv = state.campaign.length;
  const partes = [
    state.autoFormats
      ? "<strong>Tamaños del KV</strong> (el original y los de redes que aguante)"
      : "<strong>" + String(plan.formatos) + " formato" + (plan.formatos === 1 ? "" : "s") + "</strong>",
    "× " + String(plan.porFormato) + " propuesta" + (plan.porFormato === 1 ? "" : "s"),
    "× " + String(plan.salidas) + " producto" + (plan.salidas === 1 ? "" : "s"),
    kv > 1 ? "× " + String(kv) + " KV" : "",
  ].filter(Boolean).join(" ");
  const total = plan.piezas * plan.salidas * Math.max(1, kv);
  const recorte = plan.recortado
    ? " Una tanda no pasa de " + String(MAX_PIECES) + " piezas por producto: se entregarán " +
      String(plan.porFormato) + " por formato."
    : "";
  return partes + " = <strong>" + String(total) + " piezas</strong>." + recorte;
}

/** Repinta la cuenta sin rehacer la pantalla: se lee mientras se elige. */
function refreshGenerationTotals(): void {
  const node = query<HTMLElement>("#generation-total");
  if (node) node.innerHTML = generationTotalsText();
}

function bindFormatSelector(): void {
  query<HTMLInputElement>("#auto-formats")?.addEventListener("change", (event) => {
    state.autoFormats = (event.currentTarget as HTMLInputElement).checked;
    query<HTMLElement>("#manual-formats")!.hidden = state.autoFormats;
    refreshGenerationTotals();
  });
  queryAll<HTMLButtonElement>(".platform-filter").forEach((button) => {
    button.addEventListener("click", async () => {
      state.formatPlatform = button.dataset.platform || "Todos";
      const section = button.closest("section");
      if (section) section.outerHTML = formatSelectorHtml(!state.campaignWorkspace);
      bindFormatSelector();
    });
  });
  queryAll<HTMLInputElement>(".format-check").forEach((checkBox) => {
    checkBox.addEventListener("change", () => {
      if (checkBox.checked) state.selectedFormats.add(checkBox.value);
      else state.selectedFormats.delete(checkBox.value);
      const badge = query<HTMLElement>(".card .badge");
      if (badge && badge.textContent?.includes("ELEGIDOS")) badge.textContent = String(state.selectedFormats.size) + " ELEGIDOS";
      if (state.campaignWorkspace) void renderGenerate();
      else refreshGenerationTotals();
    });
  });
}

function bindGenerate(project: Project): void {
  queryAll<HTMLButtonElement>(".generation-mode").forEach((button) => {
    button.addEventListener("click", async () => {
      state.generationMode = button.dataset.mode as "catalog" | "compose";
      state.autoFormats = state.generationMode === "catalog";
      await renderGenerate();
    });
  });
  query("#go-catalog-mode")?.addEventListener("click", async () => {
    state.generationMode = "catalog";
    state.autoFormats = true;
    await renderGenerate();
  });
  // La lista de modelos sigue al motor: hay que repintar al cambiarlo.
  query<HTMLSelectElement>("#generation-provider")?.addEventListener("change", async (event) => {
    state.generationEngine = (event.currentTarget as HTMLSelectElement).value;
    const select = query<HTMLSelectElement>("#generation-model")!;
    select.innerHTML = '<option value="">Predeterminado</option>' + modelOptionsFor(state.generationEngine);
    select.disabled = !engineTakesModel(state.generationEngine);
  });
  query<HTMLInputElement>("#keep-template")?.addEventListener("change", (event) => {
    state.keepTemplate = (event.currentTarget as HTMLInputElement).checked;
    const campo = query<HTMLInputElement>("#generation-count")!;
    // Con el diseño conservado solo hay una composición por medida: el campo se
    // apaga en vez de aceptar un número que después no se cumple.
    campo.disabled = state.keepTemplate;
    refreshGenerationTotals();
  });
  query<HTMLInputElement>("#generation-count")?.addEventListener("input", (event) => {
    state.proposalsPerFormat = Number((event.currentTarget as HTMLInputElement).value) || 1;
    refreshGenerationTotals();
  });
  bindFormatSelector();
  query("#run-generation")?.addEventListener("click", () => {
    if (state.generationMode === "catalog") runCatalogGeneration();
    else runComposeGeneration(project);
  });
}

function generationSettings(): Record<string, any> {
  const generalInstruction = query<HTMLTextAreaElement>("#generation-instruction")!.value.trim();
  return {
    // Propuestas POR FORMATO. El backend las multiplica por las medidas
    // elegidas: es lo que antes se repartía entre ellas y dejaba formatos con
    // una sola pieza —o, en sustitución fiel, con ninguna—.
    count: Number(query<HTMLInputElement>("#generation-count")!.value),
    formats: state.autoFormats ? null : Array.from(state.selectedFormats),
    // Ya no se deduce de si hay fondo nuevo o indicaciones: lo dice la casilla.
    template_mode: Boolean(query<HTMLInputElement>("#keep-template")?.checked),
    intensity: query<HTMLSelectElement>("#generation-intensity")!.value,
    instruction: generalInstruction || null,
    product_position_instruction: null,
    seed: Number(query<HTMLInputElement>("#generation-seed")!.value),
    product_arrangement: "auto",
    background_provider: state.generationEngine,
    background_model: query<HTMLSelectElement>("#generation-model")!.value || null,
    background_prompt: query<HTMLTextAreaElement>("#generation-background-prompt")!.value.trim() || null,
    regenerate_background: query<HTMLInputElement>("#regenerate-background")!.checked,
  };
}

async function replaceProduct(projectId: string, targetId: string, file: File, options: Record<string, string | boolean>): Promise<any> {
  const data = new FormData();
  data.append("image", file);
  data.append("layer_id", targetId);
  Object.entries(options).forEach(([key, value]) => data.append(key, String(value)));
  return post("/projects/" + projectId + "/layers/replace", data);
}

/** Avisos de una tanda, sin repetirlos.
 *
 * Una campaña recorre KV x producto: con tres KV y veinte productos, un aviso
 * que sale en cada reemplazo aparece sesenta veces. Sesenta tostadas iguales no
 * informan de nada y tapan la pantalla mientras se genera. Se dice una vez, y al
 * final cuántas veces pasó. */
function avisadorDeTanda(): { avisar: (mensajes: string[] | undefined) => void; resumen: () => void } {
  const vistos = new Map<string, number>();
  return {
    avisar(mensajes) {
      (mensajes || []).forEach((mensaje) => {
        const veces = (vistos.get(mensaje) || 0) + 1;
        vistos.set(mensaje, veces);
        if (veces === 1) toast(mensaje, "info");
      });
    },
    resumen() {
      const repetidos = [...vistos.entries()].filter(([, veces]) => veces > 1);
      if (repetidos.length) {
        const total = repetidos.reduce((suma, [, veces]) => suma + veces, 0);
        toast(
          "Se repitieron " + String(total) + " avisos en la tanda; están en el detalle de cada propuesta.",
          "info",
        );
      }
    },
  };
}

async function runCatalogGeneration(): Promise<void> {
  const individuals = selectedProductFiles();
  const validGroups = state.groups.map((group) => ({
    ...group,
    files: group.members.map((key) => state.products.find((file) => productKey(file) === key)).filter(Boolean) as File[],
  })).filter((group) => group.files.length >= 2);
  if (!individuals.length && !validGroups.length) {
    toast("Elige al menos un producto separado o una combinación.", "error");
    return;
  }
  if (!state.autoFormats && !state.selectedFormats.size) {
    toast("Elige al menos un formato de salida.", "error");
    return;
  }
  const allTargets = state.campaign.map((project) => ({ project, layer: productTarget(project) }));
  const sinProducto = allTargets.filter((item) => !item.layer).map((item) => item.project);
  const targets = allTargets.filter((item) => Boolean(item.layer));
  if (sinProducto.length) {
    toast(
      "Se omitirán los KV sin producto identificado: " + nameList(sinProducto) + ".",
      "info",
    );
  }
  if (!targets.length) {
    toast("No hay ningún KV con un producto identificado para reemplazar.", "error");
    return;
  }
  const settings = generationSettings();
  const total = targets.length * (individuals.length + validGroups.length);
  let completed = 0;
  const avisos = avisadorDeTanda();
  busy("Produciendo campaña", "Preparando " + String(total) + " tandas…", 3);
  try {
    for (let kvIndex = 0; kvIndex < targets.length; kvIndex += 1) {
      const project = targets[kvIndex].project;
      const target = targets[kvIndex].layer!;
      let firstBatch = true;
      for (let index = 0; index < individuals.length; index += 1) {
        const file = individuals[index];
        busyProgress(Math.round(completed / Math.max(1, total) * 100), project.name + " · " + file.name);
        const replaced = await replaceProduct(project.project_id, target.id, file, {
          hide_others: true, append: false, arrangement: "auto",
        });
        avisos.avisar(replaced.warnings);
        const task = await post<any>("/projects/" + project.project_id + "/auto", {
          ...settings,
          seed: settings.seed + kvIndex * 100 + index,
          replace_existing: firstBatch,
          product_label: productName(file),
          product_arrangement: "auto",
          template_mode: settings.template_mode,
          regenerate_background: settings.regenerate_background && firstBatch,
          text_overrides: [
            ...matrixTextOverrides(project, file),
            ...(textOverrides(project, productKey(file)) || []),
          ],
        });
        const result = await pollTask(project.project_id, task.task_id, (progress, detail) => {
          const batchProgress = (completed + progress / 100) / Math.max(1, total) * 100;
          busyProgress(batchProgress, project.name + " · " + detail);
        });
        avisos.avisar(result.warnings);
        firstBatch = false;
        completed += 1;
      }
      for (let groupIndex = 0; groupIndex < validGroups.length; groupIndex += 1) {
        const group = validGroups[groupIndex];
        busyProgress(Math.round(completed / Math.max(1, total) * 100), project.name + " · " + group.name);
        for (let memberIndex = 0; memberIndex < group.files.length; memberIndex += 1) {
          const replaced = await replaceProduct(project.project_id, target.id, group.files[memberIndex], {
            hide_others: memberIndex === 0,
            append: memberIndex > 0,
            group_id: group.id,
            group_name: group.name,
            arrangement: group.arrangement,
          });
          avisos.avisar(replaced.warnings);
        }
        const label = group.files.map(productName).join(" + ");
        const task = await post<any>("/projects/" + project.project_id + "/auto", {
          ...settings,
          seed: settings.seed + kvIndex * 100 + individuals.length + groupIndex,
          replace_existing: firstBatch,
          product_label: label,
          product_arrangement: group.arrangement,
          template_mode: settings.template_mode,
          regenerate_background: settings.regenerate_background && firstBatch,
          text_overrides: textOverrides(project, COMBO_PREFIX + group.id),
        });
        const result = await pollTask(project.project_id, task.task_id, (progress, detail) => {
          const batchProgress = (completed + progress / 100) / Math.max(1, total) * 100;
          busyProgress(batchProgress, project.name + " · " + detail);
        });
        avisos.avisar(result.warnings);
        firstBatch = false;
        completed += 1;
      }
      await refreshProject(project.project_id);
      await loadTexts(project.project_id, true);
    }
    state.selectedVariants.clear();
    avisos.resumen();
    toast("Campaña generada correctamente.", "success");
    await navigate("results");
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    idle();
  }
}

function checkedLayerIds(selector: string): string[] {
  return queryAll<HTMLInputElement>(selector + ":checked").map((item) => item.dataset.layer!).filter(Boolean);
}

async function runComposeGeneration(project: Project): Promise<void> {
  if (!state.selectedFormats.size) {
    toast("Elige al menos un formato de salida.", "error");
    return;
  }
  const settings = generationSettings();
  const layouts = queryAll<HTMLInputElement>(".layout-check:checked").map((item) => item.value);
  const formats = Array.from(state.selectedFormats);
  const payload = {
    // El `count` de /generate son composiciones totales y se reparten entre los
    // formatos; en pantalla se piden POR formato. Multiplicarlo aquí es lo que
    // hace que «2 propuestas y 3 formatos» sean seis piezas y no dos.
    count: Math.min(30, Math.max(formats.length, settings.count * formats.length)),
    seed: settings.seed,
    formats,
    intensity: settings.intensity,
    instruction: settings.instruction,
    product_position_instruction: settings.product_position_instruction,
    layouts: layouts.length ? layouts : null,
    product_arrangement: settings.product_arrangement,
    replace_existing: true,
    locked_layers: checkedLayerIds(".permission-lock"),
    movable_layers: checkedLayerIds(".permission-move"),
    resizable_layers: checkedLayerIds(".permission-resize"),
    reorderable_layers: checkedLayerIds(".permission-reorder"),
    hidden_layers: checkedLayerIds(".permission-hide"),
  };
  busy("Generando variantes", project.name + " · preparando composiciones…", 6);
  try {
    const task = await post<any>("/projects/" + project.project_id + "/generate", payload);
    const result = await pollTask(project.project_id, task.task_id, busyProgress);
    await refreshProject(project.project_id);
    state.selectedVariants.clear();
    const variants = result.variants || [];
    toast(String(variants.length) + " variantes generadas.", variants.length ? "success" : "info");
    (result.warnings || []).forEach((warning: string) => toast(warning, "info"));
    await navigate("results");
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    idle();
  }
}

function resultKey(projectId: string, variantId: string): string {
  return projectId + "::" + variantId;
}

function resultCard(project: Project, variant: Variant): string {
  const format = variant.meta?.format || {};
  const key = resultKey(project.project_id, variant.id);
  const warnings = variant.quality?.warnings || [];
  const notes: string[] = variant.meta?.notes || [];
  const svg = variant.meta?.svg;
  const psd = variant.meta?.psd;
  const product = variant.meta?.product_label || "Producto";
  // El backend ya renderiza un JPEG reducido por variante; pedir el PNG a
  // tamaño real para una rejilla de 280px era descargar megas por tarjeta.
  const preview = variant.thumbnail || variant.image;
  const slot = viewerItems.findIndex(
    (item) => item.projectId === project.project_id && item.variantId === variant.id,
  );
  // La proporción real del formato: sin ella, un story 9:16 y un banner
  // 1.91:1 se veían igual de altos en la rejilla y el único modo de distinguir
  // uno de otro era leer el texto de la ficha.
  const ratio = (variant.width / Math.max(1, variant.height)).toFixed(4);
  return [
    '<article class="result-card"><div class="result-image" style="--ratio:', ratio, '">',
    '<button type="button" class="result-open" data-slot="', String(slot), '" aria-label="Ver en grande">',
    '<img src="', attr(fileUrl(project.project_id, preview)), '" alt="Variante ', String(variant.index),
    '" title="Ver en grande" loading="lazy" decoding="async"></button><span class="score">',
    String(Math.round(variant.quality?.score || 0)), '</span></div><div class="result-copy"><strong>', String(variant.width), "×", String(variant.height), '</strong><p>',
    esc((format.platform || "") + (format.placement ? " · " + format.placement : " · " + variant.format) + (format.ratio ? " · " + format.ratio : "")), "<br>", esc(product), "</p>",
    '<label class="check"><input class="variant-pick" type="checkbox" value="', attr(key), '"', checked(state.selectedVariants.has(key)), '> Elegir</label>',
    '<div class="button-row" style="margin-top:11px"><a class="ghost-button" href="', attr(variantPngUrl(project.project_id, variant.id)), '" download>PNG</a>',
    psd ? '<a class="ghost-button" href="' + attr(fileUrl(project.project_id, psd)) + '" download>Photoshop PSD</a>' : '<span class="badge gray">PSD al regenerar</span>',
    svg ? '<a class="ghost-button" href="' + attr(fileUrl(project.project_id, svg)) + '" download>Illustrator SVG</a>' : '<span class="badge gray">SVG al regenerar</span>',
    '</div><details style="margin-top:12px"><summary>', warnings.length ? "⚠ " + String(warnings.length) + " avisos" : "Detalle", '</summary><div class="notice" style="margin-top:8px">Composición: ',
    esc(variant.layout_label), warnings.length ? "<br>" + warnings.map((warning) => "• " + esc(warning)).join("<br>") : "<br>Sin avisos.",
    // Lo que el motor decidió solo: cómo escaló los productos, si tuvo que
    // rehacer la composición. Sin esto, «es automático» es indistinguible de
    // «no hizo nada».
    notes.length ? "<br><br>Decisiones del motor:<br>" + notes.map((note) => "• " + esc(note)).join("<br>") : "",
    "</div></details></div></article>",
  ].join("");
}

async function renderResults(): Promise<void> {
  if (state.campaignWorkspace) {
    await renderCampaignResults();
    return;
  }
  if (!state.campaign.length) {
    content().innerHTML = emptyState("▦", "No hay resultados", "Abre una campaña y genera sus primeras propuestas.", '<button class="button" id="go-generate">Ir a generar</button>');
    query("#go-generate")?.addEventListener("click", () => navigate("generate"));
    return;
  }
  const projects = await Promise.all(state.campaign.map((project) => refreshProject(project.project_id)));
  const total = projects.reduce((sum, project) => sum + project.variants.length, 0);
  if (!total) {
    content().innerHTML = [
      pageHead("Galería de campaña", "Todavía no hay propuestas", "Cuando generes, aquí podrás comparar, elegir y exportar PNG, PSD y SVG para Illustrator."),
      emptyState("▦", "Tu galería está vacía", "Configura productos, combinaciones y formatos para producir las primeras piezas.", '<button class="button" id="go-generate">Ir a generar</button>'),
    ].join("");
    query("#go-generate")?.addEventListener("click", () => navigate("generate"));
    return;
  }
  const allScores = projects.flatMap((project) => project.variants.map((variant) => variant.quality?.score || 0));
  const selectedCount = state.selectedVariants.size;
  const ordenar = (list: Variant[]) => [...list].sort(state.resultOrder === "score"
    ? (a, b) => (b.quality?.score || 0) - (a.quality?.score || 0)
    : (a, b) => a.index - b.index);
  // El filtro trabaja sobre los avisos, no sobre el puntaje: un aviso dice qué
  // está mal y por qué, y un número no. "Sin avisos" es lo que se puede publicar
  // sin volver a mirarlo.
  const pasaFiltro = (variant: Variant) =>
    state.resultFilter === "all" || !(variant.quality?.warnings || []).length;
  const mostrar = (list: Variant[]) => ordenar(list).filter(pasaFiltro);
  const shown = projects.reduce((sum, project) => sum + mostrar(project.variants).length, 0);
  const hidden = total - shown;
  // El recorrido del visor se fija aquí, con el mismo orden con el que se
  // pintan las tarjetas: las flechas siguen lo que el usuario está viendo.
  viewerItems = projects
    .filter((project) => project.variants.length)
    .flatMap((project) =>
      mostrar(project.variants).map((variant) => ({
        projectId: project.project_id,
        projectName: project.name,
        variantId: variant.id,
      })),
    );
  const sections = projects.filter((project) => mostrar(project.variants).length).map((project) => {
    const variants = mostrar(project.variants);
    const picked = variants.filter((variant) => state.selectedVariants.has(resultKey(project.project_id, variant.id))).map((variant) => variant.id);
    // El ZIP tiene que traer lo que se está viendo. Sin esto, "Descargar todas"
    // con el filtro puesto metía en el paquete justo las que el filtro escondió.
    const bajar = picked.length ? picked : variants.map((variant) => variant.id);
    const etiqueta = picked.length
      ? String(picked.length) + " elegidas"
      : state.resultFilter === "clean" ? "las " + String(variants.length) + " sin avisos" : "todas";
    const cuenta = project.variants.length === variants.length
      ? String(variants.length) + " propuestas"
      : String(variants.length) + " de " + String(project.variants.length) + " propuestas";
    return [
      '<section style="margin-top:28px"><div class="card-head"><div><h2>', esc(project.name), '</h2><p>', cuenta, '</p></div>',
      '<button class="button export-project" data-project="', attr(project.project_id), '" data-ids="', attr(bajar.join(",")), '">Descargar ', etiqueta, " · ZIP</button></div>",
      '<div class="result-grid">', variants.map((variant) => resultCard(project, variant)).join(""), "</div></section>",
    ].join("");
  }).join("");
  content().innerHTML = [
    stepBar("results"),
    pageHead("Galería de campaña", "Resultados listos para revisar", "Compara piezas, marca tus favoritas y descarga PNG + PSD + SVG editable para Illustrator."),
    '<div class="stat-row"><div class="stat"><strong>', String(total), '</strong><span>Propuestas</span></div><div class="stat"><strong>',
    String(Math.round(allScores.reduce((sum, value) => sum + value, 0) / Math.max(1, allScores.length))), '</strong><span>Calidad promedio</span></div><div class="stat"><strong>',
    String(Math.round(Math.max(...allScores))), '</strong><span>Mejor puntaje</span></div><div class="stat"><strong id="selected-count">', String(selectedCount), '</strong><span>Elegidas</span></div></div>',
    '<div class="button-row" style="margin-top:18px"><span class="label">Orden:</span><button class="platform-filter result-order', state.resultOrder === "score" ? " is-active" : "", '" data-order="score">Mejores primero</button>',
    '<button class="platform-filter result-order', state.resultOrder === "generation" ? " is-active" : "", '" data-order="generation">Orden de generación</button><button class="ghost-button right" id="refresh-results">↻ Actualizar</button></div>',
    '<div class="button-row" style="margin-top:10px"><span class="label">Mostrar:</span><button class="platform-filter result-filter', state.resultFilter === "all" ? " is-active" : "", '" data-filter="all">Todas</button>',
    '<button class="platform-filter result-filter', state.resultFilter === "clean" ? " is-active" : "", '" data-filter="clean">Sin avisos</button>',
    // Nunca se esconde nada en silencio: si el filtro se come propuestas, se dice
    // cuántas, porque si no parece que la generación produjo menos de lo que produjo.
    hidden ? '<span class="hint">' + String(hidden) + (hidden === 1 ? " propuesta oculta por traer avisos" : " propuestas ocultas por traer avisos") + "</span>" : "",
    "</div>",
    shown ? sections : emptyState("✓", "Ninguna propuesta está limpia", "Las " + String(total) + " tienen algún aviso. Mira el detalle de cada tarjeta en «Todas» para saber qué corregir.", '<button class="button" id="show-all-results">Ver todas</button>'),
  ].join("");
  bindStepBar();
  bindResults();
}

async function renderCampaignResults(): Promise<void> {
  if (!state.activeClientId || !state.campaignWorkspace) return;
  const batches = await listProductionBatches(
    state.activeClientId,
    state.campaignWorkspace.campaign_id,
  );
  if (batches.length) state.productionBatch = batches[0];
  const batch = state.productionBatch;
  if (!batch?.pieces.length) {
    const pending = state.productionTask && !["COMPLETED", "FAILED"].includes(state.productionTask.state)
      ? '<div class="notice compact" style="margin-top:16px"><strong>La tanda sigue en proceso.</strong> ' +
        esc(state.productionTask.meta.status || "El worker continúa renderizando.") +
        ' <button class="ghost-button" id="resume-production-task">Ver progreso</button></div>'
      : state.productionTask?.state === "FAILED"
        ? '<div class="notice warning" style="margin-top:16px"><strong>La última tanda falló:</strong> ' +
          esc(state.productionTask.error || "No se pudo completar.") + '</div>'
        : '';
    content().innerHTML = [
      stepBar("results"),
      pageHead("05 · ENTREGABLES", "Todavía no hay artes", "Carga una matriz y produce la primera tanda desde las plantillas aprobadas."),
      emptyState("▦", "La galería está vacía", "Los productos entran en este paso, nunca dentro de la plantilla.", '<button class="button" id="go-generate">Ir a producción</button>'),
      pending,
    ].join("");
    bindStepBar();
    query("#go-generate")?.addEventListener("click", () => navigate("generate"));
    query("#resume-production-task")?.addEventListener("click", () => void resumeCampaignProductionTask());
    return;
  }
  const cards = batch.pieces.map((piece) => [
    '<article class="result-card campaign-result"><a class="result-image" href="', attr(piece.png_url || piece.preview_url), '" target="_blank" rel="noreferrer">',
    '<img src="', attr(piece.preview_url), '" alt="', attr(piece.product + " · " + piece.format), '" loading="lazy" decoding="async"></a>',
    '<div class="result-info"><span class="kicker">FILA ', String(piece.row_number), ' · PROPUESTA ', String(piece.proposal), '</span>',
    '<h3>', esc(piece.product || "Arte sin nombre"), '</h3><p>', esc(piece.template_name), ' · ',
    String(piece.width), '×', String(piece.height), ' · ', esc(piece.format), '</p>',
    piece.warnings.length ? '<div class="notice warning compact">' + esc(piece.warnings.join(" · ")) + '</div>' : '',
    '<div class="button-row"><a class="ghost-button" href="', attr(piece.png_url), '" download>PNG</a>',
    '<a class="ghost-button" href="', attr(piece.jpg_url), '" download>JPG</a>',
    '<a class="ghost-button" href="', attr(piece.psd_url), '" download>PSD por capas</a></div></div></article>',
  ].join("")).join("");
  content().innerHTML = [
    stepBar("results"),
    pageHead(
      "05 · ENTREGABLES",
      String(batch.total_pieces) + " artes listos para revisar",
      "Cada salida conserva producto, plantilla, formato y propuesta. Descarga piezas sueltas o el paquete completo con CSV de estado.",
      '<a class="button large" href="' + attr(batch.zip_url) + '" download>Descargar todo · ZIP</a>',
    ),
    '<div class="stat-row"><div class="stat"><strong>', String(batch.total_rows), '</strong><span>Filas de matriz</span></div>',
    '<div class="stat"><strong>', String(batch.total_pieces), '</strong><span>Artes generados</span></div>',
    '<div class="stat"><strong>', String(new Set(batch.pieces.map((item) => item.template_name)).size), '</strong><span>Plantillas elegidas</span></div>',
    '<div class="stat"><strong>', String(new Set(batch.pieces.map((item) => item.format)).size), '</strong><span>Formatos</span></div></div>',
    batch.warnings.length ? '<div class="notice warning" style="margin-top:16px">' + esc(batch.warnings.join(" · ")) + '</div>' : '',
    '<div class="button-row" style="margin-top:18px"><a class="ghost-button" href="', attr(batch.manifest_url), '" download>CSV de estado</a>',
    '<button class="ghost-button" id="new-production">Nueva tanda</button></div>',
    '<div class="result-grid campaign-result-grid" style="margin-top:22px">', cards, '</div>',
  ].join("");
  bindStepBar();
  query("#new-production")?.addEventListener("click", () => navigate("generate"));
}

/* ------------------------------------------------------------------ visor
   La rejilla compara; el visor revisa. Se guarda la lista en el orden en que se
   pintó la galería para que las flechas recorran lo mismo que se ve. */

interface ViewerItem {
  projectId: string;
  projectName: string;
  variantId: string;
}

let viewerItems: ViewerItem[] = [];
let viewerAt = -1;

function viewerVariant(item: ViewerItem): { project: Project; variant: Variant } | null {
  const project = state.campaign.find((entry) => entry.project_id === item.projectId);
  const variant = project?.variants.find((entry) => entry.id === item.variantId);
  return project && variant ? { project, variant } : null;
}

function openViewer(slot: number): void {
  if (slot < 0 || slot >= viewerItems.length) return;
  viewerAt = slot;
  const overlay = query<HTMLElement>("#viewer")!;
  const found = viewerVariant(viewerItems[slot]);
  if (!found) return;
  const { project, variant } = found;
  const format = variant.meta?.format || {};
  const warnings = variant.quality?.warnings || [];
  const svg = variant.meta?.svg;
  const psd = variant.meta?.psd;

  // El PNG a tamaño real, no la miniatura: es lo que hay que juzgar.
  const image = query<HTMLImageElement>("#viewer-img")!;
  image.src = fileUrl(project.project_id, variant.image);
  image.alt = project.name + " · propuesta " + String(variant.index);

  query<HTMLElement>("#viewer-title")!.textContent = project.name;
  query<HTMLElement>("#viewer-meta")!.textContent = [
    String(variant.width) + "×" + String(variant.height),
    format.platform || variant.format,
    variant.meta?.product_label || "",
    "puntaje " + String(Math.round(variant.quality?.score || 0)) + "/100",
    String(slot + 1) + " de " + String(viewerItems.length),
  ].filter(Boolean).join(" · ");

  query<HTMLElement>("#viewer-actions")!.innerHTML = [
    '<button class="ghost-button" id="viewer-real">Tamaño real</button>',
    '<a class="ghost-button" href="' + attr(variantPngUrl(project.project_id, variant.id)) + '" download>PNG</a>',
    psd ? '<a class="ghost-button" href="' + attr(fileUrl(project.project_id, psd)) + '" download>PSD</a>' : "",
    svg ? '<a class="ghost-button" href="' + attr(fileUrl(project.project_id, svg)) + '" download>SVG</a>' : "",
  ].join("");

  query<HTMLElement>("#viewer-foot")!.innerHTML = [
    "Composición: <strong>" + esc(variant.layout_label) + "</strong>",
    warnings.length
      ? " · " + warnings.map((warning) => "⚠ " + esc(warning)).join(" · ")
      : " · sin avisos",
    '<span class="right"> ← → para pasar · Esc para cerrar</span>',
  ].join("");

  query<HTMLButtonElement>("#viewer-prev")!.disabled = slot === 0;
  query<HTMLButtonElement>("#viewer-next")!.disabled = slot >= viewerItems.length - 1;
  overlay.classList.remove("is-real");
  overlay.hidden = false;
  query<HTMLButtonElement>("#viewer-real")?.addEventListener("click", () => {
    overlay.classList.toggle("is-real");
  });
  query<HTMLButtonElement>("#viewer-close")!.focus();
}

function closeViewer(): void {
  const overlay = query<HTMLElement>("#viewer");
  if (!overlay || overlay.hidden) return;
  overlay.hidden = true;
  // El PNG puede pesar megas: se suelta al cerrar.
  query<HTMLImageElement>("#viewer-img")!.removeAttribute("src");
  viewerAt = -1;
}

function stepViewer(delta: number): void {
  if (viewerAt < 0) return;
  openViewer(Math.min(viewerItems.length - 1, Math.max(0, viewerAt + delta)));
}

/** Se engancha una sola vez: el visor vive fuera del contenido que se repinta. */
function bindViewer(): void {
  query("#viewer-close")?.addEventListener("click", closeViewer);
  query("#viewer-prev")?.addEventListener("click", () => stepViewer(-1));
  query("#viewer-next")?.addEventListener("click", () => stepViewer(1));
  query("#viewer")?.addEventListener("click", (event) => {
    // Pulsar el fondo cierra; pulsar la imagen o la barra, no.
    if ((event.target as HTMLElement).id === "viewer-stage") closeViewer();
  });
  document.addEventListener("keydown", (event) => {
    if (query<HTMLElement>("#viewer")?.hidden) return;
    if (event.key === "Escape") closeViewer();
    else if (event.key === "ArrowLeft") stepViewer(-1);
    else if (event.key === "ArrowRight") stepViewer(1);
    else return;
    event.preventDefault();
  });
}

function bindResults(): void {
  queryAll<HTMLButtonElement>(".result-open").forEach((button) => {
    button.addEventListener("click", () => openViewer(Number(button.dataset.slot)));
  });
  queryAll<HTMLInputElement>(".variant-pick").forEach((checkBox) => {
    checkBox.addEventListener("change", async () => {
      if (checkBox.checked) state.selectedVariants.add(checkBox.value);
      else state.selectedVariants.delete(checkBox.value);
      await renderResults();
    });
  });
  queryAll<HTMLButtonElement>(".result-order").forEach((button) => {
    button.addEventListener("click", async () => {
      state.resultOrder = button.dataset.order as State["resultOrder"];
      await renderResults();
    });
  });
  queryAll<HTMLButtonElement>(".result-filter").forEach((button) => {
    button.addEventListener("click", async () => {
      state.resultFilter = button.dataset.filter as State["resultFilter"];
      await renderResults();
    });
  });
  query("#show-all-results")?.addEventListener("click", async () => {
    state.resultFilter = "all";
    await renderResults();
  });
  query("#refresh-results")?.addEventListener("click", () => renderResults());
  queryAll<HTMLButtonElement>(".export-project").forEach((button) => {
    button.addEventListener("click", () => {
      const projectId = button.dataset.project!;
      const params = new URLSearchParams();
      params.set("include_layers", "true");
      (button.dataset.ids || "").split(",").filter(Boolean).forEach((id) => params.append("variant_ids", id));
      window.location.assign(downloadUrl("/projects/" + projectId + "/export?" + params.toString()));
    });
  });
}
