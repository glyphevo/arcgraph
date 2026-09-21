<script setup lang="ts">
const props = defineProps<{
  title: string;
  featured?: boolean;
}>();
const emit = defineEmits<{
  (event: "select", value: string): void;
}>();
</script>

<template>
  <article>{{ props.title }}</article>
</template>
