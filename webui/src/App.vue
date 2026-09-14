<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { useApp } from "@/store";
import TaskCenter from "@/components/TaskCenter.vue";
import api from "@/api";
import { tryReq } from "@/toast";
import ToastHost from "@/components/ToastHost.vue";

const app = useApp();
const route = useRoute();

const nav = [{ group: "日常工作", items: [
  { to: "/", ico: "🏠", label: "工作台" },
  { to: "/trade", ico: "🏦", label: "交易" },
  { to: "/strategy", ico: "🧩", label: "策略" },
  { to: "/backtest", ico: "⏳", label: "回测" },
  { to: "/market", ico: "📈", label: "行情与选股" },
  { to: "/settings", ico: "⚙️", label: "设置" },
]}];
async function emergencyStop() {
  await tryReq(() => api.setKillswitch(app.mode, "engage", "顶部紧急停止：禁止新增仓位"), "已请求停止开仓");
}

const title = computed(() => route.meta.title || "控制台");
// 交易页把"模式"收敛到内部响应式切换（不重建组件），其余页面保持原有"切模式即刷新"的行为
const viewKey = computed(() => (route.path.startsWith("/trade") ? "trade" : app.mode));
const modes = ["paper", "live"];
const modeLabels: Record<string, string> = {
  paper: "模拟盘",
  live: "实盘",
};

function toggleTheme() {
  app.setTheme(app.theme === "dark" ? "light" : "dark");
}
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-logo" aria-hidden="true">
          <!-- 上升 K 线三联：交易主题 Logo -->
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M6.5 11.5v8M12 7.5v9.5M17.5 3.5v8.5" stroke="#fff" stroke-width="1.7" stroke-linecap="round" />
            <rect x="4.5" y="13.4" width="4" height="3.8" rx="1.1" fill="#fff" />
            <rect x="10" y="9.6" width="4" height="4.6" rx="1.1" fill="#fff" />
            <rect x="15.5" y="5.2" width="4" height="4.8" rx="1.1" fill="#fff" />
          </svg>
        </span>
        <span class="brand-text">QMT 交易控制台<small>LLM 驱动 · A股自动交易</small></span>
      </div>
      <template v-for="g in nav" :key="g.group">
        <div class="nav-group-title">{{ g.group }}</div>
        <router-link
          v-for="it in g.items"
          :key="it.to"
          :to="it.to"
          class="nav-item"
          :class="{ active: route.path === it.to }"
        >
          <span class="ico">{{ it.ico }}</span><span>{{ it.label }}</span>
        </router-link>
      </template>
    </aside>

    <div class="main">
      <header class="topbar">
        <h1>{{ title }}</h1>
        <span class="badge info">模式</span>
        <select class="mode" :value="app.mode" @change="app.setMode(($event.target as HTMLSelectElement).value)">
          <option v-for="m in modes" :key="m" :value="m">{{ modeLabels[m] ?? m }}</option>
        </select>
        <strong v-if="app.mode === 'live'" class="badge danger">实盘账户</strong>
        <TaskCenter />
        <button class="btn danger" @click="emergencyStop">停止开仓</button>
        <button class="theme-toggle" @click="toggleTheme">{{ app.theme === "dark" ? "☀️" : "🌙" }}</button>
      </header>
      <main class="content">
        <router-view :key="viewKey" />
      </main>
    </div>
    <ToastHost />
  </div>
</template>
