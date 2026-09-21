<script lang="ts">
export default {
  name: "OptionsCard",
  props: {
    label: String,
  },
  emits: ["save"],
};
</script>

<template>
  <button>{{ label }}</button>
</template>
