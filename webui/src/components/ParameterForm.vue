<script setup lang="ts">
import { computed } from "vue";
import ParameterValue from "./ParameterValue.vue";
const props = defineProps<{ modelValue: Record<string, any>; schema: any[] }>();
const emit = defineEmits<{ (e: "update:modelValue", value: Record<string, any>): void }>();
const core = computed(() => props.schema.filter(f => f.level === "core").slice(0, 8));
const advanced = computed(() => props.schema.filter(f => !core.value.includes(f)));
function update(key: string, value: any) { emit("update:modelValue", { ...props.modelValue, [key]: value }); }
</script>
<template>
  <div>
    <div class="parameter-grid">
      <div v-for="field in core" :key="field.key">
        <ParameterValue :label="field.label" :model-value="modelValue[field.key] ?? field.default" @update:model-value="update(field.key, $event)" />
        <small class="muted">{{ field.description }}</small>
      </div>
    </div>
    <details><summary>高级参数（{{ advanced.length }}）</summary>
      <div v-for="field in advanced" :key="field.key">
        <ParameterValue :label="field.label" :model-value="modelValue[field.key] ?? field.default" @update:model-value="update(field.key, $event)" />
        <small class="muted">{{ field.description }}</small>
      </div>
    </details>
    <details><summary>专家与诊断：完整参数</summary><pre>{{ modelValue && Object.keys(modelValue).length ? JSON.stringify(modelValue, null, 2) : '（无参数数据）' }}</pre></details>
  </div>
</template>
<style scoped>
.parameter-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }
details { margin-top: 16px; } summary { cursor: pointer; padding: 8px 0; } pre { overflow: auto; }
</style>
