import { defineStore } from "pinia";
import { ref } from "vue";

export const useApp = defineStore("app", () => {
  // 默认 paper（真实数据 + 模拟撮合，绝不下真单）；旧 qmt_mode(sim) 不再读取，避免残留卡在模拟数据
  const mode = ref<string>(localStorage.getItem("qmt_mode_v2") || "paper");
  const theme = ref<string>(localStorage.getItem("qmt_theme") || "light");
  const apiBase = ref<string>(
    (import.meta as any).env?.VITE_API_BASE || "http://localhost:7099"
  );

  function setMode(m: string) {
    mode.value = m;
    localStorage.setItem("qmt_mode_v2", m);
  }
  function setTheme(t: string) {
    theme.value = t;
    localStorage.setItem("qmt_theme", t);
    document.documentElement.setAttribute("data-theme", t);
  }
  function applyTheme() {
    document.documentElement.setAttribute("data-theme", theme.value);
  }
  return { mode, theme, apiBase, setMode, setTheme, applyTheme };
});
