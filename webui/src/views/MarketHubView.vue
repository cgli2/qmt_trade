<script setup lang="ts">
import { defineAsyncComponent, provide, ref, watch } from "vue";
import { useRoute } from "vue-router";
const Market = defineAsyncComponent(() => import("./MarketView.vue"));
const Selection = defineAsyncComponent(() => import("./SelectionView.vue"));
const route = useRoute();
const tab = ref("market");

// 供子视图（如 SelectionView）主动切换 Hub 内部 tab：从选股研判点击个股时，若仅 router.push
// 更新 query，Hub 不会重挂载、tab 仍是 selection，MarketView 因 v-if 未挂载导致 K 线窗口打不开。
// 子视图 inject 后可先切 tab 再 push，确保 MarketView 立即挂载并响应深链。
provide("marketHubSetTab", (t: string) => {
  if (t === "market" || t === "selection") tab.value = t;
});

// 深链兜底：外部 URL 带 ?sym=（如浏览器直接访问 /market?sym=xxx 或从 /selection 独立路由跳转）
// 强制切到「行情与事件」tab，让 MarketView 挂载并 loadKline。
watch(() => route.query.sym, (v) => {
  if (v) tab.value = "market";
});
</script>
<template>
  <div class="hub-tabs" role="tablist">
    <button
      type="button"
      class="hub-tab"
      :class="{ active: tab==='market' }"
      role="tab"
      :aria-selected="tab==='market'"
      @click="tab='market'"
    >行情与事件</button>
    <button
      type="button"
      class="hub-tab"
      :class="{ active: tab==='selection' }"
      role="tab"
      :aria-selected="tab==='selection'"
      @click="tab='selection'"
    >选股研判</button>
  </div>
  <Market v-if="tab==='market'" /><Selection v-else />
</template>

<style scoped>
.hub-tabs {
  display: flex;
  gap: 2px;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--border, #e6e8f0);
}
.hub-tab {
  appearance: none;
  background: transparent;
  border: none;
  padding: 8px 16px;
  font-size: 14px;
  font-weight: 500;
  color: var(--text-2, #69708a);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  border-radius: 6px 6px 0 0;
  transition: color .15s, background .15s, border-color .15s;
}
.hub-tab:hover {
  color: var(--text, #202433);
  background: var(--bg-2, #eef0f6);
}
.hub-tab.active {
  color: var(--primary, #4a59c9);
  border-bottom-color: var(--primary, #4a59c9);
  background: transparent;
}
.hub-tab:focus-visible {
  outline: 2px solid var(--primary, #4a59c9);
  outline-offset: -2px;
}
</style>
