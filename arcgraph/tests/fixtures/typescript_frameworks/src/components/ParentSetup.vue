<script setup lang="ts">
import ChildCard from "./ChildCard.vue";

const activeComponent = ChildCard;
</script>

<template>
  <section>
    <ChildCard title="Hello" @select="() => undefined" />
    <component :is="activeComponent" />
  </section>
</template>
