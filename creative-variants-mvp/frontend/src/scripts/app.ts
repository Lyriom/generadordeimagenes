import {
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
import type {
  Capabilities,
  FormatPreset,
  Layer,
  ProductGroup,
  Project,
  ProjectSummary,
  Variant,
  ViewName,
} from "./types";

const VIEW_LABELS: Record<ViewName, string> = {
  campaign: "Cargar KV",
  layers: "Revisar capas",
  products: "Productos",
  generate: "Generar",
  results: "Resultados",
};

/* El flujo es una secuencia, no cuatro secciones independientes: no se puede
   elegir productos sin haber confirmado las capas, ni generar sin productos.
   `stepState` calcula qué pasos están hechos y cuál es el siguiente, y la
   navegación bloquea los que aún no tocan explicando por qué. */
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

function stepState(): StepState {
  const hasCampaign = state.campaign.length > 0;
  const reviewed = hasCampaign && state.campaign.every(layersConfirmed);
  const pending = hasCampaign ? state.campaign.filter((item) => !layersConfirmed(item)) : [];
  const hasProducts = productsReady();

  const done: Record<string, boolean> = {
    campaign: hasCampaign,
    layers: reviewed,
    products: hasProducts,
    generate: false,
  };
  const blocked: Record<string, string | null> = {
    campaign: null,
    layers: hasCampaign ? null : "Primero carga al menos un KV.",
    products: !hasCampaign
      ? "Primero carga al menos un KV."
      : !reviewed
        ? "Falta confirmar las capas de " + String(pending.length) + " KV."
        : null,
    generate: !hasCampaign
      ? "Primero carga al menos un KV."
      : !reviewed
        ? "Falta confirmar las capas de " + String(pending.length) + " KV."
        : !hasProducts
          ? "Elige al menos un producto o una combinación."
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
  products: File[];
  individualProducts: Set<string>;
  groups: ProductGroup[];
  generationMode: "catalog" | "compose";
  selectedVariants: Set<string>;
  autoFormats: boolean;
  resultOrder: "score" | "generation";
  /* Posición de los productos individuales dentro del arte. Vive en el estado y
     no en el DOM porque se elige en el paso 3 y se usa al generar en el 4. */
  productArrangement: ProductGroup["arrangement"];
  /* Lo que informó el barrido de retención: horas de vida y disco ocupado. */
  retention: {
    retention_hours?: number;
    max_projects_kept?: number;
    disk_usage_mb?: number;
    removed_count?: number;
  } | null;
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
  products: [],
  individualProducts: new Set(),
  groups: [],
  generationMode: "catalog",
  selectedVariants: new Set(),
  autoFormats: true,
  resultOrder: "score",
  productArrangement: "auto",
  retention: null,
};

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
  const item = document.createElement("div");
  item.className = "toast " + kind;
  item.textContent = message;
  region.append(item);
  window.setTimeout(() => item.remove(), kind === "error" ? 8000 : 4200);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
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

/* La campaña en curso vive en `sessionStorage`, no en `localStorage`: recargar
   la pestaña no pierde el trabajo, pero abrir el generador de cero empieza
   limpio en el paso 1 en vez de resucitar la campaña de la semana pasada. */
function saveSession(): void {
  try {
    sessionStorage.setItem("creative-campaign", JSON.stringify(state.campaignIds));
    if (state.activeId) sessionStorage.setItem("creative-active", state.activeId);
    else sessionStorage.removeItem("creative-active");
  } catch {
    /* modo privado: la campaña solo vive en memoria */
  }
}

function readSession(): { ids: string[]; active: string | null } {
  try {
    const raw = sessionStorage.getItem("creative-campaign");
    const ids = raw ? JSON.parse(raw) : [];
    return {
      ids: Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string") : [],
      active: sessionStorage.getItem("creative-active"),
    };
  } catch {
    return { ids: [], active: null };
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
  return project;
}

async function refreshAll(): Promise<void> {
  // El listado va acotado a la sesión: el trabajo de sesiones anteriores no se
  // ofrece porque el servidor lo borra por antigüedad.
  const [health, capabilities, projects] = await Promise.all([
    get<any>("/health"),
    get<Capabilities>("/capabilities"),
    get<ProjectSummary[]>("/projects?session=" + encodeURIComponent(sessionId())),
  ]);
  state.health = health;
  state.capabilities = capabilities;
  state.projects = projects;

  const existing = new Set(projects.map((item) => item.project_id));
  state.campaignIds = state.campaignIds.filter((id) => existing.has(id));
  if (!state.campaignIds.length && state.activeId && existing.has(state.activeId)) {
    state.campaignIds = [state.activeId];
  }
  state.campaign = await Promise.all(
    state.campaignIds.map((id) => get<Project>("/projects/" + id)),
  );
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
    connected
      ? "FastAPI " + esc(state.health.version || "")
      : "Revisa backend y red",
    "</small></div>",
  ].join("");
  const live = query<HTMLElement>(".live-pill")!;
  // El texto va envuelto: en móvil el CSS oculta el <span> y deja solo el punto.
  live.innerHTML = '<i></i><span>' + (connected ? "Motor conectado" : "Sin conexión") + "</span>";

  const project = activeProject();
  const panel = query<HTMLElement>("#sidebar-project")!;
  panel.innerHTML = project
    ? [
        '<span class="eyebrow">CAMPAÑA · ', String(state.campaign.length), " KV</span>",
        "<strong>", esc(project.name), "</strong>",
        "<small>", String(project.canvas.width), "×", String(project.canvas.height),
        " · ", String(project.variants.length), " variantes</small>",
      ].join("")
    : '<span class="eyebrow">SIN CAMPAÑA</span><strong>Carga tu primer KV</strong><small>PSD, PSB, PNG o JPG</small>';
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

function projectCard(project: Project | ProjectSummary, full?: Project): string {
  const id = project.project_id;
  const canvas = project.canvas;
  const layerCount = "layers" in project && typeof project.layers === "number"
    ? project.layers
    : full?.layers.length || 0;
  const variantCount = "variants" in project && typeof project.variants === "number"
    ? project.variants
    : full?.variants.length || 0;
  // Siempre hay miniatura: el endpoint la genera desde el KV y la cachea, así
  // que funciona igual para un proyecto abierto y para uno solo listado.
  const image = '<img src="' + attr(thumbnailUrl(id, 420)) + '" alt="' + attr(project.name) +
    '" loading="lazy" decoding="async">';
  return [
    '<article class="project-card', state.activeId === id ? " is-active" : "", '" data-project="', attr(id), '">',
    '<div class="project-thumb">', image, "</div>",
    '<div class="project-info"><strong>', esc(project.name), "</strong>",
    '<div class="project-meta"><span>', String(canvas.width), "×", String(canvas.height),
    "</span><span>", String(layerCount), " capas · ", String(variantCount), " artes</span></div></div>",
    '<div class="project-actions"><button class="ghost-button open-project" data-id="', attr(id), '">Abrir</button>',
    '<button class="danger-button delete-project" data-id="', attr(id), '">Borrar</button></div></article>',
  ].join("");
}

/** Aviso permanente de que esto no es un archivo: el trabajo se borra. */
function retentionNotice(): string {
  const info = state.retention;
  const hours = info?.retention_hours ?? 8;
  const disk = info?.disk_usage_mb;
  return [
    '<div class="notice warning"><strong>Nada de esto se guarda.</strong> El trabajo vive solo durante tu sesión y el servidor lo borra tras ',
    String(hours), " horas sin actividad, para no llenarse.",
    disk !== undefined ? " Ocupado ahora: <strong>" + String(disk) + " MB</strong>." : "",
    ' Descarga lo que quieras conservar desde <em>Resultados</em>.',
    '<div class="button-row" style="margin-top:10px"><button class="danger-button" id="wipe-all">Borrar todo del servidor ahora</button></div></div>',
  ].join("");
}

function bindRetention(): void {
  query("#wipe-all")?.addEventListener("click", async () => {
    if (!window.confirm(
      "Se borrará TODO el trabajo del servidor, incluido el de otras sesiones abiertas. ¿Continuar?",
    )) return;
    busy("Liberando el servidor", "Borrando proyectos y sus archivos…", 20);
    try {
      const result = await del<any>("/projects");
      state.campaignIds = [];
      state.campaign = [];
      state.activeId = null;
      saveSession();
      await refreshAll();
      toast(String(result.removed_count || 0) + " proyecto(s) borrados. Disco: " +
        String(result.disk_usage_mb ?? 0) + " MB.", "success");
      await navigate("campaign");
    } catch (error) {
      toast(errorMessage(error), "error");
    } finally {
      idle();
    }
  });
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
  const activeCards = state.campaign
    .map((project) => projectCard(project, project))
    .join("");
  const saved = state.projects.filter((item) => !state.campaignIds.includes(item.project_id));

  content().innerHTML = [
    stepBar("campaign"),
    pageHead(
      "Creative workspace",
      state.campaign.length ? "Tu campaña está lista" : "Empieza con tus key visuals",
      "Carga uno o varios KV. El PSD conserva sus capas reales; PNG y JPG también funcionan con separación asistida.",
      state.campaign.length
        ? '<button class="ghost-button" id="clear-campaign">Cambiar campaña</button>'
        : "",
    ),
    state.campaign.length
      ? [
          '<div class="stat-row">',
          '<div class="stat"><strong>', String(state.campaign.length), '</strong><span>KV activos</span></div>',
          '<div class="stat"><strong>', String(state.campaign.reduce((n, p) => n + p.layers.length, 0)), '</strong><span>Capas detectadas</span></div>',
          '<div class="stat"><strong>', String(state.campaign.reduce((n, p) => n + p.variants.length, 0)), '</strong><span>Artes generadas</span></div>',
          '<div class="stat"><strong>', String(state.capabilities?.format_catalog.length || 0), '</strong><span>Presets disponibles</span></div>',
          "</div>",
          '<h2 class="section-title">KV de esta campaña</h2><div class="project-grid">', activeCards, "</div>",
          '<div class="spacer"></div><div class="button-row"><button class="ghost-button" id="add-more-kv">Añadir más KV</button></div>',
        ].join("")
      : [
          '<div class="grid two">',
          '<section class="card elevated"><div class="card-head"><div><h2>Subir archivos</h2><p>Hasta 300 MB por archivo</p></div><span class="badge">RECOMENDADO</span></div>',
          '<label class="dropzone" id="artwork-drop"><input id="artwork-files" type="file" multiple accept=".psd,.psb,.png,.jpg,.jpeg">',
          '<span class="drop-icon">⇧</span><strong>Arrastra tus KV aquí</strong><span>PSD, PSB, PNG o JPG · puedes elegir varios</span></label>',
          '<div id="upload-file-list" class="file-list"></div>',
          '<div class="form-grid" style="margin-top:14px"><label class="field"><span>Logo opcional</span><input id="logo-file" type="file" accept=".png,.jpg,.jpeg"></label>',
          '<label class="field"><span>Tipografía opcional</span><input id="font-file" type="file" accept=".ttf,.otf"></label></div>',
          '<label class="check" style="margin:14px 0"><input id="import-layers" type="checkbox" checked> Importar todas las capas reales del PSD</label>',
          '<button class="button large full" id="upload-campaign" disabled>Crear campaña</button></section>',
          '<section class="card dark"><div class="card-head"><div><h2>Desde carpeta del servidor</h2><p>Ideal para PSD de 60–100 MB</p></div><span class="badge green">RÁPIDO</span></div>',
          '<div id="ingest-list" class="stack"><div class="loader"></div></div>',
          '<button class="button rose full" id="import-ingest" disabled>Importar seleccionados</button></section>',
          "</div>",
        ].join(""),
    state.campaign.length ? stepFooter("campaign", "Revisar capas") : "",
    '<div class="spacer"></div>',
    savedProjectsHtml(saved),
    '<div class="spacer"></div>',
    retentionNotice(),
  ].join("");

  bindStepBar();
  bindStepFooter("campaign");
  bindSavedProjects();
  bindRetention();
  bindProjectCards();
  query("#clear-campaign")?.addEventListener("click", () => {
    state.campaignIds = [];
    state.campaign = [];
    state.activeId = null;
    saveSession();
    navigate("campaign");
  });
  query("#add-more-kv")?.addEventListener("click", () => {
    state.campaignIds = [];
    state.campaign = [];
    saveSession();
    navigate("campaign");
  });
  if (!state.campaign.length) {
    bindUpload();
    await loadIngest();
  }
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
    const message = ids.length === 1
      ? "¿Borrar este proyecto y sus archivos?"
      : "¿Borrar " + String(ids.length) + " proyectos y todos sus archivos?";
    if (!window.confirm(message)) return;
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
      if (!window.confirm("¿Borrar este proyecto y sus archivos?")) return;
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

function bindUpload(): void {
  const input = query<HTMLInputElement>("#artwork-files")!;
  const button = query<HTMLButtonElement>("#upload-campaign")!;
  const list = query<HTMLElement>("#upload-file-list")!;
  input.addEventListener("change", () => {
    const files = Array.from(input.files || []);
    button.disabled = files.length === 0;
    list.innerHTML = files.map((file) =>
      '<div class="file-chip"><strong>' + esc(file.name) + '</strong><span>' +
      (file.size / 1024 / 1024).toFixed(1) + " MB</span></div>"
    ).join("");
  });
  button.addEventListener("click", async () => {
    const files = Array.from(input.files || []);
    if (!files.length) return;
    const logo = query<HTMLInputElement>("#logo-file")?.files?.[0];
    const font = query<HTMLInputElement>("#font-file")?.files?.[0];
    const importLayers = query<HTMLInputElement>("#import-layers")!.checked;
    const created: Project[] = [];
    busy("Importando campaña", "Leyendo capas y preparando vistas previas…", 5);
    try {
      for (let index = 0; index < files.length; index += 1) {
        const artwork = files[index];
        busyProgress(5 + Math.round((index / files.length) * 80), "Importando " + artwork.name);
        const data = new FormData();
        data.append("artwork", artwork);
        data.append("name", artwork.name.replace(/\.[^.]+$/, ""));
        data.append("import_layers", String(importLayers));
        if (logo) data.append("logo", logo);
        if (font) data.append("font", font);
        if (/\.(psd|psb)$/i.test(artwork.name)) {
          const result = await post<any>("/projects/split", data);
          created.push(...result.projects);
        } else {
          created.push(await post<Project>("/projects", data));
        }
      }
      state.campaign = created;
      state.campaignIds = created.map((project) => project.project_id);
      state.activeId = state.campaignIds[0] || null;
      saveSession();
      await refreshAll();
      toast("Campaña importada correctamente.", "success");
      await navigate("layers");
    } catch (error) {
      toast(errorMessage(error), "error");
    } finally {
      idle();
    }
  });
}

async function loadIngest(): Promise<void> {
  const host = query<HTMLElement>("#ingest-list");
  const button = query<HTMLButtonElement>("#import-ingest");
  if (!host || !button) return;
  try {
    const result = await get<any>("/ingest?with_pieces=true");
    const files = result.files || [];
    if (!files.length) {
      host.innerHTML = '<div class="notice">La carpeta de ingesta está vacía.</div>';
      return;
    }
    host.innerHTML = files.map((file: any) => [
      '<label class="choice"><input class="ingest-check" type="checkbox" value="', attr(file.path), '">',
      '<span><strong>', esc(file.name), '</strong><br><small>', String(file.width), "×",
      String(file.height), " · ", String(file.size_mb), " MB · ", String(file.pieces || 1),
      " pieza(s)</small></span></label>",
    ].join("")).join("");
    queryAll<HTMLInputElement>(".ingest-check").forEach((check) => {
      check.addEventListener("change", () => {
        button.disabled = !query<HTMLInputElement>(".ingest-check:checked");
      });
    });
    button.addEventListener("click", async () => {
      const selectedPaths = queryAll<HTMLInputElement>(".ingest-check:checked").map((item) => item.value);
      const created: Project[] = [];
      busy("Importando desde servidor", "Analizando archivos grandes…", 8);
      try {
        for (let index = 0; index < selectedPaths.length; index += 1) {
          const path = selectedPaths[index];
          busyProgress(8 + Math.round(index / selectedPaths.length * 80), "Importando " + path);
          if (/\.(psd|psb)$/i.test(path)) {
            const result = await post<any>("/projects/from-ingest/split", {
              source: path, import_layers: true,
            });
            created.push(...result.projects);
          } else {
            created.push(await post<Project>("/projects/from-ingest", {
              source: path, import_layers: true,
            }));
          }
        }
        state.campaign = created;
        state.campaignIds = created.map((project) => project.project_id);
        state.activeId = state.campaignIds[0] || null;
        saveSession();
        await refreshAll();
        toast("Campaña importada correctamente.", "success");
        await navigate("layers");
      } catch (error) {
        toast(errorMessage(error), "error");
      } finally {
        idle();
      }
    });
  } catch (error) {
    host.innerHTML = '<div class="notice error">' + esc(errorMessage(error)) + "</div>";
  }
}

export async function mountApp(): Promise<void> {
  const session = readSession();
  state.campaignIds = session.ids;
  state.activeId = session.active;

  // Barrido al abrir: libera el disco del servidor sin depender de reinicios.
  // Borra por antigüedad, así que no toca la campaña que otro esté produciendo.
  try {
    state.retention = await post<any>("/projects/purge");
  } catch {
    /* si falla, el arranque del backend ya hace su propio barrido */
  }

  queryAll<HTMLButtonElement>(".nav-item").forEach((button) => {
    button.addEventListener("click", () => navigate(button.dataset.view as ViewName));
  });
  query("#mobile-menu")?.addEventListener("click", () => document.body.classList.toggle("menu-open"));
  query("#refresh-button")?.addEventListener("click", async () => {
    try {
      await refreshAll();
      await navigate(state.view);
      toast("Información actualizada.", "success");
    } catch (error) {
      toast(errorMessage(error), "error");
    }
  });

  try {
    await refreshAll();
    // Se retoma donde se quedó el flujo, que con una campaña ya revisada es el
    // paso de productos y no la pantalla de carga otra vez.
    await navigate(state.campaign.length ? stepState().next : "campaign");
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

function layerItem(project: Project, layer: Layer): string {
  const preview = layer.src
    ? '<img class="layer-mini" src="' + attr(fileUrl(project.project_id, layer.src)) + '" alt="">'
    : '<span class="layer-mini"></span>';
  return [
    '<button class="layer-item', state.selectedLayerId === layer.id ? " is-active" : "",
    '" data-layer-id="', attr(layer.id), '">', preview, "<span><strong>", esc(layer.name),
    "</strong><small>", esc(CATEGORY_LABELS[layer.category] || layer.category), " · ",
    String(layer.width), "×", String(layer.height), layer.visible ? "" : " · oculta",
    "</small></span></button>",
  ].join("");
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
  const projectOptions = state.campaign.map((item) =>
    '<option value="' + attr(item.project_id) + '"' + selected(item.project_id === project.project_id) + ">" + esc(item.name) + "</option>"
  ).join("");
  const layers = [...project.layers]
    .filter((item) => item.category !== "background")
    .sort((a, b) => b.z_index - a.z_index);
  const layerList = layers.map((item) => layerItem(project, item)).join("");
  const preview = layer
    ? [
        '<div class="layer-preview"><img id="mask-source" src="/api/projects/', attr(project.project_id),
        "/preview/mask/", attr(layer.id), '" alt="Máscara de ', attr(layer.name), '">',
        '<canvas id="mask-canvas" class="mask-canvas" hidden></canvas></div>',
        '<div class="button-row" style="margin-top:12px"><button class="ghost-button" id="draw-mask">Dibujar máscara</button>',
        '<button class="ghost-button" id="auto-segment">Auto-segmentar</button>',
        '<button class="ghost-button" id="reset-mask">Usar rectángulo</button></div>',
        '<div id="mask-tools" class="card soft" style="margin-top:12px" hidden>',
        '<div class="form-grid"><label class="field"><span>Pincel</span><input id="brush-size" type="range" min="2" max="60" value="16"></label>',
        '<label class="field"><span>Modo</span><select id="brush-mode"><option value="add">Añadir</option><option value="subtract">Borrar</option></select></label></div>',
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
    '<details class="card"><summary>Ajustes finos de la capa seleccionada · opcional</summary>',
    '<p class="muted tiny" style="margin-top:10px">Máscaras, geometría y permisos. Solo si algo salió mal en la importación.</p>',
    '<div class="button-row" style="margin:14px 0 16px"><label class="field" style="min-width:260px"><span>KV activo</span><select id="active-kv">', projectOptions, "</select></label>",
    '<button class="ghost-button" id="analyze-project">Detectar elementos</button>',
    '<button class="ghost-button" id="extract-layers">Extraer PNG</button>',
    '<button class="ghost-button" id="rebuild-background">Reconstruir fondo</button>',
    '<button class="ghost-button" id="show-detections">Ver detecciones</button></div>',
    '<div class="layer-workbench">',
    '<section class="card flush"><div class="card-head" style="padding:16px 16px 0"><div><h2>Capas</h2><p>', String(layers.length), " elementos</p></div>",
    '<button class="icon-button" id="new-layer" title="Crear capa">+</button></div><div class="layer-list">', layerList || '<div class="notice">Sin capas.</div>', "</div></section>",
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
  bindLayerActions(project, layer, layers);
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
    return [
      '<button class="kv-chip', isCurrent ? " is-current" : "", done ? " is-done" : "",
      '" data-kv="', attr(project.project_id), '" title="', attr(project.name), '">',
      '<img src="', attr(thumbnailUrl(project.project_id, 120)), '" alt="" loading="lazy" decoding="async">',
      '<span class="kv-chip-copy"><strong>', esc(project.name), "</strong><small>",
      String(project.canvas.width), "×", String(project.canvas.height), " · ",
      done ? "confirmado" : "sin confirmar", "</small></span>",
      '<i class="kv-chip-mark" aria-hidden="true">', done ? "✓" : String(index + 1), "</i>",
      "</button>",
    ].join("");
  }).join("");
  const done = state.campaign.filter(layersConfirmed).length;
  return [
    '<section class="card" style="margin-bottom:18px"><div class="card-head"><div><h2>KV que estás revisando</h2>',
    '<p>Cada uno se confirma por separado. Al guardar se salta al siguiente que falte.</p></div>',
    '<span class="badge', done === state.campaign.length ? " green" : "", '">', String(done), " DE ",
    String(state.campaign.length), "</span></div>",
    '<div class="kv-switch">', chips, "</div></section>",
  ].join("");
}

/** Miniatura de la capa. Sin verla no se puede decidir su función. */
function roleThumb(project: Project, layer: Layer): string {
  if (layer.src) {
    return '<img class="role-thumb" src="' + attr(fileUrl(project.project_id, layer.src)) +
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
    '<div class="button-row" style="margin-top:18px">',
    '<button class="button large" id="confirm-roles" style="flex:1;min-width:260px">',
    'Guardar y confirmar este KV</button>',
    others > 0
      ? '<button class="ghost-button large" id="apply-to-all" title="Copia estas mismas funciones a las capas con el mismo nombre en los demás KV">' +
        "Aplicar a los otros " + String(others) + " KV</button>"
      : "",
    "</div></section>",
  ].join("");
}

function bindLayerActions(project: Project, layer: Layer | null, layers: Layer[]): void {
  query<HTMLSelectElement>("#active-kv")?.addEventListener("change", async (event) => {
    state.activeId = (event.currentTarget as HTMLSelectElement).value;
    state.selectedLayerId = null;
    saveSession();
    await navigate("layers");
  });
  queryAll<HTMLButtonElement>(".layer-item").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedLayerId = button.dataset.layerId || null;
      renderLayers();
    });
  });
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
    const modelOptions = (state.capabilities?.image_models || []).map((model) =>
      '<option value="' + attr(model.id) + '">' + esc(model.label) + "</option>"
    ).join("");
    content().insertAdjacentHTML("afterbegin", [
      '<section class="card elevated" id="background-panel" style="margin-bottom:18px"><div class="card-head"><div><h2>Reconstruir fondo</h2><p>Elige el motor sin modificar las capas</p></div><button class="icon-button" id="close-background">×</button></div>',
      '<div class="form-grid"><label class="field"><span>Motor</span><select id="background-engine"><option value="auto">Automático</option><option value="opencv">Local · OpenCV</option>',
      modelOptions ? '<option value="magnific">Magnific</option>' : "", '<option value="openai">OpenAI</option></select></label>',
      '<label class="field"><span>Modelo Magnific</span><select id="background-model"><option value="">Predeterminado</option>', modelOptions, "</select></label></div>",
      '<label class="field" style="margin-top:12px"><span>Dirección visual</span><textarea id="background-prompt" placeholder="Fondo limpio, sin texto ni logos"></textarea></label>',
      '<div class="form-grid" style="margin-top:12px"><label class="field"><span>Expansión de máscara</span><input id="background-dilate" type="number" min="0" max="64" value="8"></label>',
      '<button class="button" id="run-background">Reconstruir</button></div></section>',
    ].join(""));
    query("#close-background")?.addEventListener("click", () => query("#background-panel")?.remove());
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

  query("#confirm-roles")?.addEventListener("click", async () => {
    busy("Guardando revisión", "Actualizando funciones de las capas…", 35);
    try {
      await put("/projects/" + project.project_id + "/layers", {
        updates: readRoleSelections(),
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
    // Las piezas de un mismo PSD repiten la estructura de capas, así que
    // revisar 8 KV a mano son 80 desplegables. Aquí se copia por nombre.
    const selections = readRoleSelections();
    const roleByName = new Map<string, Record<string, any>>();
    layers.forEach((layer) => {
      const match = selections.find((item) => item.id === layer.id);
      if (match) roleByName.set(layer.name, match);
    });

    const others = state.campaign.filter((item) => item.project_id !== project.project_id);
    busy("Copiando la revisión", "Aplicando las mismas funciones al resto…", 5);
    let applied = 0;
    const unmatched: string[] = [];
    try {
      // El KV actual también se guarda: si no, quedaría como el único sin confirmar.
      await put("/projects/" + project.project_id + "/layers", { updates: selections, delete: [] });
      await refreshProject(project.project_id);

      for (let index = 0; index < others.length; index += 1) {
        const target = others[index];
        busyProgress(8 + Math.round((index / Math.max(1, others.length)) * 88), target.name);
        const updates = target.layers
          .filter((layer) => layer.category !== "background" && roleByName.has(layer.name))
          .map((layer) => ({ ...roleByName.get(layer.name)!, id: layer.id }));
        const missing = target.layers.filter(
          (layer) => layer.category !== "background" && !roleByName.has(layer.name),
        );
        if (missing.length) unmatched.push(target.name);
        if (!updates.length) continue;
        await put("/projects/" + target.project_id + "/layers", { updates, delete: [] });
        await refreshProject(target.project_id);
        applied += 1;
      }
      toast("Revisión copiada a " + String(applied) + " KV.", "success");
      if (unmatched.length) {
        toast(
          "Revisa a mano las capas con otro nombre en: " + unmatched.join(", ") + ".",
          "info",
        );
      }
      const pendiente = state.campaign.find((item) => !layersConfirmed(item));
      if (pendiente) {
        state.activeId = pendiente.project_id;
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
    if (!window.confirm("¿Eliminar la capa " + layer.name + "?")) return;
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
  query("#draw-mask")?.addEventListener("click", () => enableMaskCanvas(project, layer));
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

function enableMaskCanvas(project: Project, layer: Layer): void {
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
  return [
    '<label class="format-card"><input class="format-check" type="checkbox" value="', attr(spec.id), '"',
    checked(state.selectedFormats.has(spec.id)), '><span class="format-shape" style="', attr(style), '"><i class="safe-zone"></i></span>',
    '<span class="format-copy"><strong>', esc(spec.placement), '</strong><span>', String(spec.width), "×", String(spec.height), " · ", esc(spec.ratio),
    '</span><span>', esc(spec.platform), spec.recommended ? " · recomendado" : "", "</span></span></label>",
  ].join("");
}

function formatSelectorHtml(allowAuto: boolean): string {
  const catalog = state.capabilities?.format_catalog || [];
  const platforms = ["Todos", ...Array.from(new Set(catalog.map((item) => item.platform)))];
  const visible = state.formatPlatform === "Todos"
    ? catalog
    : catalog.filter((item) => item.platform === state.formatPlatform);
  const filters = platforms.map((platform) =>
    '<button type="button" class="platform-filter' + (platform === state.formatPlatform ? " is-active" : "") +
    '" data-platform="' + attr(platform) + '">' + esc(platform) + "</button>"
  ).join("");
  return [
    '<section class="card"><div class="card-head"><div><h2>Formatos de salida</h2><p>Ubicaciones reales con sus áreas seguras</p></div><span class="badge">',
    String(state.selectedFormats.size), " ELEGIDOS</span></div>",
    allowAuto ? '<label class="choice" style="margin-bottom:14px"><input id="auto-formats" type="checkbox"' + checked(state.autoFormats) + '> Automático · original + formatos sociales</label>' : "",
    '<div id="manual-formats"', allowAuto && state.autoFormats ? " hidden" : "", '><div class="format-platforms">', filters, '</div><div class="format-grid">',
    visible.map(formatCard).join(""), "</div></div>",
    '<p class="muted tiny" style="margin:14px 0 0">Las líneas blancas marcan dónde deben quedar logo, producto, copy y legales.</p></section>',
  ].join("");
}

function productCard(file: File): string {
  const key = productKey(file);
  return [
    '<article class="product-card"><img src="', attr(productUrl(file)), '" alt="', attr(productName(file)), '">',
    '<strong>', esc(file.name), '</strong><label class="check"><input class="individual-product" type="checkbox" value="', attr(key), '"',
    checked(state.individualProducts.has(key)), "> Generar por separado</label></article>",
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

/* ---------------------------------------------------------------- paso 3
   Productos: qué se pone, si va individual o en combinación, y en qué posición.
   El paso 4 solo decide el cómo (modelo, contexto, formatos). */
async function renderProducts(): Promise<void> {
  const missingTargets = state.campaign.filter((project) => !productTarget(project));
  const productCards = state.products.map(productCard).join("");
  const groups = state.groups.map(groupCard).join("");
  const productChoices = state.products.map((file) =>
    '<label class="choice"><input class="group-member" type="checkbox" value="' + attr(productKey(file)) + '"> ' + esc(productName(file)) + "</label>"
  ).join("");
  const individuals = selectedProductFiles().length;

  const targetsNotice = missingTargets.length
    ? '<div class="notice warning" style="margin-bottom:16px"><strong>Falta identificar el producto en ' + String(missingTargets.length) + ' KV.</strong> El sistema necesita saber qué pieza retirar para poner la nueva.<div class="button-row" style="margin-top:10px">' +
      missingTargets.map((project) => '<button class="ghost-button detect-product" data-project="' + attr(project.project_id) + '">Detectar en ' + esc(project.name) + "</button>").join("") + "</div></div>"
    : '<div class="notice success" style="margin-bottom:16px">Los ' + String(state.campaign.length) + ' KV tienen su producto identificado: se retirará el original y entrará el nuevo.</div>';

  content().innerHTML = [
    stepBar("products"),
    pageHead(
      "Paso 3 de 4",
      "Qué producto va en la plantilla",
      "Sube el catálogo una sola vez. Después eliges cuáles llevan arte propio, cuáles se combinan y en qué posición.",
    ),
    targetsNotice,
    '<section class="card elevated"><div class="card-head"><div><h2>Catálogo de productos</h2><p>PNG con transparencia da el mejor recorte</p></div><span class="badge green">',
    String(state.products.length), " CARGADOS</span></div>",
    '<label class="dropzone compact"><input id="product-files" type="file" multiple accept=".png,.jpg,.jpeg,.webp"><span class="drop-icon">⇧</span><strong>Sube los productos</strong><span>PNG, JPG o WEBP · puedes elegir varios a la vez</span></label>',
    productCards
      ? '<div class="product-grid" style="margin-top:16px">' + productCards + "</div>"
      : '<div class="notice" style="margin-top:16px">Aún no has cargado productos.</div>',
    "</section>",

    '<div class="spacer"></div>',
    '<div class="grid two">',
    '<section class="card"><div class="card-head"><div><h2>Individual o en combinación</h2><p>Puedes usar las dos cosas a la vez</p></div></div>',
    '<div class="notice" style="margin-bottom:14px">Marcado <strong>“Generar por separado”</strong> en una tarjeta = ese producto tendrá su propio arte. Ahora mismo: <strong>',
    String(individuals), " individual(es)</strong> y <strong>", String(validGroups().length), " combinación(es)</strong>.</div>",
    state.products.length >= 2 ? [
      '<div class="stack"><label class="field"><span>Nombre de la combinación</span><input id="group-name" placeholder="Ej. Combo familiar"></label>',
      '<div><span class="label">Productos que van juntos</span><div class="choice-row" style="margin-top:8px">', productChoices, "</div></div>",
      '<label class="field"><span>Posición de estos productos</span><select id="group-arrangement">',
      optionList(ARRANGEMENT_OPTIONS, "auto"), "</select></label>",
      '<button class="button" id="add-group">Añadir combinación</button></div>',
    ].join("") : '<div class="notice">Carga al menos dos productos para poder combinarlos.</div>',
    groups ? '<div class="stack" style="margin-top:16px">' + groups + "</div>" : "",
    "</section>",

    '<section class="card"><div class="card-head"><div><h2>Posición de los productos</h2><p>Para los que van por separado</p></div></div>',
    '<label class="field"><span>Disposición dentro del arte</span><select id="individual-arrangement">',
    optionList(ARRANGEMENT_OPTIONS, state.productArrangement), "</select></label>",
    '<p class="muted tiny" style="margin-top:12px">La ubicación exacta se toma de la zona que ya venía diseñada en el PSD; esto solo decide cómo se reparten cuando hay más de una pieza.</p>',
    '<div class="divider"></div>',
    '<span class="label">Vista previa de la disposición</span>',
    '<div class="group-preview ', attr(state.productArrangement === "auto" ? "horizontal" : state.productArrangement), '" style="margin-top:10px">',
    (selectedProductFiles().slice(0, 3).map((file) => '<img src="' + attr(productUrl(file)) + '" alt="">').join("")
      || '<span class="muted tiny">Sube productos para verlo</span>'),
    "</div></section></div>",

    stepFooter("products", "Elegir modelo y generar"),
  ].join("");

  bindStepBar();
  bindStepFooter("products");
  bindProductStep();
}

function bindProductStep(): void {
  query<HTMLInputElement>("#product-files")?.addEventListener("change", async (event) => {
    const files = Array.from((event.currentTarget as HTMLInputElement).files || []);
    state.products = files;
    // Por defecto todos llevan arte individual, como en el flujo anterior.
    state.individualProducts = new Set(files.map(productKey));
    state.groups = [];
    await renderProducts();
  });
  queryAll<HTMLInputElement>(".individual-product").forEach((checkBox) => {
    checkBox.addEventListener("change", async () => {
      if (checkBox.checked) state.individualProducts.add(checkBox.value);
      else state.individualProducts.delete(checkBox.value);
      await renderProducts();
    });
  });
  query<HTMLSelectElement>("#individual-arrangement")?.addEventListener("change", async (event) => {
    state.productArrangement = (event.currentTarget as HTMLSelectElement).value as ProductGroup["arrangement"];
    await renderProducts();
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
        await renderProducts();
      } catch (error) {
        toast(errorMessage(error), "error");
      } finally {
        idle();
      }
    });
  });
}

/* ---------------------------------------------------------------- paso 4
   Cómo se genera: formatos, modelo, contexto y el botón final. */
async function renderGenerate(): Promise<void> {
  const active = activeProject()!;
  const modeTabs = [
    '<div class="tabs"><button class="tab generation-mode', state.generationMode === "catalog" ? " is-active" : "", '" data-mode="catalog">Producir por producto</button>',
    '<button class="tab generation-mode', state.generationMode === "compose" ? " is-active" : "", '" data-mode="compose">Ajustes finos del KV activo</button></div>',
  ].join("");

  let body = "";
  if (state.generationMode === "catalog") {
    const total = (selectedProductFiles().length + validGroups().length) * state.campaign.length;
    body = [
      '<div class="notice success" style="margin-bottom:16px">Se producirán <strong>', String(total),
      " tanda(s)</strong>: ", String(selectedProductFiles().length), " individual(es) y ",
      String(validGroups().length), " combinación(es) por cada uno de los ", String(state.campaign.length), " KV.</div>",
      formatSelectorHtml(true),
      '<div class="spacer"></div>', generationOptionsHtml("catalog"),
    ].join("");
  } else {
    body = [
      '<div class="notice" style="margin-bottom:16px">Ajustes finos sobre <strong>', esc(active.name), '</strong>: recompone las capas actuales sin usar el catálogo de productos.</div>',
      '<div class="grid two">', formatSelectorHtml(false), generationOptionsHtml("compose"), "</div>",
      '<div class="spacer"></div>', permissionsHtml(active),
    ].join("");
  }
  content().innerHTML = [
    stepBar("generate"),
    pageHead(
      "Paso 4 de 4",
      "Modelo, contexto y formatos",
      "Elige las medidas de salida, con qué motor se rehace el fondo y qué le pides en palabras. Después, genera.",
    ),
    modeTabs, body,
  ].join("");
  bindStepBar();
  bindGenerate(active);
}

function generationOptionsHtml(mode: "catalog" | "compose"): string {
  const imageModels = state.capabilities?.image_models || [];
  const models = imageModels.map((model) => '<option value="' + attr(model.id) + '">' + esc(model.label || model.id) + "</option>").join("");
  const countMin = mode === "catalog" ? 2 : 4;
  const countMax = mode === "catalog" ? 6 : 30;
  const countValue = mode === "catalog" ? 3 : 12;
  const layouts = (state.capabilities?.layouts || []).map((layout) =>
    '<label class="choice"><input class="layout-check" type="checkbox" value="' + attr(layout.key) + '"> ' + esc(layout.label) + "</label>"
  ).join("");
  return [
    // El modelo y el contexto ya no viven dentro de un acordeón cerrado: eran
    // justo las dos decisiones que el usuario no encontraba.
    '<section class="card elevated"><div class="card-head"><div><h2>Modelo y contexto</h2><p>Con qué motor se rehace el fondo y qué le pides</p></div></div>',
    '<label class="choice" style="margin-bottom:14px"><input id="regenerate-background" type="checkbox"> Rehacer el fondo con IA</label>',
    '<div class="form-grid"><label class="field"><span>Motor del fondo</span><select id="generation-provider">',
    '<option value="opencv">Local · gratis</option><option value="magnific">Magnific · eliges el modelo (con costo)</option>',
    '<option value="openai">OpenAI · IA de imagen (con costo)</option><option value="auto">Automático</option></select></label>',
    '<label class="field"><span>Modelo de IA</span><select id="generation-model"><option value="">Predeterminado</option>', models,
    '</select><small>Solo aplica con Magnific.</small></label></div>',
    '<label class="field" style="margin-top:14px"><span>Contexto · qué quieres del arte</span>',
    '<textarea id="generation-instruction" placeholder="Producto grande, titular arriba, composición minimal"></textarea>',
    "<small>Entiende: producto grande o pequeño, titular arriba, centrado, vertical, diagonal, dividido, izquierda, derecha, minimal.</small></label>",
    '<label class="field" style="margin-top:14px"><span>Dirección visual del fondo · opcional</span>',
    '<textarea id="generation-background-prompt" placeholder="Fondo deportivo premium, luces azules, profundidad, sin texto ni logos"></textarea>',
    "<small>Solo se usa si se rehace el fondo.</small></label></section>",

    '<div class="spacer"></div>',
    '<section class="card"><div class="card-head"><div><h2>Variación</h2><p>Cuántas propuestas y cuánto pueden alejarse</p></div></div>',
    '<div class="form-grid"><label class="field"><span>Cantidad de propuestas</span><input id="generation-count" type="number" min="', String(countMin), '" max="', String(countMax), '" value="', String(countValue), '"></label>',
    '<label class="field"><span>Cuánto se pueden alejar del original</span><select id="generation-intensity">',
    '<option value="conservative">Parecidas al original</option><option value="moderate" selected>Equilibradas</option>',
    '<option value="creative">Muy distintas entre sí</option></select></label></div>',
    '<label class="field" style="margin-top:14px"><span>Semilla</span><input id="generation-seed" type="number" min="0" max="2147483647" value="42"><small>La misma semilla repite el mismo resultado.</small></label>',
    mode === "compose" && layouts ? '<div style="margin-top:14px"><span class="label">Familias de layout · vacío = todas</span><div class="choice-row" style="margin-top:8px">' + layouts + "</div></div>" : "",
    "</section>",
    '<button class="button large full" id="run-generation" style="margin-top:20px">✦ ',
    mode === "catalog" ? "Generar artes por producto" : "Generar variantes", "</button>",
  ].join("");
}

function permissionsHtml(project: Project): string {
  const layers = project.layers.filter((layer) => layer.category !== "background");
  const rows = layers.map((layer) => [
    '<tr><td><strong>', esc(layer.name), '</strong><br><span class="muted">', esc(CATEGORY_LABELS[layer.category] || layer.category), "</span></td>",
    permissionCell("lock", layer, layer.locked), permissionCell("move", layer, layer.movable), permissionCell("resize", layer, layer.resizable),
    permissionCell("reorder", layer, layer.reorderable), permissionCell("hide", layer, !layer.visible), "</tr>",
  ].join("")).join("");
  return [
    '<section class="card"><div class="card-head"><div><h2>Permisos por capa</h2><p>Define exactamente qué puede cambiar el compositor</p></div></div>',
    '<div class="inventory"><table><thead><tr><th>Capa</th><th>Bloqueada</th><th>Mover</th><th>Escalar</th><th>Reordenar</th><th>Ocultar</th></tr></thead><tbody>', rows, "</tbody></table></div></section>",
  ].join("");
}

function permissionCell(kind: string, layer: Layer, value: boolean): string {
  return '<td><input class="permission-' + attr(kind) + '" data-layer="' + attr(layer.id) + '" type="checkbox"' + checked(value) + "></td>";
}

function bindFormatSelector(): void {
  query<HTMLInputElement>("#auto-formats")?.addEventListener("change", (event) => {
    state.autoFormats = (event.currentTarget as HTMLInputElement).checked;
    query<HTMLElement>("#manual-formats")!.hidden = state.autoFormats;
  });
  queryAll<HTMLButtonElement>(".platform-filter").forEach((button) => {
    button.addEventListener("click", async () => {
      state.formatPlatform = button.dataset.platform || "Todos";
      await renderGenerate();
    });
  });
  queryAll<HTMLInputElement>(".format-check").forEach((checkBox) => {
    checkBox.addEventListener("change", () => {
      if (checkBox.checked) state.selectedFormats.add(checkBox.value);
      else state.selectedFormats.delete(checkBox.value);
      const badge = query<HTMLElement>(".card .badge");
      if (badge && badge.textContent?.includes("ELEGIDOS")) badge.textContent = String(state.selectedFormats.size) + " ELEGIDOS";
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
  bindFormatSelector();
  query("#run-generation")?.addEventListener("click", () => {
    if (state.generationMode === "catalog") runCatalogGeneration();
    else runComposeGeneration(project);
  });
}

function generationSettings(): Record<string, any> {
  return {
    count: Number(query<HTMLInputElement>("#generation-count")!.value),
    formats: state.autoFormats ? null : Array.from(state.selectedFormats),
    intensity: query<HTMLSelectElement>("#generation-intensity")!.value,
    instruction: query<HTMLTextAreaElement>("#generation-instruction")!.value.trim() || null,
    seed: Number(query<HTMLInputElement>("#generation-seed")!.value),
    // La posición se eligió en el paso 3, no en esta pantalla.
    product_arrangement: state.productArrangement,
    background_provider: query<HTMLSelectElement>("#generation-provider")!.value,
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
  const targets = state.campaign.map((project) => ({ project, layer: productTarget(project) }));
  if (targets.some((item) => !item.layer)) {
    toast("Todos los KV necesitan una capa Producto antes de generar.", "error");
    return;
  }
  const settings = generationSettings();
  const total = targets.length * (individuals.length + validGroups.length);
  let completed = 0;
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
          hide_others: true, append: false, arrangement: state.productArrangement,
        });
        (replaced.warnings || []).forEach((warning: string) => toast(warning, "info"));
        const task = await post<any>("/projects/" + project.project_id + "/auto", {
          ...settings,
          seed: settings.seed + kvIndex * 100 + index,
          replace_existing: firstBatch,
          product_label: productName(file),
          product_arrangement: state.productArrangement,
          template_mode: true,
          regenerate_background: settings.regenerate_background && firstBatch,
        });
        const result = await pollTask(project.project_id, task.task_id, (progress, detail) => {
          const batchProgress = (completed + progress / 100) / Math.max(1, total) * 100;
          busyProgress(batchProgress, project.name + " · " + detail);
        });
        (result.warnings || []).forEach((warning: string) => toast(warning, "info"));
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
          (replaced.warnings || []).forEach((warning: string) => toast(warning, "info"));
        }
        const label = group.files.map(productName).join(" + ");
        const task = await post<any>("/projects/" + project.project_id + "/auto", {
          ...settings,
          seed: settings.seed + kvIndex * 100 + individuals.length + groupIndex,
          replace_existing: firstBatch,
          product_label: label,
          product_arrangement: group.arrangement,
          template_mode: true,
          regenerate_background: settings.regenerate_background && firstBatch,
        });
        const result = await pollTask(project.project_id, task.task_id, (progress, detail) => {
          const batchProgress = (completed + progress / 100) / Math.max(1, total) * 100;
          busyProgress(batchProgress, project.name + " · " + detail);
        });
        (result.warnings || []).forEach((warning: string) => toast(warning, "info"));
        firstBatch = false;
        completed += 1;
      }
      await refreshProject(project.project_id);
    }
    state.selectedVariants.clear();
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
  const payload = {
    count: settings.count,
    seed: settings.seed,
    formats: Array.from(state.selectedFormats),
    intensity: settings.intensity,
    instruction: settings.instruction,
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
  const svg = variant.meta?.svg;
  const product = variant.meta?.product_label || "Producto";
  // El backend ya renderiza un JPEG reducido por variante; pedir el PNG a
  // tamaño real para una rejilla de 280px era descargar megas por tarjeta.
  const preview = variant.thumbnail || variant.image;
  return [
    '<article class="result-card"><div class="result-image"><img src="', attr(fileUrl(project.project_id, preview)), '" alt="Variante ', String(variant.index), '" loading="lazy" decoding="async"><span class="score">',
    String(Math.round(variant.quality?.score || 0)), '</span></div><div class="result-copy"><strong>', String(variant.width), "×", String(variant.height), '</strong><p>',
    esc((format.platform || "") + (format.placement ? " · " + format.placement : " · " + variant.format) + (format.ratio ? " · " + format.ratio : "")), "<br>", esc(product), "</p>",
    '<label class="check"><input class="variant-pick" type="checkbox" value="', attr(key), '"', checked(state.selectedVariants.has(key)), '> Elegir</label>',
    '<div class="button-row" style="margin-top:11px"><a class="ghost-button" href="', attr(variantPngUrl(project.project_id, variant.id)), '" download>PNG</a>',
    svg ? '<a class="ghost-button" href="' + attr(fileUrl(project.project_id, svg)) + '" download>Illustrator SVG</a>' : '<span class="badge gray">SVG al regenerar</span>',
    '</div><details style="margin-top:12px"><summary>', warnings.length ? "⚠ " + String(warnings.length) + " avisos" : "Detalle", '</summary><div class="notice" style="margin-top:8px">Composición: ',
    esc(variant.layout_label), warnings.length ? "<br>" + warnings.map((warning) => "• " + esc(warning)).join("<br>") : "<br>Sin avisos.", "</div></details></div></article>",
  ].join("");
}

async function renderResults(): Promise<void> {
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
  const sections = projects.filter((project) => project.variants.length).map((project) => {
    const variants = [...project.variants].sort(state.resultOrder === "score"
      ? (a, b) => (b.quality?.score || 0) - (a.quality?.score || 0)
      : (a, b) => a.index - b.index);
    const picked = variants.filter((variant) => state.selectedVariants.has(resultKey(project.project_id, variant.id))).map((variant) => variant.id);
    return [
      '<section style="margin-top:28px"><div class="card-head"><div><h2>', esc(project.name), '</h2><p>', String(variants.length), ' propuestas</p></div>',
      '<button class="button export-project" data-project="', attr(project.project_id), '" data-ids="', attr(picked.join(",")), '">Descargar ', picked.length ? String(picked.length) + " elegidas" : "todas", " · ZIP</button></div>",
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
    sections,
  ].join("");
  bindStepBar();
  bindResults();
}

function bindResults(): void {
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
