import { createRouter, createWebHistory } from 'vue-router'

export default createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/login', component: () => import('./views/Login.vue') },
    { path: '/', component: () => import('./views/Dashboard.vue') },
  ],
})
