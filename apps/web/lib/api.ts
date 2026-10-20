import type { User } from "@firebase/auth";
import { signOut } from "@firebase/auth";
import { auth } from "@/lib/firebase";

const apiUnavailableMessage =
  "Can't reach the Roofline API. Check that the FastAPI service is running and NEXT_PUBLIC_API_BASE_URL points to it.";

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function tokenFor(user: User, forceRefresh: boolean): Promise<string> {
  return user.getIdToken(forceRefresh);
}

async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(input, init);
  } catch (cause) {
    if (cause instanceof TypeError) {
      throw new ApiError(0, "api_unavailable", apiUnavailableMessage);
    }
    throw cause;
  }
}

async function requestWithToken<T>(user: User, path: string, init: RequestInit, forceRefresh: boolean): Promise<T> {
  const token = await tokenFor(user, forceRefresh);
  const response = await apiFetch(path, {
    ...init,
    headers: { ...init.headers, Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (response.status === 401 && !forceRefresh) return requestWithToken<T>(user, path, init, true);
  if (response.status === 401 && forceRefresh) {
    if (auth) await signOut(auth);
    throw new ApiError(401, "invalid_token", "Your session expired. Sign in again.");
  }
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail = typeof body === "object" && body !== null && "error" in body ? body.error
      : typeof body === "object" && body !== null && "detail" in body ? body.detail : null;
    const message = typeof detail === "object" && detail !== null && "message" in detail && typeof detail.message === "string"
      ? detail.message : `Request failed (${response.status}).`;
    const code = typeof detail === "object" && detail !== null && "code" in detail && typeof detail.code === "string" ? detail.code : "request_failed";
    throw new ApiError(response.status, code, message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function apiRequest<T>(user: User, path: string, init: RequestInit = {}): Promise<T> {
  return requestWithToken<T>(user, path, init, false);
}

export function uploadPdf(user: User, projectId: string, file: File, onProgress: (value: number) => void): Promise<{ id: string; status: string; duplicate: boolean; jobId: string | null }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let refreshed = false;
    const send = async (forceRefresh: boolean): Promise<void> => {
      try {
        const token = await tokenFor(user, forceRefresh);
        const body = new FormData();
        body.append("file", file);
        body.append("autoExtract", "true");
        xhr.open("POST", `/api/projects/${encodeURIComponent(projectId)}/reports`);
        xhr.setRequestHeader("Authorization", `Bearer ${token}`);
        xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(Math.round(event.loaded * 100 / event.total)); };
        xhr.onerror = () => reject(new ApiError(0, "api_unavailable", apiUnavailableMessage));
        xhr.onload = () => {
          if (xhr.status === 401 && !refreshed) {
            refreshed = true;
            void send(true);
            return;
          }
          if (xhr.status === 401 && auth) void signOut(auth);
          if (xhr.status < 200 || xhr.status >= 300) {
            let message = `Upload failed (${xhr.status}).`;
            let code = "upload_failed";
            try {
              const parsed: unknown = JSON.parse(xhr.responseText);
              if (typeof parsed === "object" && parsed !== null && ("error" in parsed || "detail" in parsed)) {
                const detail = "error" in parsed ? parsed.error : parsed.detail;
                if (typeof detail === "object" && detail !== null && "message" in detail && typeof detail.message === "string") message = detail.message;
                if (typeof detail === "object" && detail !== null && "code" in detail && typeof detail.code === "string") code = detail.code;
              }
            } catch { /* Keep the generic response. */ }
            reject(new ApiError(xhr.status, code, message));
            return;
          }
          resolve(JSON.parse(xhr.responseText) as { id: string; status: string; duplicate: boolean; jobId: string | null });
        };
        xhr.send(body);
      } catch (error) {
        reject(error);
      }
    };
    void send(false);
  });
}

export async function downloadWithAuth(user: User, path: string): Promise<Blob> {
  const token = await user.getIdToken();
  let response = await apiFetch(path, { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" });
  if (response.status === 401) {
    response = await apiFetch(path, { headers: { Authorization: `Bearer ${await user.getIdToken(true)}` }, cache: "no-store" });
  }
  if (response.status === 401 && auth) await signOut(auth);
  if (!response.ok) throw new ApiError(response.status, "download_failed", "The file could not be downloaded.");
  return response.blob();
}
