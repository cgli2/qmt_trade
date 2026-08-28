<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { useApp } from "@/store";
import ToastHost from "@/components/ToastHost.vue";

const app = useApp();
const route = useRoute();

const nav = [
  { group: "总览", items: [{ to: "/", ico: "🏠", label: "工作台" }] },
  {
    group: "智能决策",
    items: [
      { to: "/selection", ico: "🎯", label: "选股研判" },
      { to: "/market", ico: "📈", label: "行情与事件" },
    ],
  },
  {
    group: "交易执行",
    items: [
      { to: "/trade/paper", ico: "🧪", label: "模拟盘" },
      { to: "/trade/live", ico: "🏦", label: "实盘" },
      { to: "/strategy", ico: "🧩", label: "策略管理" },
      { to: "/strategylab", ico: "🧪", label: "策略实验室" },
      { to: "/backtest", ico: "⏳", label: "回测管理" },
      { to: "/risk", ico: "🛡️", label: "风控管理" },
    ],
  },
  {
    group: "运维与系统",
    items: [
      { to: "/reports", ico: "📄", label: "绩效报告" },
      { to: "/settings", ico: "⚙️", label: "系统设置" },
    ],
  },
];

const title = computed(() => route.meta.title || "控制台");
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
        <button class="theme-toggle" @click="toggleTheme">{{ app.theme === "dark" ? "☀️" : "🌙" }}</button>
      </header>
      <main class="content">
        <router-view />
      </main>
    </div>
    <ToastHost />
  </div>
</template>
