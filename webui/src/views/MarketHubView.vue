<script setup lang="ts">
import { defineAsyncComponent, ref } from "vue";
const Market = defineAsyncComponent(() => import("./MarketView.vue"));
const Selection = defineAsyncComponent(() => import("./SelectionView.vue"));
const tab = ref("market");
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
