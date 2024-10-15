import './assets/main.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import loadVuetify from './plugins/vuetify'

import ResizeObserver from 'resize-observer-polyfill'
import vMathJax from './directives/mathjax'

if (typeof window.ResizeObserver === 'undefined') {
  console.debug('Polyfilling ResizeObserver')
  window.ResizeObserver = ResizeObserver
}

loadVuetify()
  .then((vuetify) => {
    const app = createApp(App)
    app.use(createPinia())
    app.use(vuetify)
    app.use(router)
    app.directive('mathjax', vMathJax)
    app.mount('#app')
  })
  .catch((error) => {
    console.error('Error initializing app:', error)
  })
