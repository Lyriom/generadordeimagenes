import { del, downloadUrl, fileUrl, get, pollTask, post, put, variantPngUrl } from "./api";
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
  campaign: "Campaña",
  layers: "Capas y KV",
  generate: "Generar",
  results: "Resultados",
};

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

function saveSession(): void {
  localStorage.setItem("creative-campaign", JSON.stringify(state.campaignIds));
  if (state.activeId) localStorage.setItem("creative-active", state.activeId);
}

function sourceUrl(project: Project): string {
  return fileUrl(project.project_id, project.source.path);
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
  const [health, capabilities, projects] = await Promise.all([
    get<any>("/health"),
    get<Capabilities>("/capabilities"),
    get<ProjectSummary[]>("/projects"),
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
  queryAll<HTMLButtonElement>(".nav-item").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === state.view);
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
  state.view = view;
  renderChrome();
  document.body.classList.remove("menu-open");
  content().innerHTML = '<div class="loading-state"><div class="loader"></div><strong>Cargando</strong></div>';
  try {
    if (view === "campaign") await renderCampaign();
    if (view === "layers") await renderLayers();
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
  const image = full
    ? '<img src="' + attr(sourceUrl(full)) + '" alt="' + attr(full.name) + '" loading="lazy">'
    : '<span class="empty-icon">◇</span>';
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

async function renderCampaign(): Promise<void> {
  const activeCards = state.campaign
    .map((project) => projectCard(project, project))
    .join("");
  const savedCards = state.projects
    .filter((item) => !state.campaignIds.includes(item.project_id))
    .map((item) => projectCard(item))
    .join("");

  content().innerHTML = [
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
          '<div class="spacer"></div><div class="button-row"><button class="button large" id="go-layers">Revisar capas →</button>',
          '<button class="ghost-button" id="add-more-kv">Añadir más KV</button></div>',
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
    '<h2 class="section-title">Trabajos guardados</h2>',
    savedCards
      ? '<div class="project-grid">' + savedCards + "</div>"
      : '<div class="notice">No hay otros proyectos guardados.</div>',
  ].join("");

  bindProjectCards();
  query("#clear-campaign")?.addEventListener("click", () => {
    state.campaignIds = [];
    state.campaign = [];
    state.activeId = null;
    saveSession();
    navigate("campaign");
  });
  query("#go-layers")?.addEventListener("click", () => navigate("layers"));
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
  try {
    state.campaignIds = JSON.parse(localStorage.getItem("creative-campaign") || "[]");
  } catch {
    state.campaignIds = [];
  }
  state.activeId = localStorage.getItem("creative-active");

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
    await navigate("campaign");
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

  content().innerHTML = [
    pageHead("Control de producción", "Capas y key visual", "Corrige categorías, máscaras, geometría y comportamiento sin alterar el archivo original."),
    '<div class="button-row" style="margin-bottom:16px"><label class="field" style="min-width:260px"><span>KV activo</span><select id="active-kv">', projectOptions, "</select></label>",
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
    '<div class="grid two" style="margin-top:18px">',
    orderEditor(layers),
    reviewRoles(layers),
    "</div>",
    '<div style="margin-top:18px">', inventoryHtml(project), "</div>",
  ].join("");

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

function reviewRoles(layers: Layer[]): string {
  const rows = [...layers].sort((a, b) => a.z_index - b.z_index).map((layer) => {
    const current = layer.visible ? layer.category : "ignore";
    return [
      '<label class="field"><span>', esc(layer.name), '</span><select class="role-select" data-id="',
      attr(layer.id), '">', optionList(ROLE_LABELS, current), "</select></label>",
    ].join("");
  }).join("");
  return [
    '<section class="card"><div class="card-head"><div><h2>Función de cada capa</h2><p>Confirmación humana para generar</p></div></div>',
    '<div class="stack">', rows, '</div><button class="button full" id="confirm-roles" style="margin-top:14px">Guardar y confirmar</button></section>',
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
  query("#confirm-roles")?.addEventListener("click", async () => {
    const updates = queryAll<HTMLSelectElement>(".role-select").map((selectBox) => {
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
    busy("Guardando revisión", "Actualizando funciones de las capas…", 35);
    try {
      await put("/projects/" + project.project_id + "/layers", { updates, delete: [] });
      await refreshProject(project.project_id);
      toast("Capas confirmadas.", "success");
      await renderLayers();
    } catch (error) {
      toast(errorMessage(error), "error");
    } finally {
      idle();
    }
  });
  query("#new-layer")?.addEventListener("click", () => showNewLayer(project));
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

async function renderGenerate(): Promise<void> {
  if (!state.campaign.length) {
    content().innerHTML = emptyState("✦", "Primero abre una campaña", "Necesitamos al menos un KV antes de generar.", '<button class="button" id="go-campaign">Ir a campaña</button>');
    query("#go-campaign")?.addEventListener("click", () => navigate("campaign"));
    return;
  }
  const missingTargets = state.campaign.filter((project) => !productTarget(project));
  const productCards = state.products.map(productCard).join("");
  const groups = state.groups.map(groupCard).join("");
  const productChoices = state.products.map((file) =>
    '<label class="choice"><input class="group-member" type="checkbox" value="' + attr(productKey(file)) + '"> ' + esc(productName(file)) + "</label>"
  ).join("");
  const active = activeProject()!;
  const modeTabs = [
    '<div class="tabs"><button class="tab generation-mode', state.generationMode === "catalog" ? " is-active" : "", '" data-mode="catalog">Catálogo de productos</button>',
    '<button class="tab generation-mode', state.generationMode === "compose" ? " is-active" : "", '" data-mode="compose">Componer capas actuales</button></div>',
  ].join("");
  const targetsNotice = missingTargets.length
    ? '<div class="notice warning" style="margin-bottom:16px"><strong>Falta identificar el producto en ' + String(missingTargets.length) + ' KV.</strong> Usa “Detectar producto” para crear la capa reemplazable.<div class="button-row" style="margin-top:10px">' +
      missingTargets.map((project) => '<button class="ghost-button detect-product" data-project="' + attr(project.project_id) + '">Detectar en ' + esc(project.name) + "</button>").join("") + "</div></div>"
    : '<div class="notice success" style="margin-bottom:16px">Todos los KV tienen una capa de producto preparada para reemplazo.</div>';

  let body = "";
  if (state.generationMode === "catalog") {
    body = [
      targetsNotice,
      '<div class="grid two"><section class="card elevated"><div class="card-head"><div><h2>1 · Productos</h2><p>PNG, JPG o WEBP; mejor con transparencia</p></div><span class="badge green">LOCALES</span></div>',
      '<label class="dropzone compact"><input id="product-files" type="file" multiple accept=".png,.jpg,.jpeg,.webp"><span class="drop-icon">⇧</span><strong>Sube los productos</strong><span>Puedes crear artes separados y combinaciones</span></label>',
      productCards ? '<div class="product-grid" style="margin-top:14px">' + productCards + "</div>" : '<div class="notice" style="margin-top:14px">Aún no has cargado productos.</div>',
      '</section><section class="card"><div class="card-head"><div><h2>2 · Combinaciones</h2><p>Cada producto seguirá siendo una capa independiente</p></div></div>',
      state.products.length >= 2 ? [
        '<div class="stack"><label class="field"><span>Nombre de la combinación</span><input id="group-name" placeholder="Ej. Combo familiar"></label>',
        '<div><span class="label">Productos que van juntos</span><div class="choice-row" style="margin-top:8px">', productChoices, "</div></div>",
        '<label class="field"><span>Disposición</span><select id="group-arrangement"><option value="auto">Automática según formato</option><option value="horizontal">En fila</option><option value="vertical">Apilados</option><option value="overlap">Superpuestos</option></select></label>',
        '<button class="button" id="add-group">Añadir combinación</button></div>',
      ].join("") : '<div class="notice">Carga al menos dos productos para crear una combinación.</div>',
      groups ? '<div class="stack" style="margin-top:16px">' + groups + "</div>" : "",
      "</section></div>",
      '<div class="spacer"></div>', formatSelectorHtml(true),
      '<div class="spacer"></div>', generationOptionsHtml("catalog"),
    ].join("");
  } else {
    body = [
      '<div class="notice" style="margin-bottom:16px">Ajustes finos sobre <strong>', esc(active.name), '</strong>. Conserva el flujo avanzado existente.</div>',
      '<div class="grid two">', formatSelectorHtml(false), generationOptionsHtml("compose"), "</div>",
      '<div class="spacer"></div>', permissionsHtml(active),
    ].join("");
  }
  content().innerHTML = [
    pageHead("Producción multiformato", "Genera sin perder el control", "Elige qué productos van separados, cuáles juntos y las medidas exactas de Meta, Google Ads y YouTube."),
    modeTabs, body,
  ].join("");
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
    '<section class="card elevated"><div class="card-head"><div><h2>', mode === "catalog" ? "3 · Producción" : "Configuración", '</h2><p>Variación, fondo y semilla reproducible</p></div></div>',
    '<div class="form-grid"><label class="field"><span>Cantidad de propuestas</span><input id="generation-count" type="number" min="', String(countMin), '" max="', String(countMax), '" value="', String(countValue), '"></label>',
    '<label class="field"><span>Intensidad</span><select id="generation-intensity"><option value="conservative">Conservadora</option><option value="moderate" selected>Moderada</option><option value="creative">Creativa</option></select></label></div>',
    '<div class="form-grid" style="margin-top:14px"><label class="field"><span>Semilla</span><input id="generation-seed" type="number" min="0" max="2147483647" value="42"></label>',
    '<label class="field"><span>Disposición de varios productos</span><select id="generation-arrangement"><option value="auto">Automática</option><option value="horizontal">En fila</option><option value="vertical">Apilados</option><option value="overlap">Superpuestos</option></select></label></div>',
    '<label class="field" style="margin-top:14px"><span>Pedido en palabras · opcional</span><textarea id="generation-instruction" placeholder="Producto grande, titular arriba, composición minimal"></textarea></label>',
    mode === "compose" && layouts ? '<div style="margin-top:14px"><span class="label">Familias de layout · vacío = todas</span><div class="choice-row" style="margin-top:8px">' + layouts + "</div></div>" : "",
    '<details style="margin-top:18px"><summary>Reconstrucción del fondo</summary><div class="stack" style="margin-top:14px">',
    '<label class="choice"><input id="regenerate-background" type="checkbox"> Rehacer fondo durante la primera tanda</label>',
    '<div class="form-grid"><label class="field"><span>Motor</span><select id="generation-provider"><option value="opencv">Local · OpenCV</option><option value="auto">Automático</option><option value="magnific">Magnific</option><option value="openai">OpenAI</option></select></label>',
    '<label class="field"><span>Modelo Magnific</span><select id="generation-model"><option value="">Predeterminado</option>', models, "</select></label></div>",
    '<label class="field"><span>Dirección visual del fondo</span><textarea id="generation-background-prompt" placeholder="Fondo premium sin texto ni logos"></textarea></label></div></details>',
    '<button class="button large full" id="run-generation" style="margin-top:20px">✦ ', mode === "catalog" ? "Generar artes por producto" : "Generar variantes", "</button></section>",
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
  query<HTMLInputElement>("#product-files")?.addEventListener("change", async (event) => {
    const files = Array.from((event.currentTarget as HTMLInputElement).files || []);
    state.products = files;
    state.individualProducts = new Set(files.map(productKey));
    state.groups = [];
    await renderGenerate();
  });
  queryAll<HTMLInputElement>(".individual-product").forEach((checkBox) => {
    checkBox.addEventListener("change", () => {
      if (checkBox.checked) state.individualProducts.add(checkBox.value);
      else state.individualProducts.delete(checkBox.value);
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
    await renderGenerate();
  });
  queryAll<HTMLButtonElement>(".remove-group").forEach((button) => {
    button.addEventListener("click", async () => {
      state.groups = state.groups.filter((group) => group.id !== button.dataset.group);
      await renderGenerate();
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
        await renderGenerate();
      } catch (error) {
        toast(errorMessage(error), "error");
      } finally {
        idle();
      }
    });
  });
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
    product_arrangement: query<HTMLSelectElement>("#generation-arrangement")!.value,
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
          hide_others: true, append: false, arrangement: "auto",
        });
        (replaced.warnings || []).forEach((warning: string) => toast(warning, "info"));
        const task = await post<any>("/projects/" + project.project_id + "/auto", {
          ...settings,
          seed: settings.seed + kvIndex * 100 + index,
          replace_existing: firstBatch,
          product_label: productName(file),
          product_arrangement: "auto",
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
  return [
    '<article class="result-card"><div class="result-image"><img src="', attr(fileUrl(project.project_id, variant.image)), '" alt="Variante ', String(variant.index), '" loading="lazy"><span class="score">',
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
    pageHead("Galería de campaña", "Resultados listos para revisar", "Compara piezas, marca tus favoritas y descarga PNG + PSD + SVG editable para Illustrator."),
    '<div class="stat-row"><div class="stat"><strong>', String(total), '</strong><span>Propuestas</span></div><div class="stat"><strong>',
    String(Math.round(allScores.reduce((sum, value) => sum + value, 0) / Math.max(1, allScores.length))), '</strong><span>Calidad promedio</span></div><div class="stat"><strong>',
    String(Math.round(Math.max(...allScores))), '</strong><span>Mejor puntaje</span></div><div class="stat"><strong id="selected-count">', String(selectedCount), '</strong><span>Elegidas</span></div></div>',
    '<div class="button-row" style="margin-top:18px"><span class="label">Orden:</span><button class="platform-filter result-order', state.resultOrder === "score" ? " is-active" : "", '" data-order="score">Mejores primero</button>',
    '<button class="platform-filter result-order', state.resultOrder === "generation" ? " is-active" : "", '" data-order="generation">Orden de generación</button><button class="ghost-button right" id="refresh-results">↻ Actualizar</button></div>',
    sections,
  ].join("");
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
