import { createRouter, createWebHistory } from "vue-router";
import ParentSetup from "./components/ParentSetup.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/vue",
      component: ParentSetup,
    },
  ],
});
