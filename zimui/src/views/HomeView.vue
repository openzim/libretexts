<script setup lang="ts">
import { watch, ref } from 'vue'

import { useMainStore } from '@/stores/main'
import { useRoute } from 'vue-router'
import type { SharedPage } from '@/types/shared'

const main = useMainStore()

const route = useRoute()

const getPage = function () {
  if (main.shared === null) {
    return null
  }
  let path = route.params.pathMatch
  if (Array.isArray(path)) {
    if (path.length != 1) {
      console.error('Improper path length: ' + path)
      return null
    }
    path = path[0]
  }
  if (path == '') {
    path = main.shared.rootPagePath
  }
  const page = main.pagesByPath[path] || null
  if (page) {
    main.fetchPageContent(page)
  }
  return page
}

const page = ref<SharedPage | null>(getPage())

watch(
  () => (main.shared ? route.params.pathMatch : undefined),
  () => {
    page.value = getPage()
  }
)
</script>

<template>
  <!-- Reproduce DOM structure of libretexts.org for proper CSS functioning -->
  <main class="elm-skin-container">
    <article id="elm-main-content" class="elm-content-container">
      <h1>{{ page?.title }}</h1>
      <section
        class="mt-content-container"
        v-if="main.pageContent"
        v-html="main.pageContent.htmlBody"
        v-mathjax="page"
      ></section>
      <div v-else>Page not found</div>
    </article>
  </main>
</template>
