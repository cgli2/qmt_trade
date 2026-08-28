import { reactive } from "vue";

export const toasts = reactive<{ id: number; text: string; type: string }[]>([]);
let seq = 1;

export function pushToast(text: string, type: "ok" | "err" | "info" = "info") {
  const id = seq++;
  toasts.push({ id, text, type });
  setTimeout(() => {
    const i = toasts.findIndex((t) => t.id === id);
    if (i >= 0) toasts.splice(i, 1);
  }, 4000);
}

export async function tryReq(fn: () => Promise<any>, okMsg?: string): Promise<any> {
  try {
    const r = await fn();
    if (okMsg) pushToast(okMsg, "ok");
    return r;
  } catch (e: any) {
    pushToast(e?.message || String(e), "err");
    return undefined;
  }
}
