import { defineStore } from "pinia";
import { ref } from "vue";
import api from "./api";

export const useTasks = defineStore("tasks", () => {
  const jobs = ref<any[]>([]);
  const notifications = ref<any[]>([]);
  const error = ref("");
  let timer: ReturnType<typeof setTimeout> | undefined;
  let active = false;
  let inflight: Promise<void> | undefined;
  function refresh() {
    if (inflight) return inflight;
    inflight = (async () => {
      try {
        const [j, n] = await Promise.all([api.jobs(50), api.notifications()]);
        jobs.value = j; notifications.value = n; error.value = "";
      } catch (e: any) { error.value = e.message || "任务中心连接失败"; }
      finally { inflight = undefined; }
    })();
    return inflight;
  }
  async function poll() {
    await refresh();
    if (!active) return;
    const running = jobs.value.some(j => ["pending", "running"].includes(j.status));
    timer = setTimeout(poll, document.hidden ? 30000 : running ? 2000 : 15000);
  }
  function start() { if (!active) { active = true; void poll(); } }
  function stop() { active = false; if (timer) clearTimeout(timer); }
  return { jobs, notifications, error, refresh, start, stop };
});
