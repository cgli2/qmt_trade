<script setup lang="ts">
// 系统设置：参数配置 / 数据源 / LLM 模型 / 消息推送 四合一（tabs），
// 直接复用原有页面组件，切换不丢状态（keep-alive）。
import { ref } from "vue";
import ConfigView from "@/views/ConfigView.vue";
import DatasourceView from "@/views/DatasourceView.vue";
import LlmView from "@/views/LlmView.vue";
import NotifyView from "@/views/NotifyView.vue";

// 注意：comp 必须直接放组件对象。若包一层 shallowRef，TABS 是普通数组，
// 模板不会自动解包，<component :is> 拿到 Ref 对象会解析失败渲染空白。
const TABS = [
  { key: "config", label: "⚙️ 参数配置", comp: ConfigView },
  { key: "datasource", label: "🔌 数据源", comp: DatasourceView },
  { key: "llm", label: "🤖 LLM 模型", comp: LlmView },
  { key: "notify", label: "🔔 消息推送", comp: NotifyView },
];
const cur = ref("config");
</script>

<template>
  <div>
    <div class="set-tabs">
      <button
        v-for="t in TABS" :key="t.key"
        class="set-tab" :class="{ on: cur === t.key }"
        @click="cur = t.key"
      >{{ t.label }}</button>
    </div>
    <keep-alive>
      <component :is="TABS.find((t) => t.key === cur)!.comp" />
    </keep-alive>
  </div>
</template>

<style scoped>
.set-tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.set-tab {
  padding: 8px 16px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg-elev);
  color: var(--text-2);
  cursor: pointer;
  font-size: 14px;
  transition: all 0.15s;
}
.set-tab:hover { border-color: var(--primary); }
.set-tab.on {
  background: var(--primary);
  border-color: var(--primary);
  color: #fff;
  font-weight: 700;
}
</style>
