const API_ROOT = "/api";

/* Identidad de la sesión del navegador.
 *
 * Nada de lo que se produce está pensado para quedarse en el servidor: un PSD
 * de 100 MB deja cientos de MB entre capas, máscaras, fondos y variantes, y
 * acumularlo llena el disco. El backend etiqueta cada proyecto con esta sesión
 * y barre por antigüedad; la interfaz solo lista lo de la sesión en curso.
 *
 * Va en `sessionStorage` únicamente para poder localizar y borrar el trabajo de
 * esta página durante el siguiente arranque. Cada recarga crea una sesión nueva. */
const SESSION_KEY = "creative-session";

function newSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return "s-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
}

export function sessionId(): string {
  try {
    const existing = sessionStorage.getItem(SESSION_KEY);
    if (existing) return existing;
    const created = newSessionId();
    sessionStorage.setItem(SESSION_KEY, created);
    return created;
  } catch {
    // Modo privado sin almacenamiento: una sesión por carga de página.
    return newSessionId();
  }
}

export function resetSessionId(): string {
  try {
    sessionStorage.removeItem(SESSION_KEY);
  } catch {
    /* modo privado: sessionId ya genera una identidad por carga */
  }
  return sessionId();
}

export class ApiError extends Error {
  status: number;
  payload: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

function readableDetail(payload: any): string {
  const detail = payload?.detail ?? payload;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => item?.msg || item?.message || JSON.stringify(item))
      .join(" · ");
  }
  if (typeof detail === "object" && detail) {
    return detail.message || JSON.stringify(detail);
  }
  return String(detail || "El servidor no pudo completar la operación.");
}

function gatewayMessage(status: number): string {
  if (status === 413) {
    return (
      "El archivo es más grande de lo que admite el servidor web, que corta la " +
      "subida antes de que llegue a la aplicación. Déjalo en la carpeta " +
      "data/ingest del servidor y tráelo desde la pestaña «Tomarlo de la " +
      "carpeta compartida», que no pasa por el navegador."
    );
  }
  if (status === 502 || status === 503 || status === 504) {
    return (
      "El servidor no contestó a tiempo. Si era un PSD grande puede seguir " +
      "trabajando: espere un momento y recargue antes de repetir la operación."
    );
  }
  return `El servidor respondió ${status} y no dio detalle.`;
}


export async function request<T = any>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(API_ROOT + path, {
    ...init,
    headers: {
      ...(init.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      "X-Session-Id": sessionId(),
      ...(init.headers || {}),
    },
  });
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json")
    ? await response.json()
    : await response.blob();
  if (!response.ok) {
    // Los proxys de delante (nginx, Caddy) contestan en HTML, no en JSON. Sin
    // esto el mensaje que se veía era "[object Blob]", justo en el error más
    // frecuente al subir un PSD grande, que además tiene solución conocida.
    const message = type.includes("application/json")
      ? readableDetail(payload)
      : gatewayMessage(response.status);
    throw new ApiError(message, response.status, payload);
  }
  return payload as T;
}

export const get = <T = any>(path: string) => request<T>(path);
export const post = <T = any>(path: string, body?: unknown) =>
  request<T>(path, {
    method: "POST",
    body: body instanceof FormData ? body : JSON.stringify(body ?? {}),
  });
export const put = <T = any>(path: string, body: unknown) =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const del = <T = any>(path: string) => request<T>(path, { method: "DELETE" });

export function fileUrl(projectId: string, relative: string): string {
  const safe = relative.split("/").map(encodeURIComponent).join("/");
  return API_ROOT + "/projects/" + projectId + "/files/" + safe;
}

/** Miniatura del KV. No uses `source.path` en las rejillas: es el PNG original
 *  y en un PSD real pesa megas por tarjeta. */
export function thumbnailUrl(projectId: string, maxSide = 420): string {
  return API_ROOT + "/projects/" + projectId + "/thumbnail?max_side=" + String(maxSide);
}

export function variantPngUrl(projectId: string, variantId: string): string {
  return API_ROOT + "/projects/" + projectId + "/variants/" + variantId + "?download=true";
}

export function downloadUrl(path: string): string {
  return API_ROOT + path;
}

export async function pollTask(
  projectId: string,
  taskId: string,
  onProgress: (progress: number, detail: string) => void,
): Promise<any> {
  const started = Date.now();
  while (Date.now() - started < 20 * 60 * 1000) {
    const task = await get<any>("/projects/" + projectId + "/tasks/" + taskId);
    if (task.state === "COMPLETED") return task.result;
    if (task.state === "FAILED") throw new Error(task.error || "La generación falló.");
    const meta = task.meta || {};
    onProgress(Number(meta.progress || 10), meta.status || "Procesando variantes…");
    await new Promise((resolve) => window.setTimeout(resolve, 1200));
  }
  throw new Error("La generación superó el tiempo máximo de espera.");
}
