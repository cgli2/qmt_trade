<script setup lang="ts">
import { computed } from "vue";
const props = defineProps<{ modelValue: any; label: string }>();
const emit = defineEmits<{ (e: "update:modelValue", value: any): void }>();
const array = computed(() => Array.isArray(props.modelValue));
const object = computed(() => props.modelValue !== null && typeof props.modelValue === "object" && !array.value);
function update(key: string | number, value: any) {
  const next = array.value ? [...props.modelValue] : { ...props.modelValue };
  next[key] = value;
  emit("update:modelValue", next);
}
function remove(index: number) { emit("update:modelValue", props.modelValue.filter((_: any, i: number) => i !== index)); }
function add() {
  const sample = props.modelValue[0];
  emit("update:modelValue", [...props.modelValue, typeof sample === "number" ? 0 : typeof sample === "object" ? {} : ""]);
}
</script>
<template>
  <fieldset v-if="object || array" class="parameter-nested">
    <legend>{{ label }}</legend>
    <div v-for="(value, key) in modelValue" :key="key" class="parameter-entry">
      <ParameterValue :label="String(key)" :model-value="value" @update:model-value="update(key, $event)" />
      <button v-if="array" type="button" class="btn sm ghost" @click="remove(Number(key))" :aria-label="`删除 ${label} 第 ${Number(key) + 1} 项`">删除</button>
    </div>
    <button v-if="array" type="button" class="btn sm ghost" @click="add">添加一项</button>
  </fieldset>
  <label v-else class="parameter-value">
    <span>{{ label }}</span>
    <input v-if="typeof modelValue === 'boolean'" type="checkbox" :checked="modelValue" @change="emit('update:modelValue', ($event.target as HTMLInputElement).checked)" />
    <input v-else-if="typeof modelValue === 'number'" type="number" step="any" :value="modelValue" @input="emit('update:modelValue', ($event.target as HTMLInputElement).valueAsNumber)" />
    <input v-else type="text" :value="modelValue ?? ''" @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)" />
  </label>
</template>
<style scoped>
.parameter-value { display: grid; gap: 6px; margin: 8px 0; }
.parameter-nested { border: 1px solid var(--border); border-radius: 8px; padding: 10px; min-width: 0; }
.parameter-entry { display: flex; align-items: center; gap: 8px; }
.parameter-entry > :first-child { flex: 1; min-width: 0; }
input[type=checkbox] { width: auto; justify-self: start; }
</style>
