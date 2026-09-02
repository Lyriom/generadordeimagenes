const API_ROOT = "/api";

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

export async function request<T = any>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(API_ROOT + path, {
    ...init,
    headers: {
      ...(init.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(init.headers || {}),
    },
  });
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json")
    ? await response.json()
    : await response.blob();
  if (!response.ok) throw new ApiError(readableDetail(payload), response.status, payload);
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
