<script setup lang="ts">
defineProps<{ title: string }>();
const emit = defineEmits<{ (e: "close"): void }>();
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <h3>{{ title }}</h3>
      <slot />
      <div class="modal-actions">
        <slot name="actions" />
      </div>
    </div>
  </div>
</template>
